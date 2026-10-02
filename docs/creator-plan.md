# Creator Mode: Programming Plan

Status (2026-10-01, branch `creator-mode`; Phase 6a on branch `creator-phase6a`): Phases 1, 2, 3 and 4 are built, tested, and smoke-tested against a real model in the Docker container. The "all tools in one turn" TODO is done. **Now: Phase 7 (the Creator window), before Phase 6.** Steps 7a–7d are done: history route, the window, live run with Stop, and answering pauses. Phases 5, 6 and 8 are not started.
Items marked **[CHECK]** are things not yet looked at, so their size isn't known.

## Purpose, scope and safeguards (read this first)
**What this is.** Creator mode is a feature of my own self-hosted Odysseus install (a fork of the open-source Odysseus app), running in Docker on my own Debian machine. I'm its only owner and administrator. It lets an AI agent that I start carry out admin tasks on this one machine for me (install a package, edit a config file, fix a web page), and report back what it did. It does the same work I'd otherwise do by hand in a terminal, with me in control.

**Why it needs host access.** The agent runs inside the Odysseus container, which is deliberately sandboxed: it can't see or change the host. Useful admin work has to happen on the host. Phase 6 adds a narrow, supervised way for it to do that.

**What it is not.** It's not remote access. Nothing listens on a network port. It can't be reached from other machines or the internet. It doesn't hide, doesn't install or start itself, and doesn't change its own permissions. It only acts when I start a Creator job, and I can see and stop every step.

