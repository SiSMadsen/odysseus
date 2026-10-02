#!/usr/bin/env python3
"""Creator host helper (Phase 6 of docs/creator-plan.md).

Runs on the HOST, outside Docker, as the unprivileged `creator` user (see
creator-helper.service and README.md in this folder). Odysseus reaches it
through one Unix socket file, bind-mounted read-only into the container.

Requests (one line of JSON in, one line of JSON out, one request per
connection):
- {"type": "hello"}: who and what the helper is. (6a)
- {"type": "run", "command": "...", "timeout_s": 120, "redact": ["..."]}:
  run one shell command as `creator`. (6b) It runs with /bin/bash -c in the
  work folder, with a clean environment and no stdin, in its own process
  group. That group is killed when the command ends (nothing is left running),
  at the time limit, or when the client disconnects (so Creator's Stop also
  stops the host command). One command at a time. `redact` lists values
  (secrets) to blank in the audit log; they don't change what runs.

What `creator` may do on the host is the real limit, not this program: see
README.md ("What creator can do").

Protections here:
- The helper listens on nothing but its Unix socket.
- Peer check: on every connection the kernel tells us the connecting
  process's uid (SO_PEERCRED, which the client can't fake). Anyone not in
  --allow-uid gets one refusal line and is disconnected before anything they
  sent is read.
- One request per connection, with a read timeout and a size limit; commands
  have a time limit and an output limit.
- Every connection is written to an audit log (JSONL, mode 0600), secrets
  blanked.

Standard library only: the host Python has no pip.
"""

import argparse
import asyncio
import json
import os
import pwd
import signal
import socket
import stat
import struct
import sys
import time
from datetime import datetime, timezone

VERSION = 2
# Request types this helper answers.
CAPABILITIES = ("hello", "run")

DEFAULT_SOCKET = "/srv/creator-helper/helper.sock"
DEFAULT_AUDIT_LOG = "/var/log/creator-helper/audit.jsonl"
DEFAULT_WORKDIR = "/srv/creator-helper/work"
DEFAULT_HOME = "/srv/creator-helper/home"
DEFAULT_MAX_REQUEST_BYTES = 256 * 1024
DEFAULT_READ_TIMEOUT_S = 10.0
DEFAULT_RUN_TIMEOUT_S = 120
DEFAULT_MAX_RUN_TIMEOUT_S = 600
# Per stream (stdout, stderr). The rest is read and thrown away, so a chatty
# command can't block on a full pipe.
DEFAULT_OUTPUT_CAP_BYTES = 256 * 1024
MAX_COMMAND_CHARS = 100_000
MAX_REDACT_VALUES = 200
AUDIT_PREVIEW_CHARS = 2000
# After SIGTERM to a command's process group, how long before SIGKILL.
KILL_GRACE_S = 2.0
# The socket's own mode. Write permission on a socket is what lets a process
# connect, so file permissions alone can't single out the container's uid
# without sharing a group with it; SO_PEERCRED is the check that decides.
# See README.md ("Who can connect").
DEFAULT_SOCKET_MODE = 0o666

