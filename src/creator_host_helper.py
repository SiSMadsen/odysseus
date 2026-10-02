"""Client for the Creator host helper (Phase 6 of docs/creator-plan.md).

The helper runs on the host (host_helper/creator_helper.py) and listens on a
Unix socket that docker/creator-helper.yml mounts into the container.
Requests: `hello` (the connection test in Settings) and `run` (one command
as the host's `creator` user; the host_exec tool). One request per
connection. Closing the connection while a command runs stops it on the host:
that's how Creator's Stop reaches host commands.

The socket path is fixed (env CREATOR_HELPER_SOCKET overrides it), not an
admin setting: nothing should be able to point Creator at another socket.
"""

import asyncio
import json
import os
from typing import Optional

DEFAULT_SOCKET = "/app/host-helper/helper.sock"
_TIMEOUT_S = 5.0
# A run reply carries up to 256 KB of stdout and of stderr, JSON-escaped.
_MAX_REPLY_BYTES = 8 * 1024 * 1024
# How long past a command's own time limit to wait for the helper's answer
# (it kills the command at the limit, with a short grace period).
_RUN_REPLY_GRACE_S = 15.0


class HelperError(Exception):
    """The helper couldn't be reached or gave no usable answer."""


def socket_path() -> str:
    return os.environ.get("CREATOR_HELPER_SOCKET") or DEFAULT_SOCKET


def missing_socket_message(path: str) -> str:
    """Why there's no socket, and the command that checks it. No folder means
    the compose overlay isn't mounting it; a folder without the socket means
    the helper isn't running on the host."""
    folder = os.path.dirname(path) or "."
    if not os.path.isdir(folder):
        return (f"The helper's folder isn't mounted into the container ({folder} doesn't exist), "
                "so docker/creator-helper.yml isn't enabled. On the host, in the odysseus folder, "
                "`docker compose config | grep host-helper` should print \"target: /app/host-helper\"; if it prints "
                "nothing, add COMPOSE_FILE=docker-compose.yml:docker/creator-helper.yml to .env and "
                "rebuild (sudo docker compose up -d --build).")
    return (f"The helper's folder is mounted, but there's no socket in it ({path}). Is the helper "
            "running on the host? Check with: systemctl status creator-helper")


async def request(payload: dict, path: Optional[str] = None, timeout: float = _TIMEOUT_S,
                  reply_timeout: Optional[float] = None) -> dict:
    """Send one request, return the helper's one-line JSON reply. `timeout`
    covers connecting and sending; `reply_timeout` (default: the same) the wait
    for the answer. Cancelling this coroutine closes the connection."""
    path = path or socket_path()
    if not os.path.exists(path):
        raise HelperError(missing_socket_message(path))
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path, limit=_MAX_REPLY_BYTES + 1), timeout=timeout)
    except asyncio.TimeoutError:
        raise HelperError("Timed out connecting to the helper.")
    except OSError as e:
        raise HelperError(f"Couldn't connect to the helper: {e.strerror or e}")
    try:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=reply_timeout or timeout)
        except asyncio.TimeoutError:
            raise HelperError("The helper didn't answer in time.")
        except (ValueError, asyncio.LimitOverrunError):
            raise HelperError("The helper's answer was too large.")
    except OSError as e:
        raise HelperError(f"Connection to the helper failed: {e.strerror or e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    if not line:
        raise HelperError("The helper closed the connection without answering.")
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HelperError("The helper's answer wasn't JSON.")
    if not isinstance(reply, dict):
        raise HelperError("The helper's answer wasn't a JSON object.")
    return reply


async def hello(path: Optional[str] = None) -> dict:
    """The connection test: {"ok": True, "reply": {...}} or {"ok": False, "error": "..."}."""
    try:
        reply = await request({"type": "hello"}, path=path)
    except HelperError as e:
        return {"ok": False, "error": str(e), "socket": path or socket_path()}
    if not reply.get("ok"):
        return {"ok": False, "error": f"The helper refused: {reply.get('error') or 'no reason given'}",
                "socket": path or socket_path()}
    return {"ok": True, "reply": reply, "socket": path or socket_path()}


async def run(command: str, timeout_s: int = 120, redact=None, path: Optional[str] = None) -> dict:
    """Run one command on the host as `creator`. Returns the helper's reply:
    {"ok": True, "exit_code", "stdout", "stderr", "timed_out", ...} or
    {"ok": False, "error"}. Raises HelperError when the helper can't be reached."""
    payload = {"type": "run", "command": command, "timeout_s": int(timeout_s)}
    values = [v for v in (redact or []) if isinstance(v, str) and len(v) >= 4]
    if values:
        payload["redact"] = values[:200]
    return await request(payload, path=path, reply_timeout=float(timeout_s) + _RUN_REPLY_GRACE_S)


# ---------------------------------------------------------------------------
# The host_exec agent tool (Creator mode only)
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_TIMEOUT_S = 120
_MAX_TOOL_TIMEOUT_S = 600


def parse_host_exec_args(content):
    """(command, timeout_s) from {"command": ..., "timeout_s": ...} or a bare
    command string. Raises ValueError with a message for the model."""
    timeout = _DEFAULT_TOOL_TIMEOUT_S
    if isinstance(content, dict):
        args = content
    else:
        raw = (content or "").strip()
        args = None
        if raw.startswith("{"):
            try:
                args = json.loads(raw)
            except ValueError:
                args = None
        if not isinstance(args, dict):
            args = {"command": raw}
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError('Missing command. Use {"command": "<shell command>", "timeout_s": 120}.')
    t = args.get("timeout_s", timeout)
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        timeout = max(1, min(int(t), _MAX_TOOL_TIMEOUT_S))
    return command, timeout


def format_run_result(reply: dict) -> dict:
    """The helper's run reply as a tool result: {"output", "exit_code"}."""
    parts = []
    if reply.get("stdout"):
        parts.append(reply["stdout"].rstrip("\n"))
    if reply.get("stderr"):
        parts.append("[stderr]\n" + reply["stderr"].rstrip("\n"))
    notes = []
    if reply.get("timed_out"):
        notes.append(f"stopped at the time limit after {reply.get('duration_s')} s")
    if reply.get("truncated"):
        notes.append(f"output cut (stdout {reply.get('stdout_bytes')} bytes, stderr {reply.get('stderr_bytes')} bytes)")
    if notes:
        parts.append("[host_exec: " + "; ".join(notes) + "]")
    code = reply.get("exit_code")
    if reply.get("timed_out") and (code is None or code == 0):
        code = 124
    return {"output": "\n".join(parts) or "(no output)", "exit_code": code if code is not None else 1}


async def do_host_exec(content, owner: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """Run a command on the HOST (outside the container) as the `creator` user,
    through the host helper. Only inside a running Creator job; the job's
    approval gate decides before this is reached."""
    try:
        command, timeout_s = parse_host_exec_args(content)
    except ValueError as e:
        return {"error": str(e), "exit_code": 1}
    from src.creator_mode import get_active_manager
    manager = get_active_manager()
    if manager is None:
        return {"error": "host_exec only works inside a running Creator job.", "exit_code": 1}
    return await manager.run_on_host(session_id, owner, command, timeout_s)
