# Creator host helper (Phase 6a: hello only)

A small program that runs on the **host**, outside Docker, so Creator can
later (Phase 6b) do supervised work outside its container. In 6a it can only
answer `hello`: there is no request that runs a command, reads a file or
changes anything. Background and decisions: `docs/creator-plan.md`, Phase 6.

Nothing in Odysseus installs, starts or changes this helper. You do every step
below by hand, and you can read every file first.

## Files

| File | What it is |
|---|---|
| `creator_helper.py` | The helper. Python 3 standard library only. |
| `creator-helper.service` | systemd unit: runs it as `creator`, no network, locked down. |
| `../docker/creator-helper.yml` | Compose overlay: mounts the socket folder into the container, read-only. |

## Install (as you, with sudo)

```sh
# 1. A dedicated user with no login shell, no home, no sudo rights.
sudo useradd --system --no-create-home --shell /usr/sbin/nologin creator

# 2. The program, owned by root so `creator` can't change it.
sudo install -d -o root -g root -m 0755 /opt/creator-helper
sudo install -o root -g root -m 0644 host_helper/creator_helper.py /opt/creator-helper/

# 3. The socket folder: the only place `creator` can write (besides its log).
sudo install -d -o creator -g creator -m 0755 /srv/creator-helper

# 4. The service.
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
s = socket.socket(socket.AF_UNIX); s.connect("/srv/creator-helper/helper.sock")
s.sendall(b'{"type": "hello"}\n'); print(s.makefile().readline())
EOF
```

You should see `{"ok": true, "type": "hello", "helper": "creator-helper", ...,
"user": "creator", ...}`. In Odysseus: Settings > Secrets > Host helper > Test
connection.

The audit log: `sudo cat /var/log/creator-helper/audit.jsonl` (one line per
connection: who connected, by pid/uid, what they asked, the answer).

## Kill switch

```sh
sudo systemctl stop creator-helper      # cuts it off now; the socket file is removed
sudo systemctl disable creator-helper   # keeps it off after a reboot
```

Uninstall: stop and disable it, then remove `/etc/systemd/system/creator-helper.service`,
`/opt/creator-helper`, `/srv/creator-helper`, `/var/log/creator-helper`, the
`creator` user, and the `COMPOSE_FILE` line.

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
  socket directly, not only through Odysseus. With hello only, that's harmless.
  It matters for 6b; see the plan doc.

## Not verified yet

- Connecting through a **read-only** bind mount (`:ro`). Linux allows it (the
  read-only check doesn't apply to sockets), but it hasn't been tried in this
  container. If Test connection reports a read-only file system error, drop
  the `:ro` from `docker/creator-helper.yml`.
- The unit's hardening on your systemd version. `systemctl status` shows an
  error if a setting isn't supported; `systemd-analyze security creator-helper`
  rates it.
