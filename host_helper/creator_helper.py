#!/usr/bin/env python3
"""Creator host helper (Phase 6a of docs/creator-plan.md): hello only.

Runs on the HOST, outside Docker, as the unprivileged `creator` user (see
creator-helper.service and README.md in this folder). Odysseus reaches it
through one Unix socket file, bind-mounted read-only into the container.

What it does in 6a: answers `hello`. Nothing else. There is no request type
that runs a command, reads a file or changes anything; `run` is Phase 6b.

Protections:
- No network: it only ever opens a Unix socket (and the systemd unit adds
  PrivateNetwork=yes and RestrictAddressFamilies=AF_UNIX on top).
- Peer check: on every connection the kernel tells us the connecting
  process's uid (SO_PEERCRED, which the client can't fake). Anyone not in
  --allow-uid gets one refusal line and is disconnected before anything they
  sent is read.
- One small request per connection, with a read timeout and a size limit.
- Every connection is written to an audit log (JSONL, mode 0600).

Protocol: the client sends one line of JSON, e.g. {"type": "hello"}, and gets
one line of JSON back: {"ok": true, ...} or {"ok": false, "error": "..."}.

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

VERSION = 1
# Request types this helper answers. 6b adds "run".
CAPABILITIES = ("hello",)

DEFAULT_SOCKET = "/srv/creator-helper/helper.sock"
DEFAULT_AUDIT_LOG = "/var/log/creator-helper/audit.jsonl"
DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_READ_TIMEOUT_S = 10.0
# The socket's own mode. Write permission on a socket is what lets a process
# connect, so file permissions alone can't single out the container's uid
# without sharing a group with it; SO_PEERCRED is the check that decides.
# See README.md ("Who can connect").
DEFAULT_SOCKET_MODE = 0o666


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def peer_credentials(sock: socket.socket):
    """(pid, uid, gid) of the process on the other end, from the kernel."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


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


class Helper:
    def __init__(self, socket_path: str, allow_uids, audit: AuditLog,
                 max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
                 read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
                 socket_mode: int = DEFAULT_SOCKET_MODE):
        if not allow_uids:
            raise ValueError("refusing to start with no --allow-uid: nobody could use it")
        self.socket_path = socket_path
        self.allow_uids = frozenset(int(u) for u in allow_uids)
        self.audit = audit
        self.max_request_bytes = int(max_request_bytes)
        self.read_timeout_s = float(read_timeout_s)
        self.socket_mode = socket_mode
        self.server = None
        self.started = time.time()
        try:
            self.user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            self.user = str(os.getuid())

    # -- requests ---------------------------------------------------------

    def handle_request(self, request) -> dict:
        """The reply to one parsed request. Pure: no I/O."""
        if not isinstance(request, dict):
            return {"ok": False, "error": "request must be a JSON object"}
        rtype = request.get("type")
        if rtype == "hello":
            return {
                "ok": True,
                "type": "hello",
                "helper": "creator-helper",
                "version": VERSION,
                "capabilities": list(CAPABILITIES),
                "user": self.user,
                "uid": os.getuid(),
                "uptime_s": int(time.time() - self.started),
            }
        return {"ok": False, "error": f"unknown request type: {str(rtype)[:40]!r}",
                "capabilities": list(CAPABILITIES)}

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

            reply = self.handle_request(request)
            entry.update(request_type=str(request.get("type"))[:40] if isinstance(request, dict) else None,
                         ok=reply.get("ok"), error=reply.get("error"))
            await self._send(writer, reply)
        finally:
            self.audit.write(entry)
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

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
                          "capabilities": list(CAPABILITIES)})

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
    p = argparse.ArgumentParser(description="Creator host helper (hello only).")
    p.add_argument("--socket", default=DEFAULT_SOCKET, help=f"socket path (default {DEFAULT_SOCKET})")
    p.add_argument("--allow-uid", type=int, action="append", default=[], required=True,
                   help="uid allowed to connect (the container's user, normally 1000); repeatable")
    p.add_argument("--audit-log", default=DEFAULT_AUDIT_LOG, help=f"audit log (default {DEFAULT_AUDIT_LOG})")
    p.add_argument("--max-request-bytes", type=int, default=DEFAULT_MAX_REQUEST_BYTES)
    p.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT_S)
    return p.parse_args(argv)


async def _main(args) -> None:
    if os.getuid() == 0:
        raise SystemExit("creator-helper must not run as root (use the `creator` user)")
    helper = Helper(args.socket, args.allow_uid, AuditLog(args.audit_log),
                    max_request_bytes=args.max_request_bytes, read_timeout_s=args.read_timeout)
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
