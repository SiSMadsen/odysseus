"""Client for the Creator host helper (Phase 6a of docs/creator-plan.md).

The helper runs on the host (host_helper/creator_helper.py) and listens on a
Unix socket that docker/creator-helper.yml mounts into the container. In 6a
the only request is `hello`, used by the connection test in Settings.

The socket path is fixed (env CREATOR_HELPER_SOCKET overrides it), not an
admin setting: nothing should be able to point Creator at another socket.
"""

import asyncio
import json
import os
from typing import Optional

DEFAULT_SOCKET = "/app/host-helper/helper.sock"
_TIMEOUT_S = 5.0
_MAX_REPLY_BYTES = 64 * 1024


class HelperError(Exception):
    """The helper couldn't be reached or gave no usable answer."""


def socket_path() -> str:
    return os.environ.get("CREATOR_HELPER_SOCKET") or DEFAULT_SOCKET


async def request(payload: dict, path: Optional[str] = None, timeout: float = _TIMEOUT_S) -> dict:
    """Send one request, return the helper's one-line JSON reply."""
    path = path or socket_path()
    if not os.path.exists(path):
        raise HelperError(
            f"No helper socket at {path}. Is the helper installed and running "
            "(systemctl status creator-helper), and is docker/creator-helper.yml enabled?")
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
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
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
