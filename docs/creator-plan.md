# Creator Mode: Programming Plan

Status: DRAFT. Based on reading the fork's code (deep research, agent loop, secret storage). Nothing has been built yet. Host access is decided: a helper program on the host, reached through a socket file (Phase 0, item 1, and Phase 6).
Items marked **[CHECK]** are things I have not yet looked at, so I can't promise how big they are.

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

## Phase 2: The "never give up" behaviour
- [ ] Creator-specific instructions: "Try another approach when something fails. List what you tried. Ask the user only when blocked on something only they can give you, like a missing credential or a decision."
- [ ] Failure tracking: if the same command fails the same way 3 times, force a different approach.
- [ ] Progress notes written regularly, so a long run can't lose its place (and so the report is accurate).
- [ ] Final report: what was asked, what was done, what worked, what didn't, what's left, and the exact commands run.
- [ ] A genuine "I'm blocked" exit that pauses the job and notifies you, instead of ending it.

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
- [ ] **[CHECK]** Re-read the compose file's shared `data` folder setup, and test that a socket file works across that mount. Sockets usually work over a bind mount on Linux, but this is not confirmed. First build a tiny "hello" helper that only answers over the socket, before any real command-running goes in.
- [ ] On the host (you do this): create a dedicated user for the helper, for example `creator`, with limited sudo rights. The helper runs as this user, not your own account.
- [ ] Helper service on the host, started outside Docker. It listens on a socket file inside the shared folder, for example `/home/madsen/odysseus/data/host-helper/helper.sock`. Only the container's user may use that file (strict permissions).
- [ ] Narrow requests only: `run(command)` first, `run_as_root(command)` later. Every request has a time limit and returns the output.
- [ ] Tool `host_exec(command)` in Odysseus: sends a `run` request to the helper. `run_as_root` (Phase 5) goes through the same socket.
- [ ] Decide where the root switch is checked. Option 1: Odysseus checks it and passes the password to the helper over the socket (simpler). Option 2: the helper reads the switch itself (stronger, because a compromised app can't get around it). Not decided yet.
- [ ] Helper audit log: every request and result written to a file you can read, with secrets blanked out.
- [ ] Kill switch: stopping the helper cuts off all host access straight away.
- [ ] Security note: anything that can write to the socket file can run commands on your host. The file's permissions and the dedicated host user are the main protections, so build them first.
- [ ] Connection test button in settings (sends "hello" to the helper and shows the reply).

## Phase 7: UI
- [ ] **[CHECK]** Read how the research panel is built, to see how much UI code a new panel takes.
- [ ] Creator toggle next to Research.
- [ ] Creator panel: task box, start/stop, live log, report view.
- [ ] Secrets screen (from Phase 4).

## Phase 8: Testing
- [ ] Harmless first task: "list the files in a folder and write a report."
- [ ] Test a task that fails on purpose, to confirm it tries workarounds.
- [ ] Test the secret switch: switch it off and confirm `get_secret` and `run_as_root` both fail.
- [ ] Test that a secret never shows in logs, reports or chat history.
- [ ] Test the stop button and the time limit.

## Separate TODO: access to all tools within the same turn
**Problem:** I get a smaller set of tools each turn than the server has switched on. When I need a tool that isn't in the set, you have to send a second message.

- [ ] **[CHECK]** Find the code that picks which tools I get each turn. It isn't in the tools folder, so it is somewhere in the chat code.
- [ ] Find out how it decides (my guess: the chat-bar toggles and the message text). I haven't confirmed that.
- [ ] Decide on the fix. Options:
  - Give every enabled tool on every turn (simplest, but a longer prompt costs more).
  - Add a "load more tools" tool I can call mid-turn to pull in the ones I need.
  - Always include a core set (shell, files, memory, teacher) and add the rest on demand.
- [ ] Check the safety lock that blocks some tools after I read web pages or emails. Decide whether to keep it, loosen it, or leave it off in Creator mode.
- [ ] Creator mode needs this fixed, since a run can't stop to wait for a second message.

## Suggested build order
Phase 0, then the "all tools in one turn" TODO (Creator depends on it), then Phases 3, 1, 2, 4, 6, 5, 7, 8. The safety net comes before anything powerful.

## Working on the fork
- Do this on a new branch, for example `creator-mode`, branched from `dev`. Keep `anthropic-model-fix` as it is.
- The Creator code is mostly new files, so merging upstream updates should rarely clash.
- After each phase: commit, push, rebuild with `sudo docker compose up -d --build`, test.