RUN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def peer_credentials(sock: socket.socket):
    """(pid, uid, gid) of the process on the other end, from the kernel."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def blank(text, values) -> str:
    """`text` with every value in `values` replaced by [REDACTED], longest first
    (so a value containing another is blanked whole)."""
    if not isinstance(text, str) or not values:
        return text
    for v in sorted(values, key=len, reverse=True):
        text = text.replace(v, "[REDACTED]")
    return text


class AuditLog:
    """Append-only JSONL, created 0600. A failed write never stops the helper."""

    def __init__(self, path: str):
        self.path = path

    def write(self, entry: dict) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, mode=0o700, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": _now_iso(), **entry}, default=str) + "\n")
        except OSError as e:
            print(f"creator-helper: audit log write failed: {e}", file=sys.stderr)


class _Capped:
    """Reads a stream to the end, keeping only the first `cap` bytes."""

    def __init__(self, cap: int):
        self.cap = cap
        self.kept = bytearray()
        self.total = 0

    async def drain(self, stream) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            self.total += len(chunk)
            room = self.cap - len(self.kept)
            if room > 0:
                self.kept += chunk[:room]

    def text(self) -> str:
        return self.kept.decode("utf-8", errors="replace")


class Helper:
    def __init__(self, socket_path: str, allow_uids, audit: AuditLog,
                 max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
                 read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
                 socket_mode: int = DEFAULT_SOCKET_MODE,
                 workdir: str = DEFAULT_WORKDIR,
                 home: str = DEFAULT_HOME,
                 max_run_timeout_s: int = DEFAULT_MAX_RUN_TIMEOUT_S,
                 output_cap_bytes: int = DEFAULT_OUTPUT_CAP_BYTES):
        if not allow_uids:
            raise ValueError("refusing to start with no --allow-uid: nobody could use it")
        self.socket_path = socket_path
        self.allow_uids = frozenset(int(u) for u in allow_uids)
        self.audit = audit
        self.max_request_bytes = int(max_request_bytes)
        self.read_timeout_s = float(read_timeout_s)
        self.socket_mode = socket_mode
        self.workdir = workdir
        self.home = home
        self.max_run_timeout_s = int(max_run_timeout_s)
        self.output_cap_bytes = int(output_cap_bytes)
        self.server = None
        self.started = time.time()
        self._run_lock = asyncio.Lock()
        try:
            self.user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            self.user = str(os.getuid())

    # -- hello ------------------------------------------------------------

    def work_usage(self, max_entries: int = 200_000) -> dict:
        """How much the work folder holds (bytes, files), for Settings. Stops
        counting after `max_entries` so a huge folder can't stall the helper."""
        total, files, partial = 0, 0, False
        for root, dirs, names in os.walk(self.workdir):
            for name in names:
                files += 1
                if files > max_entries:
                    partial = True
                    break
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
            if partial:
                break
        return {"bytes": total, "files": min(files, max_entries), "partial": partial}

    def hello(self) -> dict:
        return {
            "ok": True,
            "type": "hello",
            "helper": "creator-helper",
            "version": VERSION,
            "capabilities": list(CAPABILITIES),
            "user": self.user,
            "uid": os.getuid(),
            "uptime_s": int(time.time() - self.started),
            "workdir": self.workdir,
            "work_usage": self.work_usage(),
        }

    # -- run --------------------------------------------------------------

    def _run_env(self) -> dict:
        return {
            "PATH": RUN_PATH,
            "HOME": self.home,
            "USER": self.user,
            "LOGNAME": self.user,
            "SHELL": "/bin/bash",
            "LANG": "C.UTF-8",
            "TERM": "dumb",
        }

    @staticmethod
    def parse_run(request: dict, max_timeout_s: int):
        """(command, timeout_s, redact) or raise ValueError with the reason."""
        command = request.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("run needs a non-empty \"command\" string")
        if len(command) > MAX_COMMAND_CHARS:
            raise ValueError(f"command is longer than {MAX_COMMAND_CHARS} characters")
        if "\x00" in command:
            raise ValueError("command contains a NUL byte")
        timeout = request.get("timeout_s", DEFAULT_RUN_TIMEOUT_S)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout_s must be a number")
        timeout = max(1, min(int(timeout), max_timeout_s))
        redact = request.get("redact") or []
        if not isinstance(redact, list) or len(redact) > MAX_REDACT_VALUES:
            raise ValueError(f"redact must be a list of at most {MAX_REDACT_VALUES} strings")
        redact = [v for v in redact if isinstance(v, str) and len(v) >= 4]
        return command, timeout, redact

    @staticmethod
    def _killpg(pgid: int, sig) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    async def _exited(proc) -> None:
        """Until the shell itself exits. Not proc.wait(): that also waits for the
        output pipes to close, which a background job it started keeps open."""
        while proc.returncode is None:
            await asyncio.sleep(0.05)

    async def _stop_group(self, proc) -> None:
        """TERM the command's process group, then KILL what's left."""
        self._killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self._exited(proc), timeout=KILL_GRACE_S)
        except asyncio.TimeoutError:
            pass
        self._killpg(proc.pid, signal.SIGKILL)

    async def run(self, command: str, timeout_s: int, reader) -> dict:
        """Run one command. `reader` is the client's stream: EOF on it means the
        client went away (Creator's Stop), which stops the command."""
        for d in (self.workdir, self.home):
            os.makedirs(d, mode=0o700, exist_ok=True)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command,
            cwd=self.workdir, env=self._run_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,   # its own process group (pgid == pid)
            umask=0o022,
        )
        out, err = _Capped(self.output_cap_bytes), _Capped(self.output_cap_bytes)
        readers = [asyncio.ensure_future(out.drain(proc.stdout)),
                   asyncio.ensure_future(err.drain(proc.stderr))]
        exited = asyncio.ensure_future(self._exited(proc))
        gone = asyncio.ensure_future(reader.read(1))   # b"" once the client closes
        timed_out = disconnected = False
        try:
            done, _ = await asyncio.wait({exited, gone}, timeout=timeout_s,
                                         return_when=asyncio.FIRST_COMPLETED)
            if exited not in done:
                if gone in done:
                    disconnected = True
                else:
                    timed_out = True
                await self._stop_group(proc)
        finally:
            gone.cancel()
            exited.cancel()
            # Whatever the command left behind in its group goes too, even after
            # a normal exit (a background job it started, say).
            self._killpg(proc.pid, signal.SIGKILL)
            # A process that left the group (setsid) could still hold the pipes
            # open; don't wait for it forever.
            try:
                await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
            except asyncio.TimeoutError:
                pass
            await asyncio.wait(readers, timeout=KILL_GRACE_S)
            for r in readers:
                r.cancel()
        return {
            "ok": True,
            "type": "run",
            "exit_code": proc.returncode,
            "stdout": out.text(),
            "stderr": err.text(),
            "stdout_bytes": out.total,
            "stderr_bytes": err.total,
            "truncated": out.total > out.cap or err.total > err.cap,
            "duration_s": round(time.monotonic() - started, 3),
            "timed_out": timed_out,
            "disconnected": disconnected,
        }

    # -- connections ------------------------------------------------------

    async def _send(self, writer, reply: dict) -> None:
        writer.write((json.dumps(reply) + "\n").encode("utf-8"))
        try:
            await asyncio.wait_for(writer.drain(), timeout=self.read_timeout_s)
        except (asyncio.TimeoutError, ConnectionError):
            pass

    async def on_connection(self, reader, writer) -> None:
        sock = writer.get_extra_info("socket")
        entry = {"type": "connection"}
        try:
            try:
                pid, uid, gid = peer_credentials(sock)
            except OSError as e:
                entry.update(ok=False, error=f"no peer credentials: {e}")
                return
            entry.update(peer_pid=pid, peer_uid=uid, peer_gid=gid)
            if uid not in self.allow_uids:
                # Refused before anything the peer sent is read.
                entry.update(ok=False, error="peer uid not allowed")
                await self._send(writer, {"ok": False, "error": "not allowed"})
                return

            try:
                line = await asyncio.wait_for(reader.readline(), timeout=self.read_timeout_s)
            except asyncio.TimeoutError:
                entry.update(ok=False, error="read timeout")
                await self._send(writer, {"ok": False, "error": "read timeout"})
                return
            except (ValueError, asyncio.LimitOverrunError):
                entry.update(ok=False, error="request too large")
                await self._send(writer, {"ok": False, "error": "request too large"})
                return
            if len(line) > self.max_request_bytes:
                entry.update(ok=False, error="request too large")
                await self._send(writer, {"ok": False, "error": "request too large"})
                return
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                entry.update(ok=False, error="bad JSON")
                await self._send(writer, {"ok": False, "error": "request must be one line of JSON"})
                return
            if not isinstance(request, dict):
                entry.update(ok=False, error="not an object")
                await self._send(writer, {"ok": False, "error": "request must be a JSON object"})
                return

            rtype = request.get("type")
            entry["request_type"] = str(rtype)[:40]
            if rtype == "hello":
                reply = self.hello()
            elif rtype == "run":
                reply = await self._handle_run(request, reader, entry)
            else:
                reply = {"ok": False, "error": f"unknown request type: {str(rtype)[:40]!r}",
                         "capabilities": list(CAPABILITIES)}
            entry.update(ok=reply.get("ok"), error=reply.get("error"))
            await self._send(writer, reply)
        finally:
            self.audit.write(entry)
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _handle_run(self, request: dict, reader, entry: dict) -> dict:
        try:
            command, timeout_s, redact = self.parse_run(request, self.max_run_timeout_s)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        entry.update(command=blank(command, redact), timeout_s=timeout_s)
        if self._run_lock.locked():
            return {"ok": False, "error": "busy: another command is running"}
        async with self._run_lock:
            try:
                reply = await self.run(command, timeout_s, reader)
            except OSError as e:
                return {"ok": False, "error": f"could not start the command: {e.strerror or e}"}
        entry.update(
            exit_code=reply["exit_code"], duration_s=reply["duration_s"],
            timed_out=reply["timed_out"], disconnected=reply["disconnected"],
            stdout_bytes=reply["stdout_bytes"], stderr_bytes=reply["stderr_bytes"],
            stdout_preview=blank(reply["stdout"][:AUDIT_PREVIEW_CHARS], redact),
            stderr_preview=blank(reply["stderr"][:AUDIT_PREVIEW_CHARS], redact),
        )
        return reply

    # -- lifecycle --------------------------------------------------------

    def _remove_stale_socket(self) -> None:
        try:
            st = os.lstat(self.socket_path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(st.st_mode):
            raise RuntimeError(f"{self.socket_path} exists and is not a socket; not touching it")
        os.unlink(self.socket_path)

    async def start(self) -> None:
        self._remove_stale_socket()
        old_umask = os.umask(0o177)   # no window where the socket is open to others
        try:
            self.server = await asyncio.start_unix_server(
                self.on_connection, path=self.socket_path, limit=self.max_request_bytes + 1)
        finally:
            os.umask(old_umask)
        os.chmod(self.socket_path, self.socket_mode)
        self.audit.write({"type": "start", "socket": self.socket_path, "user": self.user,
                          "allow_uids": sorted(self.allow_uids), "version": VERSION,
                          "capabilities": list(CAPABILITIES), "workdir": self.workdir})

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self.audit.write({"type": "stop"})


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Creator host helper.")
    p.add_argument("--socket", default=DEFAULT_SOCKET, help=f"socket path (default {DEFAULT_SOCKET})")
    p.add_argument("--allow-uid", type=int, action="append", default=[], required=True,
                   help="uid allowed to connect (the container's user, normally 1000); repeatable")
    p.add_argument("--audit-log", default=DEFAULT_AUDIT_LOG, help=f"audit log (default {DEFAULT_AUDIT_LOG})")
    p.add_argument("--workdir", default=DEFAULT_WORKDIR, help=f"where commands run (default {DEFAULT_WORKDIR})")
    p.add_argument("--home", default=DEFAULT_HOME, help=f"HOME for commands (default {DEFAULT_HOME})")
    p.add_argument("--max-run-timeout", type=int, default=DEFAULT_MAX_RUN_TIMEOUT_S,
                   help=f"longest a command may run, seconds (default {DEFAULT_MAX_RUN_TIMEOUT_S})")
    p.add_argument("--max-request-bytes", type=int, default=DEFAULT_MAX_REQUEST_BYTES)
    p.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT_S)
    return p.parse_args(argv)


async def _main(args) -> None:
    if os.getuid() == 0:
        raise SystemExit("creator-helper must not run as root (use the `creator` user)")
    helper = Helper(args.socket, args.allow_uid, AuditLog(args.audit_log),
                    max_request_bytes=args.max_request_bytes, read_timeout_s=args.read_timeout,
                    workdir=args.workdir, home=args.home, max_run_timeout_s=args.max_run_timeout)
    await helper.start()
    print(f"creator-helper {VERSION}: listening on {args.socket} for uid(s) {sorted(helper.allow_uids)}",
          file=sys.stderr, flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await helper.stop()


if __name__ == "__main__":
    asyncio.run(_main(_parse_args(sys.argv[1:])))
