"""Client for the Creator root helper (Phases 5b–5d of docs/creator-plan.md).

The root helper runs on the host as root (host_helper/root_helper.py) and
holds the root switch: whether root is on, and until when. Odysseus asks it
for the status, passes on your authenticator code to switch root on, and asks
it to revoke. The code is checked by the helper against a key Odysseus never
sees. It also holds the watchdog (5c), which judges each root command, and
the watchdog's settings, which only it writes. And it runs root commands
(5d, the run_as_root tool): it judges each one again itself, so nothing here
can make a command automatic or un-refuse it; Odysseus only says whether you
approved an approval-tier command.

The socket path is fixed (env CREATOR_ROOT_SOCKET overrides it), not an admin
setting, like the host helper's.
"""

import json
import os
import socket
from typing import Optional

from src.creator_host_helper import HelperError, format_run_result, request

DEFAULT_SOCKET = "/app/root-helper/root.sock"
# The watchdog's longest root command is at most an hour; the helper answers
# once the command ends.
_RUN_REPLY_TIMEOUT_S = 3600 + 60
# The approval gate asks the helper synchronously; it answers in milliseconds.
_CHECK_TIMEOUT_S = 3.0


def socket_path() -> str:
    return os.environ.get("CREATOR_ROOT_SOCKET") or DEFAULT_SOCKET


def installed(path: Optional[str] = None) -> bool:
    """Whether the root helper's folder is mounted into the container at all.
    Without it, the Creator window shows no root control."""
    return os.path.isdir(os.path.dirname(path or socket_path()) or ".")


def missing_socket_message(path: str) -> str:
    folder = os.path.dirname(path) or "."
    if not os.path.isdir(folder):
        return (f"The root helper's folder isn't mounted into the container ({folder} doesn't exist): "
                "add docker/creator-root-helper.yml to COMPOSE_FILE in .env and rebuild.")
    if not os.access(folder, os.X_OK):
        return (f"The root helper's folder is mounted, but this user can't enter it ({folder}). "
                "On the host: sudo setfacl -m u:1000:x /srv/creator-root")
    return (f"The root helper's folder is mounted, but there's no socket in it ({path}). Is it "
            "running on the host? Check with: systemctl status creator-root-helper")


async def ask(payload: dict, path: Optional[str] = None, reply_timeout: Optional[float] = None) -> dict:
    """One request; the helper's reply. Raises HelperError when it can't be reached."""
    path = path or socket_path()
    try:
        exists = os.path.exists(path)
    except OSError:
        exists = False
    if not exists:
        raise HelperError(missing_socket_message(path))
    return await request(payload, path=path, reply_timeout=reply_timeout)


async def status(path: Optional[str] = None) -> dict:
    """{"available": bool, "installed": bool, ...the helper's status} or
    {"available": False, "error": "..."}."""
    path = path or socket_path()
    base = {"installed": installed(path)}
    try:
        reply = await ask({"type": "status"}, path=path)
    except HelperError as e:
        return {**base, "available": False, "on": False, "error": str(e)}
    if not reply.get("ok"):
        return {**base, "available": False, "on": False,
                "error": f"The root helper refused: {reply.get('error') or 'no reason given'}"}
    reply.pop("ok", None)
    reply.pop("type", None)
    return {**base, "available": True, **reply}


async def enable(code: str, minutes: int, path: Optional[str] = None) -> dict:
    """The helper's reply to enable: {"ok": True, ...status} or {"ok": False,
    "reason", "error", ...}. Raises HelperError when it can't be reached."""
    return await ask({"type": "enable", "code": code, "minutes": int(minutes)}, path=path)


async def revoke(path: Optional[str] = None) -> dict:
    return await ask({"type": "revoke"}, path=path)


async def check(command: str, path: Optional[str] = None) -> dict:
    """The watchdog's verdict on a command: {"ok": True, "tier", "reason", ...}.
    Runs nothing."""
    return await ask({"type": "check", "command": command}, path=path)


