# Creator host helper

A small program that runs on the **host**, outside Docker, so Creator can do
supervised work outside its container. It answers two requests: `hello`
(the connection test) and `run` (one shell command as the user `creator`).
Background and decisions: `docs/creator-plan.md`, Phase 6.

Nothing in Odysseus installs, starts or changes this helper. You do every step
below by hand, and you can read every file first.

## Files

| File | What it is |
|---|---|
| `creator_helper.py` | The helper. Python 3 standard library only. |
| `creator-helper.service` | systemd unit: runs it as `creator`, locked down (see "What creator can do"). |
| `50-creator-apache.rules` | polkit rule: lets `creator` start/reload/restart Apache, nothing else. |
| `../docker/creator-helper.yml` | Compose overlay: mounts the socket folder into the container, read-only. |

## Install (as you, with sudo)

```sh
# 1. A dedicated user with no login shell, no home, no sudo rights.
sudo useradd --system --no-create-home --shell /usr/sbin/nologin creator

# 2. The program, owned by root so `creator` can't change it.
sudo install -d -o root -g root -m 0755 /opt/creator-helper
sudo install -o root -g root -m 0644 host_helper/creator_helper.py /opt/creator-helper/

# 3. The helper's folder: its socket, plus work/ and home/ for commands.
sudo install -d -o creator -g creator -m 0755 /srv/creator-helper

# 4. What creator may do (each step is optional; leave one out and Creator
#    simply gets "Permission denied" there):
#    a) edit the web root, including files added later
sudo setfacl -R -m u:creator:rwX /var/www/html
sudo setfacl -R -d -m u:creator:rwX /var/www/html
#    b) read Apache's logs (not the adm group, which would open auth.log too)
sudo setfacl -m u:creator:rx /var/log/apache2
sudo setfacl -m u:creator:r /var/log/apache2/*
sudo setfacl -d -m u:creator:r /var/log/apache2
#    c) start/reload/restart Apache
sudo install -o root -g root -m 0644 host_helper/50-creator-apache.rules /etc/polkit-1/rules.d/

# 5. The service.
sudo install -o root -g root -m 0644 host_helper/creator-helper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now creator-helper
systemctl status creator-helper
```

Then mount the socket folder into the container. In `.env`:

```
COMPOSE_FILE=docker-compose.yml:docker/creator-helper.yml
```

and rebuild: `sudo docker compose up -d --build`.

## Check it

On the host, as you (uid 1000):

```sh
python3 - <<'EOF'
import socket, json
def ask(req):
    s = socket.socket(socket.AF_UNIX); s.connect("/srv/creator-helper/helper.sock")
    s.sendall((json.dumps(req) + "\n").encode()); return s.makefile().readline()
print(ask({"type": "hello"}))
print(ask({"type": "run", "command": "id; ls -ld /var/www/html; systemctl reload apache2 && echo reloaded"}))
EOF
```

`hello` should show `"user": "creator"` and `"capabilities": ["hello", "run"]`.
The `run` should show `uid=...(creator)` and `reloaded` (if you did step 4c).

In Odysseus: Settings > Secrets > Host helper > Test connection. A Creator job
started after that says "Host helper connected" in its timeline, and gets the
`host_exec` tool.

The audit log: `sudo cat /var/log/creator-helper/audit.jsonl`. One line per
connection: who connected (pid/uid), what they asked, the full command with
secrets blanked, exit code, time, output sizes and the first 2,000 characters
of output.

## What creator can do

This is the real limit on what Creator can do on the host, so it's worth
knowing exactly:

- **Read** what any ordinary user can read (most of `/etc`, `/usr`, …), plus
  Apache's logs if you did step 4b.