**Safeguards that already exist (Phases 1–4):**
- **Off by default.** Creator is behind a permission (`can_use_creator`) that's off by default. Only I (the admin) have it.
- **One job at a time**, with a hard time limit (default 60 minutes) and a stop button that also kills the running command.
- **I approve gated actions.** A run pauses for my approval before acting on anything it read from an untrusted source, and before touching protected paths (`creator_protected_paths`, plus the app's own key and database files, which are always protected).
- **Everything is logged.** A full audit log on disk (mode 0600) records every command, result, approval and pause, with secrets blanked out.
- **Secrets are switched.** Each one has an on/off switch the server checks. The agent can't get a switched-off secret by asking for it.

**Safeguards Phase 6 adds:**
- **A dedicated, unprivileged user.** The helper runs as a dedicated host user (`creator`) with no sudo rights in 6a and 6b. Root is not part of Phase 6 at all; it's Phase 5, behind its own switch, and decided separately.
- **One local socket file.** It's reachable only through a socket file in the Odysseus data folder, with strict file permissions. The helper also checks the connecting process's user ID (`SO_PEERCRED`) and refuses anyone but the container's user.
- **No network.** No TCP/UDP listener, ever.
- **Narrow requests.** Fixed request types only: `hello` in 6a, then `run` in 6b. Every request has a time limit and an output size limit.
- **Its own log.** Every request and its result go to an audit log on the host that I can read.
- **A kill switch.** Stopping the helper (`systemctl stop creator-helper`) cuts off all host access immediately. Disabling the service keeps it off.
- **I install it.** I install and start it by hand, as a systemd unit I can read. Nothing in Odysseus can install, start or modify it.

## The idea in one paragraph
A new mode, like Deep Research, called **Creator**. You give it a task. It works through the task using the server the way you would, keeps finding workarounds when it hits a problem, and only stops to ask you when it is truly blocked. When it finishes, it writes a report. A "Secrets" section holds passwords and tokens, each with an on/off switch. Root commands go through a server-side broker, so the agent never sees the root password.

## Decisions to make before coding (Phase 0)
1. **How does Creator reach the real server?** (the container is only a sandbox)
   - A. SSH from the container to the host with a dedicated key and a dedicated user. Not chosen: each new connection adds a delay unless the connection is kept open (ControlMaster), and the socket helper does the same job faster. Kept as the fallback.
   - B. Mount the Docker socket into the container. Simple, but it is effectively full root on the host, with no way to limit it.
   - **C. A small helper program on the host, reached through a socket file shared with the container. CHOSEN.** It is fast (a local file, not a network connection), and it is also where the root-password idea fits, because the helper can add the password itself.
   - Decision: **C**. If socket files turn out not to work across the shared folder, fall back to A with a kept-open connection.
2. **Time limit per run** (suggested default: 60 minutes, adjustable).
3. **Which actions always need your OK** even in Creator mode (suggested: none by default, but the list should exist and be editable).

## Phase 1: Skeleton (the mode exists and runs) — DONE
- [x] **[CHECK]** Trace how the chat decides which mode a message runs in, and how the UI toggles/panels for research are wired. Findings:
  - Mode is decided per message in `routes/chat_routes.py`: the form's `mode` field (`chat`/`agent`) is escalated to `agent` by tool/search/web intent, and research is a separate `do_research` flag (`_research_flags`), turned off when the user lacks `can_use_research`.
  - Deep Research does **not** use the agent loop. `src/deep_research.py` is its own search/extract/synthesize loop, run by `src/research_handler.py`. So Creator is modelled on its *job pattern* (background task, routes, privilege), not its engine. The engine follows `src/task_scheduler.py` / `src/bg_monitor.py`, which already run `stream_agent_loop` headless.
  - UI: the Research toggle is `#research-toggle-btn` in `static/index.html`, referenced from `static/app.js`, `static/js/chat.js`, `static/js/sessions.js`, and hidden by privilege in `static/js/init.js`. The panel is `static/js/research/panel.js` + `jobs.js` (~1,600 lines together). A Creator panel (Phase 7) is a real chunk of work, but it can be much smaller than that.
- [x] New engine file `src/creator_mode.py` (`CreatorManager`): runs each job as an asyncio background task.
- [x] New routes in `routes/creator_routes.py`: `POST /api/creator/start`, `GET /api/creator/status/{job_id}?since=N`, `POST /api/creator/stop/{job_id}`, `GET /api/creator/report/{job_id}`. Owner-scoped (someone else's job is a 404).
- [x] New permission flag `can_use_creator`, off by default (admins have it). Unlike `require_privilege`, the Creator check fails closed when the key is missing. The agent's own loopback user can't call these routes, and `/api/creator` is blocked in `app_api`.
- [x] Job record in the database: `creator_jobs` table (`CreatorJob` in `core/database.py`) with task, status, start/end time, report, error, model, event log (JSON). Jobs left `running` by a restart are marked `interrupted` at startup.
- [x] Engine runs `stream_agent_loop` with `max_rounds=500`, `max_tool_calls=2000`, `workload="background"`, and the user's privilege-based + global disabled tools.
- Notes for later phases:
  - No time limit yet (Phase 3). A run only ends when the model stops, a cap is hit, or someone calls stop.
  - If a tool asks for approval mid-run, the approval is retired (the action is not run) and the job ends as `blocked`, the same as scheduled tasks. Phase 2 replaces this with a real pause.
  - The report is just the model's final text for now. Phase 2 makes it structured.
  - No UI yet (Phase 7). Use the routes directly to try it.

## Phase 2: The "never give up" behaviour — DONE
All in `src/creator_mode.py`, plus small hooks in `src/agent_loop.py`.
- [x] Creator-specific instructions (`CREATOR_SYSTEM_PROMPT`). They tell the model to:
  - try another approach when something fails,
  - write a `PROGRESS:` line after each meaningful step (its list of what it tried),
  - never repeat a failed command,
  - ask only when blocked on something only you can give (a credential, a switched-off secret, access, a decision), using `ask_user`,
  - finish with the four-heading report and a `STATUS: DONE` line, or `STATUS: BLOCKED: …`.
- [x] Failure tracking. "The same way" means the same tool and command (whitespace ignored), the same exit code, and the same error text (numbers ignored, so pids and timings don't count).
  - On the 3rd identical failure, the result the model reads says the command is now refused.
  - From then on, the agent loop refuses that exact command before running it (new `tool_refusal_check` hook) and tells the model to change approach. A different error, or a changed command, is a new attempt.
  - Failures are listed in the report, and in the context given to the model whenever the run continues.
- [x] Progress notes. `PROGRESS:` lines are saved as they're written. Creator also writes its own checkpoint note every 10 tool calls, so notes exist even if the model writes none. Notes go to the job state, the event log and the audit log.
  - **Checkpoints:** a run is now a series of agent-loop segments of up to 30 rounds. When one ends, the next starts from a fresh context, rebuilt from the task, the notes, the last 25 tool calls and the refused commands. That's what keeps a long run from losing its place.
  - The whole-run caps (500 rounds, 2000 tool calls) count across segments. Reaching them ends the job as `limit`.
- [x] Structured final report (`report` is markdown; `report_data` in `/report` has the same as data):
  - what was asked, and the status,
  - what was done / worked / didn't work / is left, taken from the model's headings, with a clear placeholder where it skipped one,
  - Creator's own failure list, added under "What didn't work",
  - **the exact commands run**, from Creator's own log (each with exit code, and marked if you approved it),
  - the progress notes.

  A run that stops early still gets a report, and "What's left" says why it stopped.
- [x] A real pause instead of the "blocked" ending. The job pauses (status `paused`) when:
  - a command needs approval (a protected path, or an action held back because of untrusted content), or
  - the model calls `ask_user`, or
  - it ends with `STATUS: BLOCKED`.

  `GET /status` shows what it's waiting for (`pause`: kind, question, options, action, allowed choices), plus the notes and `deadline_at`. `POST /api/creator/resume/{id}` answers it:
  - **Approvals** take `{"decision": "approve_once" | "approve_job" | "deny"}`. `approve_once` runs exactly that action: Creator runs it itself, with only that exact command let past the protected-path check, then continues. `approve_job` also lifts the untrusted-content gate for the rest of the job, like chat's "Allow for this task". It's **not** offered for protected paths, which are always approved one at a time. `deny` doesn't run the action, and the model is told to find another way.
  - **Questions / blocked** take `{"answer": "..."}`. It can be empty: "carry on as best you can".
  - Pausing and resuming are in the event log, the live stream and the audit log. A paused job still holds the one-job slot. Stop works while paused. A job left paused by a server restart is marked `interrupted`.
- **Decision: paused time counts toward the time limit.** The limit is wall-clock for the whole run. A hard limit stays hard, and a pause nobody answers can't hold the one-job slot forever. The status shows `deadline_at`, so you can see how long you have to answer. If it runs out while paused, the job ends as `timeout`, with a report.
- **Found while building this:** the agent loop treats any bash/python output as untrusted (`WORKSPACE_UNTRUSTED`). So after a run's first command, every later command, file write or network call needs an approval. Before Phase 2, a real Creator run would have ended as `blocked` at its second command. Now it pauses there, and `approve_job` is the way to let it run. Whether Creator should skip this gate by default belongs to the separate "safety lock" item in the all-tools TODO below. It's unchanged here.
- Limits:
  - **Checkpoints lose detail.** The next segment sees notes, commands and the last reply, not full earlier outputs. A model that writes poor notes works less well after a checkpoint.
  - **Approved actions run outside the agent loop**, so they don't stream `tool_progress`.
  - **Nothing notifies you of a pause** (no email or push). You see it in `/status`, the stream, or the Phase 7 panel.

## Phase 3: Safety net (build before anything powerful) — DONE (server side; buttons come with the Phase 7 panel)
Safety code lives in `src/creator_safety.py`; `src/creator_mode.py` uses it.
- [x] Hard time limit per run. Setting `creator_max_minutes` (default 60, clamped 1–1440). A run can pass `max_minutes` to `/api/creator/start` to override it within that range. A run that hits the limit ends as `timeout`.
- [x] Stop kills the job immediately. **Found:** the bash tool runs commands inside a persistent tmux session named after the session id (`ody-agent-<job id>`), so cancelling the job alone would leave the command running. Stop, timeout and every normal end now also kill that tmux session. `POST /api/creator/stop/{id}` is ready; the button is part of the Phase 7 panel.
- [x] Live log: `GET /api/creator/stream/{id}` is an SSE stream that sends every round and tool start/result as it happens, then a final `{"final": true, "status": ...}` message. Each event has a `seq` number; `?since=N` resumes after event N, and `/status` uses the same numbering. The panel that displays it is Phase 7.
- [x] Full audit log on disk: `data/creator/audit/<job id>.jsonl`, file mode 0600 and folder mode 0700. It records job start (task, model, time limit, protected paths), every tool start (full command) and result (exit code, output up to 100k chars), any block, and the job's end (status and report). The DB event log, report and error are redacted the same way. Redaction blanks known values (secret-shaped settings, secret-shaped environment variables, the run's own API key) and common token shapes (`sk-…`, GitHub/Slack/AWS/Google keys, Bearer tokens, `PASSWORD=…`-style assignments, private-key blocks). Phase 4 adds the Secrets-section values.
- [x] Protected-actions list: setting `creator_protected_paths` (default empty, admin-editable through `/api/auth/settings`). A tool call whose input names one of these paths as a whole path component is not run. It goes through the agent loop's existing approval gate, through a new `protected_action_check` hook on `ToolRunSecurityContext`, which no approval scope can bypass. The job then ends as `blocked`, and its error says which path and what it wanted to run. Phase 2's pause/resume will turn this into "wait for your OK". **Limit:** this is a tripwire, not a sandbox. `cd /; cd etc; rm hosts` reaches `/etc` without spelling it out.
- [x] Only one Creator job at a time, across all users. A second start gets HTTP 409 and doesn't reveal the other job's id.
- Known gap: when a bash command runs without tmux (tmux missing), stop kills the shell process but not processes it started in the background. tmux is in the Docker image, so this mainly matters outside Docker.

## Phase 4: Secrets section — DONE
- [x] **[CHECK]** Read `src/secret_storage.py` properly and confirm it can hold arbitrary secrets, not just email passwords. Findings:
  - Yes. `encrypt(str)`/`decrypt(str)` is generic Fernet encryption, and `core/database.py` already has an `EncryptedText` column type built on it (used for endpoint keys, signatures, calendar tokens). The secrets table uses that, with nothing new needed.
  - One key for everything, at `data/.app_key` (mode 0600), with no rotation. `decrypt` returns `""` on a wrong key or corrupt row instead of raising, so `get_secret` treats an empty value as an error.
  - It only protects the files at rest (a stolen database or backup), not a running process. **Important:** the agent's bash tool is not sandboxed. It runs as the same user, in the same container, starting in the data folder. So an agent that sets out to can read `data/.app_key` and the database and decrypt every secret itself, switch or no switch. The switch stops a run from *being given* a secret, not from *taking* one. Decision: build as planned, add a tripwire (below), and move secrets out of the container in Phase 6.
- [x] Database table `creator_secrets` (`CreatorSecret`): name (unique per owner), description, encrypted value, enabled (default **off**), last used.
- [x] Settings screen: Settings > Secrets (`static/js/secrets.js`, shown only with `can_use_creator`). It can add, edit, delete, and flip each secret's on/off switch. Values are write-only: no route returns one and the screen never shows one. Leaving the value blank when editing keeps the current one. Routes: `GET/POST /api/creator/secrets`, `PATCH/DELETE /api/creator/secrets/{id}`, owner-scoped, gated by `can_use_creator`, and blocked in `app_api` (under `/api/creator`).
- [x] Agent tool `get_secret(name)`. The **server** checks that the call comes from a running Creator job of the same owner, that the secret exists, and that its switch is on. If any check fails, the call fails with a reason and the value never reaches the agent. Switching a secret off takes effect on the next request. Every Creator run is always offered the tool (`forced_tools`). In normal chat it refuses.
- [x] Every request is logged, allowed or denied, to `data/creator/secret_access.jsonl` (0600) and to the job's audit log. Logs hold the name and decision, never the value.
- [x] Output scrubbing. A new `output_redactor` hook in the agent loop blanks known secret values from every tool result before the model reads it and before it's streamed or stored. `get_secret`'s own result is exempt, since handing it over is its job. Scrubbed values are all of the owner's secrets, on or off, plus any value `get_secret` handed out during the run. The model sees only known values blanked, not the token-shape patterns, so it can still read e.g. a `PASSWORD=` line it's debugging. Everything stored (events, audit log, report, error) gets the full Phase 3 redaction plus these values.
- [x] Tripwire: `data/.app_key` and the SQLite database (and its `-wal`/`-shm`/`-journal` files) are always protected paths in every Creator run, by full path and by bare file name (the shell starts inside the data folder). A command naming them is not run, and the job ends as `blocked`. It's a tripwire, not a wall: an indirect command can still reach them.
- [ ] Optional "Creator may use this secret" flag, separate from "enabled". Not built: Creator is currently the only thing that can use a secret, so a second switch would do the same thing as the first. Worth adding once something else can request secrets.
- Not covered: values the *model writes into its own commands* (e.g. `curl -H "token: …"`) reach the shell unredacted, which is the point. They're blanked in the stored event log and audit log, but the tmux pane's scrollback and any files the command writes are not scrubbed.

## Phase 5: Root broker (the "run as root" idea)
How it works: the agent never receives the root password. It calls `run_as_root(command)`. The server does the following:
1. Checks that the root secret's switch is **on**. If it is off, the call fails.
2. Asks the host helper (Phase 6) to run the command with `sudo`, and the password is fed in by the server side, never by the agent.
3. Scrubs the output, logs the command, and returns the result.

- [ ] Tool `run_as_root(command, reason)` with the server-side check.
- [ ] Password is passed in via stdin only. It never goes on a command line, into an environment variable, or into logs.
- [ ] Every root command is written to the audit log with the reason the agent gave.
- [ ] Optional: a block-list of obviously destructive patterns (for example, wiping the root of the disk). This is a safety net, not real protection, so don't rely on it alone.
- [ ] Known limit: root access is root access. The agent can still do damage with a command that looks harmless. The real protections are the on/off switch, the time limit, the log and the stop button.

## Phase 6: Reaching outside the container (host helper over a socket file)
**6a (hello only) — BUILT, NOT INSTALLED** (2026-10-01, on branch `creator-phase6a`, built while you were away: review before merging). Nothing on the host has been changed; the install is yours, by hand, from `host_helper/README.md`.
- `host_helper/creator_helper.py`: standard-library Python (the host has no pip). Listens on one Unix socket, answers `{"type": "hello"}` and nothing else (an unknown type, `run` included, gets an error and the list of what it can do). Per connection: SO_PEERCRED check first (uids from `--allow-uid`; anyone else is refused before their request is read), then one line of JSON, with a 10 s read timeout and a 64 KB size limit. Every connection goes to `/var/log/creator-helper/audit.jsonl` (0600): peer pid/uid/gid, request type, result. Refuses to start as root, with no allowed uid, or if the socket path is a regular file. Removes its socket on stop.
- `host_helper/creator-helper.service`: runs as `creator`, `PrivateNetwork=yes` and `RestrictAddressFamilies=AF_UNIX` (no network at all), no capabilities, `NoNewPrivileges`, `ProtectSystem=strict` with only `/srv/creator-helper` writable, and the rest of the usual hardening. `systemd-analyze verify` is clean. 6b will have to loosen some of it on purpose (commands need to write somewhere).
- **Socket location: `/srv/creator-helper/helper.sock`**, mounted read-only into the container at `/app/host-helper` by an opt-in overlay, `docker/creator-helper.yml` (enable with `COMPOSE_FILE`, like the GPU overlays). Not `/run` + systemd `RuntimeDirectory`: that folder is deleted when the service stops, and a container bind mount would keep pointing at the deleted one until the container restarts. Not under `data/` (the entrypoint takes it over, see the findings below).
- **Decision to confirm: who may connect.** The socket file is mode 0666 and SO_PEERCRED decides. Connecting needs write permission on the socket, so a strict file mode could only let uid 1000 in by giving `creator` a group shared with uid 1000 (your `madsen` group, which would let `creator` read your group-readable files). The peer check is stricter than a group (it also refuses root, container root included). A POSIX ACL (`setfacl -m u:1000:rw`) would make the file permission strict too, without a shared group; not built.
- Odysseus side: `src/creator_host_helper.py` (client; socket path fixed, env `CREATOR_HELPER_SOCKET` overrides, deliberately not an admin setting), route `GET /api/creator/helper/hello` (Creator privilege), and **Settings > Secrets > Host helper > Test connection**.
- Tested: `tests/test_creator_host_helper.py` runs the real helper on a temporary socket (hello, the peer check refusing another uid before reading, no `run`, bad JSON, oversize, silence, stale socket, not overwriting a regular file, the route's privilege gate). Also run by hand with the host's `/usr/bin/python3` (3.11): hello, SIGTERM, socket removed, audit log 0600.
- Not verified: connecting through the read-only bind mount (Linux exempts sockets from the read-only check, but it hasn't been tried in this container), and the unit actually running under systemd as `creator`.

- [x] **[CHECK]** Re-read the compose file's shared `data` folder setup, and test that a socket file works across that mount. (Re-read: `data/` is the wrong place, see the findings below. The socket gets its own mount. Testing across the mount needs the install.) Sockets usually work over a bind mount on Linux, but this is not confirmed. First build a tiny "hello" helper that only answers over the socket, before any real command-running goes in.
- [ ] On the host (you do this): create a dedicated user for the helper, for example `creator`, with limited sudo rights. The helper runs as this user, not your own account.
- [ ] Helper service on the host, started outside Docker. It listens on a socket file inside the shared folder, for example `/home/madsen/odysseus/data/host-helper/helper.sock`. Only the container's user may use that file (strict permissions).
- [ ] Narrow requests only: `run(command)` first, `run_as_root(command)` later. Every request has a time limit and returns the output.
- [ ] Tool `host_exec(command)` in Odysseus: sends a `run` request to the helper. `run_as_root` (Phase 5) goes through the same socket.
- [ ] Decide where the root switch is checked. Option 1: Odysseus checks it and passes the password to the helper over the socket (simpler). Option 2: the helper reads the switch itself (stronger, because a compromised app can't get around it). Not decided yet.
- [ ] Helper audit log: every request and result written to a file you can read, with secrets blanked out.
- [ ] Kill switch: stopping the helper cuts off all host access straight away.
- [ ] Security note: anything that can write to the socket file can run commands on your host. The file's permissions and the dedicated host user are the main protections, so build them first.
- [x] Connection test button in settings (sends "hello" to the helper and shows the reply). Settings > Secrets > Host helper.
- **Found while planning Phase 7 (2026-10-01):**
  - **The socket can't live in `data/`.** On every start, `docker/entrypoint.sh` (`repair_tree_ownership`) changes the owner of everything under `/app/data` to the container user (PUID, 1000). A `creator`-owned socket folder there would be taken over. Use a separate bind mount outside `data/`, e.g. host `/run/creator-helper`, which the entrypoint doesn't touch.
  - **The agent's bash can reach the socket directly.** The Odysseus server and the agent's bash run as the same uid (1000) in the container, so `SO_PEERCRED` can't tell them apart, and bash could talk to the helper without going through `host_exec` (skipping its approvals, protected paths and the app's audit log). A token held by the server doesn't fix it: the host's `kernel.yama.ptrace_scope` is 0, so a same-uid process can read the server's memory. The helper's own limits (fixed request types, time and output limits, its own audit log, the kill switch) are the real boundary, the same "tripwire, not a wall" situation as Phase 4. Running the agent's bash as a separate uid would close it; not decided.

### 6b design: running commands on the host (DRAFT, for review, 2026-10-02)
Nothing here is built. It's the plan for review before any code, with the decisions marked **Decide**.

**What the goal needs.** The "Purpose" section names three kinds of tasks: fix a web page, edit a config file, install a package. On this host that means Apache 2 serving `/var/www/html` (`index.html` owned by you; `images/` by root), its config in `/etc/apache2`, and Debian packages. PostgreSQL 15 and Docker also run here. Nothing needs them yet.

**The boundary is what `creator` can do on the host.** The agent's bash in the container runs as the same uid as the Odysseus server, so it can talk to the socket directly and skip Odysseus's approval pause and Odysseus's own log (see the findings below). It can't get more than `host_exec` would: commands run as `creator`. So:
- **What `creator` is allowed to do is the real limit**, set twice: by file permissions on the host, and by the systemd unit's `ReadWritePaths` (so a permission mistake elsewhere is still blocked). `ProtectHome=yes` stays, which keeps `/home/madsen` (including Odysseus's `data/`, with the app key and database) invisible to it.
- **The helper's audit log is the record to trust**, because the agent can't reach it. Odysseus's log and its approval pauses are supervision on top: useful, not a wall.
- Rejected: running the agent's bash as a separate uid. It would change the bash tool for all agent use (shared with upstream), and the gain is limited to the approval pause.

**The `run` request.**
- `{"type": "run", "command": "...", "timeout_s": 120}` → one reply when it ends: `exit_code`, `stdout`, `stderr` (each cut to 256 KB), `duration_s`, `timed_out`.
- Run with `/bin/bash -c` as `creator`, in `/srv/creator-helper/work`, with a clean environment (fixed `PATH`, `HOME=/srv/creator-helper/home`, `LANG=C.UTF-8`, nothing inherited), `umask 022`, and stdin closed (no interactive prompts).
- Its own process group. That group is killed when the command finishes (no daemons left behind), at the time limit (default 120 s, cap 600 s), or **when the connection closes**, so Creator's Stop (which cancels the request) also kills the host command. The kill switch (`systemctl stop`) kills everything in the service's cgroup.
- One command at a time; a second gets "busy". Only uid 1000, as in 6a.
- Helper audit log: the full command, exit code, duration, output sizes and the first 2 KB of output. **Decide:** commands are logged unredacted (a token the model puts in a command line would be in this log, which only root can read). Alternative: Odysseus sends the values to blank with each request.

**In Odysseus.** A `host_exec(command, timeout_s)` tool, offered only in Creator runs (refused in chat, like `get_secret`), added to Creator's core tools when the helper answers `hello` at job start. It goes through the same checks as bash: protected paths, the untrusted-content gate, the three-failures rule, output scrubbing, the event log and audit log. The Creator window needs nothing new. The system prompt tells the model which tool reaches the host and which reaches the container.

**The rights `creator` would get** (my recommendation):
1. **Write `/var/www/html`** (the web page task): `setfacl -R -m u:creator:rwX` plus the same as default ACL, so new files stay writable, and add it to `ReadWritePaths`. Nothing else on disk.
2. **Read Apache's logs** (`/var/log/apache2`, root:adm 0640) by an ACL on that folder only, not membership of `adm` (which would also open `auth.log` and the rest).
3. **Reload/restart Apache** without sudo, through a polkit rule that allows `creator` to manage `apache2.service` and nothing else. This keeps `NoNewPrivileges=yes` (sudo would need it off for the whole service).
4. **Network for host commands: Decide.** Needed for `curl http://localhost/` checks and any download. 6a has none at all (`PrivateNetwork=yes`). My recommendation: allow it for commands (the helper still listens on nothing but its socket), since checking the page after a fix is part of the job.

**Deliberately not in 6b:**
- **Editing Apache's config.** Write access to `/etc/apache2` plus the right to reload is **root-equivalent**: Apache reads its config as root, and a config line can start a program as root (piped logs) or write any file as root (log paths). So it belongs with Phase 5 and its switch, not hidden in 6b as "just a config folder".
- **Installing packages.** `apt` runs package scripts as root, and `apt install ./file.deb` installs anything: root-equivalent too, so it's Phase 5.
- PostgreSQL and Docker: not needed by any named task. Docker access is full root.

So 6b on its own covers "fix a web page" end to end (edit, reload, check, read the logs). The other two named tasks need Phase 5. That's an honest result: they are root tasks.

**Approvals for host commands: Decide.** (a) The same gates as container commands (after the first `approve_job`, host commands run without asking), or (b) **every `host_exec` asks** (`approve_once` only) for the first while, with a setting to relax it later. I recommend (b) to start: it costs a click per command and gives you a feel for what it does on the real machine.

**Build order.** (1) `run` in the helper, with tests against a real temporary socket (exit codes, time limit, kill on disconnect, no leftover processes, output caps, busy). (2) `host_exec` in Odysseus, with tests. (3) An updated unit file and README with the rights steps above, which you apply by hand. (4) A live test: "change the title of the page in /var/www/html to X, reload Apache and check it with curl."

## Phase 7: UI (Creator window) — IN PROGRESS
**Decision: Phase 7 before Phase 6.** Stop, pause/approve, the live log and the deadline exist on the server but are only usable with curl. Phase 6b gives the agent real host access, and watching a run (and stopping it) should be easy before then. Phase 8's stop and time-limit tests need the UI too.

**Decision: Creator is its own window, not a chat-bar toggle.** It opens from the sidebar (Tools, next to Deep Research) and the icon rail, as a window like Research's. Inside it you chat with Creator: your task is the first message, progress notes, commands and pauses come in as the run goes, and the report is the last message. It has a **job history** list of your past jobs. Creator doesn't go through `chat_routes`, so a chat-bar toggle would only have redirected Send.

- [x] **[CHECK]** Read how the research panel is built. Findings: it's an overlay built on demand (`static/js/research/panel.js`, `openPanel()` creates `#research-overlay` with a `.modal-content` pane, draggable by its header, minimize/close buttons). Wiring is spread over: the sidebar item and rail button in `static/index.html`, the click handler and `_railToolMap` in `static/app.js`, the modal registry and `_AUTO_WIRE` in `static/js/modalManager.js` (dock chip when minimized), `static/js/ui_visibility.js`, `static/js/keyboard-shortcuts.js`, and privilege hiding in `static/js/init.js`. The research panel is ~1,640 lines; Creator's should be a third of that.
- [x] **7a: history route.** `GET /api/creator/jobs?limit=N` (default 50, max 200): the caller's jobs, newest first, with id, task (first 300 chars), status (live for a running job), times, model and whether there's a report. No events or report text.
- [x] **7b: the window.** `static/js/creator/panel.js` (DOM) and `static/js/creator/view.js` (event log → timeline, no DOM, tested with node in `tests/test_creator_window_js.py`). Styles at the end of `static/style.css`.
  - "Creator" in Tools in the sidebar and on the icon rail, hidden without `can_use_creator` (`init.js`). The module is loaded on first click. It's registered with `modalManager` (dock chip, rail badge) and the "toggle window" shortcut.
  - Left: "+ New job" and the history (status dot, first line of the task, status and age). On a phone it's behind a "Jobs" button.
  - Right: the task as your message, then progress notes, commands (expandable, with exit code and an "approved" mark), pauses and your answers, ending notices, and the report (markdown, via the chat's `mdToHtml`), with the audit log's path under it. Everything except the report goes in as text, not HTML.
  - Composer (only with "New job" selected): the task, a time limit (blank = server default, remembered), and "Approve untrusted actions up front" (not remembered: it's a per-run decision). Ctrl/Cmd+Enter starts. A 409 says a job is already running.
  - Endpoint and model pickers (added after 7d), as in Deep Research: your enabled LLM endpoints from `/api/model-endpoints`, then that endpoint's models. "Default" sends neither, so the run uses your default/chat model. The choice is remembered.
- [x] **7c: live run.** A running or paused job is followed over `/api/creator/stream` (SSE), starting after the last event `/status` gave.
  - New events are added as they arrive (redrawn at most once per frame). Commands you opened stay open, and the view only follows new output if you were already at the bottom. Duplicate events (by `seq`) are ignored.
  - The status pill and the history dot change on pause/resume. The header shows the time left until the hard limit (`deadline_at`, ticking every second; paused time counts), and a red **Stop** button (one click, no confirmation: it's the safety control).
  - If the stream drops, the window reconnects by hand from the last `seq` (1 s, 2 s, 4 s … up to 30 s) and says so in the header. The browser's own retry would replay events from the original `?since=`.
  - When the stream's final message comes, the job is loaded again, now with its report. Closing the window or switching jobs ends the stream; minimizing doesn't.
  - Tested: helpers with node (`tests/test_creator_window_js.py`); the panel's live behaviour with a throwaway jsdom harness (fake API and EventSource) that isn't in the repo, since the repo has no jsdom.
  - The "Refresh" button from 7b is gone.
- [x] **7d: pauses.** While the job on screen is paused, the bottom bar becomes a reply bar (the timeline shows the pause itself: question and action).
  - **Approvals:** the choices the server allows for that pause (`pause.choices`): "Approve once", "Approve for this job", "Deny". A protected path gets only "Approve once" and "Deny", with a line saying why. If `choices` is ever missing, the bar falls back to once/deny, never "for this job". The text box is an optional note sent with the choice (`answer`).
  - **Questions / blocked:** the agent's options as buttons, plus a text answer. With the box empty, Send reads "Carry on without an answer" (the engine's "carry on as best you can").
  - A pause that arrives live fetches `/status` for its choices. Typed text survives live redraws for the same pause; a new pause starts empty. A 409 from `/resume` (stopped, timed out or answered elsewhere) reloads the job; a 400 shows the server's message.
  - **Attention:** a pause while the window is minimized (or the tab is hidden) puts a dot on the sidebar and rail buttons, and shows a browser notification if you've already allowed notifications for the site. The window never asks for that permission. Nothing notifies you while the window is closed: the live stream only runs while it's open or minimized.
  - Tested: `replyControls`/`sendLabel` with node; the reply bar end to end with the jsdom harness (approve-for-job with a note, protected path, question by chip and by text, blocked with an empty answer, 409, 400, the dot and notification).
- [x] Opening the window with a job running or paused goes straight to that job (unless you've already clicked elsewhere or have an unsent task typed). An unsent task survives closing the window (Esc, a click outside it).
- [x] Secrets screen (done in Phase 4: Settings > Secrets).
- Not shown live: the model's own text between tool calls. The engine drops text deltas; only `PROGRESS:` notes, tool calls, pauses and the report reach the event log. Adding a per-round text event is possible later if the window feels too quiet.

## Phase 8: Testing
Most of these are already covered by unit tests with a fake model. What's left is doing each once for real, through the Creator window, against a real model in the container. Rebuild, then reload the page twice (or Ctrl+Shift+R): the service worker serves the cached page first.

- [ ] **Harmless first task.** New job: "List the files in /app/data/agent_workspace and write a report." Expect: the live view shows the commands as they run, it pauses once at the untrusted-content check (Approve for this job), then it finishes with a report whose "Exact commands" match the timeline. Then once more with "Approve untrusted actions up front" ticked: no pause.
- [ ] **Fails on purpose.** "Run the command `definitely-not-a-command --version` and tell me its version." Expect: different approaches in the timeline, never the same failing command 4 times (a "Refused from now on" line if it tries), and a report that lists the failures under "What didn't work". *(Automated: `test_same_command_failing_the_same_way_three_times_is_then_refused`, `test_three_failures_rule_in_the_real_agent_loop`.)*
- [ ] **A question.** "Ask me which file name to use, then create that file in the workspace." Expect: the reply bar shows the question (and options, if any); your answer appears in the timeline and the job continues. Try "Carry on without an answer" once too.
- [ ] **Protected path.** Add `/etc` to `creator_protected_paths`, then: "Read /etc/hostname." Expect: only "Approve once" and "Deny", with the protected-path line. Deny once, and check it finds another way or says it's blocked. *(Automated: `test_protected_path_pause_approve_once_runs_exactly_that_action`, `test_approve_untrusted_never_lifts_protected_paths`.)*
- [ ] **Secret switch.** Add a secret `TEST_TOKEN` (off): "Use get_secret to read TEST_TOKEN and tell me its length." Expect: refused, and it asks you or reports blocked. Switch it on and repeat: allowed. Then check the token's value appears nowhere: the timeline, the report, the audit log (`data/creator/audit/<job>.jsonl`), `data/creator/secret_access.jsonl`. *(Automated: `test_switched_off_secret_is_refused_and_value_never_returned`, `test_stored_secret_values_are_redacted_from_everything_a_run_stores`, `test_agent_loop_scrubs_tool_results_before_the_model_sees_them`.)* `run_as_root` waits for Phase 5.
- [ ] **Stop.** "Run `sleep 600` and then report." Press Stop while it runs. Expect: status "Stopped" within a few seconds, a report, and no `sleep` left in the container (`ps aux`). *(Automated: `test_stop_cancels_running_job`, `test_stop_while_paused`.)*
- [ ] **Time limit.** Same task with time limit 1 minute. Expect: the countdown reaches "time's up" and the job ends as "Time limit reached" with a report. *(Automated: `test_time_limit_stops_job_as_timeout`, `test_time_limit_counts_paused_time`.)*
- [ ] **Window behaviour.** While a job runs: minimize the window and let it pause (dot on the Creator button); close and reopen (it opens on the running job); reload the page mid-run (it picks up where it was); open a finished job from the history.

## Separate TODO: access to all tools within the same turn
**Problem:** I get a smaller set of tools each turn than the server has switched on. When I need a tool that isn't in the set, you have to send a second message.

- [x] **[CHECK]** Find the code that picks which tools I get each turn. It's in `stream_agent_loop`, `src/agent_loop.py` around line 3875 ("RAG-based tool selection").
- [x] Find out how it decides. Once per agent-loop call, in this order:
  1. The caller's `relevant_tools`, if given (scheduled tasks).
  2. Otherwise, a vector search of the tool index with the message text: the top 8 tools plus `ALWAYS_AVAILABLE` (it was only `ask_user`, `manage_memory`, `update_plan`).
  3. If the index is slow (over 1.5 s, seen in the real Creator run) or broken, a keyword list.
  4. Then additions: tools for topics detected in the message (files, email, web…), the chat-bar toggles (web search, browser) as `forced_tools`, document tools for an open document, a fixed "Terminus" set for coding requests in a workspace, and tools a loaded skill declares.

  The guess (toggles plus message text) was right. Mid-turn, only a skill could add tools. There are 72 tools with schemas, about 15,500 tokens if all were sent; a typical turn sent 24.
  - **Creator problem found:** each new segment ran the search on Creator's own continuation text (notes and commands), so the tool set could shift mid-run.
- [x] Decide on the fix. **Chosen: core set + load-on-demand.**
  - New `load_tools` tool (`src/tool_loading.py`), always available in every agent turn (added to `ALWAYS_AVAILABLE`). `{"names": [...]}` loads tools; they're offered from the model's next step, through the same path skills use. `{"search": "word"}` or `{}` lists loadable tools. Tools that are disabled or not allowed for the run (privileges, admin settings, plan mode, public users, tool policy) are refused, and the loop filters them again. Loading changes only what's offered: each tool keeps its own gates.
  - Creator gets a fixed core set every segment (`CREATOR_CORE_TOOLS`: shell and files, web, `ask_user`, `update_plan`, `get_secret`, `load_tools`), passed as both `relevant_tools` (no search on continuation text) and `forced_tools`. Tools loaded during a job stay loaded for the rest of it.
  - **Smoke-tested (2026-10-01, claude-sonnet-5-5):** `load_tools` search → load → use worked when the model was told to use it. In ordinary tasks ("create a note", "which models are configured") the model never needed it: the loop's topic detection still adds tools to Creator's core set based on the task text (notes → `manage_notes`, models → `list_models`). The core set is a guaranteed minimum, not a limit. That's useful, and it means `load_tools` is mainly a fallback for tasks whose wording doesn't name the topic.
  - Not covered: MCP tools can't be loaded this way yet. Fence-style models get a loaded tool's usage text in the `load_tools` result, not in the system prompt.
  - Rejected: all tools every turn (about 15,500 more tokens per round, and weaker local models get confused by 70+ tools); `load_tools` alone (Creator's set would still shift between segments).
- [x] Check the safety lock that blocks some tools after I read web pages or emails. Decide whether to keep it, loosen it, or leave it off in Creator mode.
  - **Found (first real run, 2026-10-01):** the lock is stricter than "web pages or emails". Results from the 11 local tools (`bash`, `python`, `read_file`, `ls`, `grep`, `glob`, `get_workspace`, `write_file`, `edit_file`, `apply_patch`, `manage_bg_jobs`) count as untrusted too, so a Creator run paused at its very first command. 56 tools count as outside-untrusted (web, email, other models, APIs), and 15 as trusted system tools.
  - **Decision: keep the lock (option 1 of 3).** `/api/creator/start` takes `approve_untrusted: true`, which is the pause's `approve_job` given up front, for unattended runs. It's off by default. Without it, a run pauses once at its first gated action, and one `approve_job` covers the rest. Protected paths and the secret switch apply either way.
  - **Also found (the `/var/www/html` run):** a run can be locked before any tool runs. The loop adds skills, integration descriptions, MCP tool descriptions and memories to the prompt as untrusted context (they're user-editable or come from outside), and that turns the lock on. Left as is on purpose: changing it would weaken chat too, and a Creator run would lock one step later anyway, after its first command's output. It still pauses once either way.
  - Rejected: trusting local results while gating only outside content (bash can `curl`/`git clone`, so outside text would get in as "local"), and turning the lock off for Creator (nothing would then stand between what the agent reads and what it does, which matters once Phases 5 and 6 exist).
- [x] Creator mode needs this fixed, since a run can't stop to wait for a second message. Done with the above.

## Suggested build order
Phase 0, then the "all tools in one turn" TODO (Creator depends on it), then Phases 3, 1, 2, 4, 7, 6, 5, 8. The safety net comes before anything powerful (7 moved ahead of 6 on 2026-10-01: see Phase 7).

## Working on the fork
- Do this on a new branch, for example `creator-mode`, branched from `dev`. Keep `anthropic-model-fix` as it is.
- The Creator code is mostly new files, so merging upstream updates should rarely clash.
- After each phase: commit, push, rebuild with `sudo docker compose up -d --build`, test.
