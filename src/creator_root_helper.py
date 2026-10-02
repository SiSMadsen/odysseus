"""Client for the Creator root helper (Phase 5b of docs/creator-plan.md).

The root helper runs on the host as root (host_helper/root_helper.py) and
holds the root switch: whether root is on, and until when. Odysseus asks it
for the status, passes on your authenticator code to switch root on, and asks
it to revoke. The code is checked by the helper against a key Odysseus never
sees. In 5b nothing runs as root yet.

The socket path is fixed (env CREATOR_ROOT_SOCKET overrides it), not an admin
setting, like the host helper's.
"""

import os
from typing import Optional

from src.creator_host_helper import HelperError, request

DEFAULT_SOCKET = "/app/root-helper/root.sock"


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


async def ask(payload: dict, path: Optional[str] = None) -> dict:
    """One request; the helper's reply. Raises HelperError when it can't be reached."""
    path = path or socket_path()
    try:
        exists = os.path.exists(path)
    except OSError:
        exists = False
    if not exists:
        raise HelperError(missing_socket_message(path))
    return await request(payload, path=path)


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