- **Write** only `/var/www/html` (step 4a) and `/srv/creator-helper`. The unit
  makes the rest of the file system read-only to it (`ProtectSystem=strict`),
  even where file permissions would allow more, and `/home` (yours, with
  Odysseus's `data/` and its keys) invisible (`ProtectHome=yes`).
- **Apache**: start, reload and restart (step 4c). Not stop, enable or disable,
  and no other service.
- **No root**: no sudo, no setuid programs (`NoNewPrivileges`), no
  capabilities. Editing Apache's config and installing packages are root work
  and belong to Phase 5.
- **Network**: allowed for commands (so it can check a page with `curl`). The
  helper itself listens on nothing but its socket.

How a command runs: `/bin/bash -c` in `/srv/creator-helper/work`, with a clean
environment (`HOME=/srv/creator-helper/home`), no stdin, `umask 022` (so files
it writes in the web root are readable by Apache), one command at a time,
2 minutes by default and 10 at most. When it ends, everything it started is
stopped. Closing the connection (Creator's Stop) stops it too.

In Creator, every host command waits for your OK: "Allow once", "Allow all
host commands for this job", or "Deny". Protected paths still ask every time.

## Kill switch

```sh
sudo systemctl stop creator-helper      # cuts it off now, and stops every command it started
sudo systemctl disable creator-helper   # keeps it off after a reboot
```

Uninstall: stop and disable it; remove `/etc/systemd/system/creator-helper.service`,
`/etc/polkit-1/rules.d/50-creator-apache.rules`, `/opt/creator-helper`,
`/srv/creator-helper` and `/var/log/creator-helper`; take the ACLs off
(`sudo setfacl -R -x u:creator -x d:u:creator /var/www/html /var/log/apache2`);
remove the `creator` user and the `COMPOSE_FILE` line.

## Who can connect

- **The decision is SO_PEERCRED.** On every connection the kernel tells the
  helper the connecting process's uid, which the client can't fake. Only uids
  given with `--allow-uid` (the unit uses 1000, the container's user) get an
  answer; anyone else gets `{"ok": false, "error": "not allowed"}` before
  anything they sent is read, and the attempt is logged.
- **The socket file is mode 0666.** Connecting to a Unix socket needs write
  permission on it, so file permissions alone could only let uid 1000 in by
  giving `creator` a group shared with uid 1000 (your own `madsen` group),
  which would let `creator` read your group-readable files. The peer check is
  stricter than a group anyway: it refuses root, including root in the
  container. If you want the file permission tight as well, a POSIX ACL does it
  without a shared group: `sudo setfacl -m u:1000:rw /srv/creator-helper/helper.sock`
  after each start, with the mode set to 0600 (not built in; ask if you want it).
- **uid 1000 is also you on the host.** Docker here doesn't remap user ids, so
  the container's user and your own account are the same uid, and your own
  programs can talk to the helper too. That gives them nothing you don't
  already have (you have sudo).
- **Inside the container, the agent's bash is uid 1000 too.** It can reach the
  socket directly, not only through Creator's `host_exec`, and so skip the
  approval pause and Odysseus's own log. It can't get more than `host_exec`
  would: commands as `creator`. That's why the list above, and the helper's own
  audit log (out of the container's reach), are the real safeguards.

## Known limits

- A command that detaches itself on purpose (`setsid`, `nohup … &` plus
  `disown`) leaves its process group, so it isn't stopped when the command
  ends. It is still in the service's cgroup: `systemctl stop creator-helper`
  stops it, and `systemctl status creator-helper` lists it.
- Commands run one at a time; a second one while the first runs gets "busy".
- Output is cut at 256 KB per stream (the reply says how much there was).

## Not verified yet

- Connecting through the **read-only** bind mount (`:ro`). Linux allows it (the
  read-only check doesn't apply to sockets), but it hasn't been tried in this
  container. If Test connection reports a read-only file system error, drop
  the `:ro` from `docker/creator-helper.yml`.
- The unit and the polkit rule on the real system: the unit passes
  `systemd-analyze verify`, and the rule's logic was checked against a stand-in
  for polkit, but neither has run for real. `systemd-analyze security
  creator-helper` rates the unit.