async def watchdog(path: Optional[str] = None) -> dict:
    """The watchdog's settings, its built-in refused list and its limits."""
    return await ask({"type": "watchdog"}, path=path)


async def save_watchdog(settings: dict, code: Optional[str] = None, path: Optional[str] = None) -> dict:
    """New settings. The helper decides whether they loosen the watchdog; if
    so it wants a code ({"ok": False, "reason": "code_needed", "loosens"})."""
    payload = {"type": "watchdog_save", "settings": settings}
    if code:
        payload["code"] = code
    return await ask(payload, path=path)


def check_sync(command: str, path: Optional[str] = None, timeout: float = _CHECK_TIMEOUT_S) -> dict:
    """check, blocking: for Creator's approval gate, which is synchronous.
    Adds "root_on" from a status request on the same occasion. Raises
    HelperError when the helper can't be reached."""
    path = path or socket_path()

    def one(payload: dict) -> dict:
        if not os.path.exists(path):
            raise HelperError(missing_socket_message(path))
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(path)
                s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                line = s.makefile("rb").readline(1024 * 1024)
        except OSError as e:
            raise HelperError(f"Couldn't reach the root helper: {e.strerror or e}")
        try:
            reply = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HelperError("The root helper's answer wasn't JSON.")
        if not isinstance(reply, dict):
            raise HelperError("The root helper's answer wasn't a JSON object.")
        return reply

    verdict = one({"type": "check", "command": command})
    if verdict.get("ok"):
        verdict["root_on"] = bool(one({"type": "status"}).get("on"))
    return verdict


async def run(command: str, approved: bool = False, redact=None, path: Optional[str] = None) -> dict:
    """Run one root command. The helper's reply: {"ok": True, "tier",
    "exit_code", "stdout", ...} or {"ok": False, "reason", "error"}. Raises
    HelperError when it can't be reached. Cancelling closes the connection,
    which stops the command (Creator's Stop)."""
    payload = {"type": "run", "command": command, "approved": bool(approved)}
    values = [v for v in (redact or []) if isinstance(v, str) and len(v) >= 4]
    if values:
        payload["redact"] = values[:200]
    return await ask(payload, path=path, reply_timeout=_RUN_REPLY_TIMEOUT_S)


async def denied(command: str, path: Optional[str] = None) -> dict:
    """Tell the helper you denied a root command (3 in a row switch root off)."""
    return await ask({"type": "denied", "command": command}, path=path)


# ---------------------------------------------------------------------------
# The run_as_root agent tool (Creator mode only)
# ---------------------------------------------------------------------------

RUN_AS_ROOT_TOOL = "run_as_root"


def parse_run_as_root_args(content) -> str:
    """The command from {"command": ...} or a bare command string. Raises
    ValueError with a message for the model."""
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
        raise ValueError('Missing command. Use {"command": "<command>"}.')
    return command.strip()


def format_root_result(reply: dict) -> dict:
    """The helper's run reply as a tool result, with how it was allowed."""
    result = format_run_result(reply)
    how = "approved by the user" if reply.get("tier") == "approval" else "automatic"
    result["output"] = result["output"].replace("[host_exec: ", "[run_as_root: ")
    result["output"] += f"\n[run_as_root: ran as root ({how})]"
    result["root"] = "approved" if reply.get("tier") == "approval" else "automatic"
    return result


async def do_run_as_root(content, owner: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """Run a command on the HOST as root, through the root helper. Only inside
    a running Creator job; the job's gate has paused for anything that isn't
    automatic before this is reached, and the helper judges it again."""
    try:
        command = parse_run_as_root_args(content)
    except ValueError as e:
        return {"error": str(e), "exit_code": 1}
    from src.creator_mode import get_active_manager
    manager = get_active_manager()
    if manager is None:
        return {"error": "run_as_root only works inside a running Creator job.", "exit_code": 1}
    return await manager.run_as_root(session_id, owner, command)
