"""Creator host helper, Phase 6a (hello only): the helper itself
(host_helper/creator_helper.py, run here on a temporary socket), the client
in src/creator_host_helper.py, and the connection-test route."""

import asyncio
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.auth import DEFAULT_PRIVILEGES
from src import creator_host_helper as client

_HELPER_PATH = Path(__file__).resolve().parent.parent / "host_helper" / "creator_helper.py"
_spec = importlib.util.spec_from_file_location("creator_helper", _HELPER_PATH)
helper_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper_mod)

SOCK = "h.sock"   # relative: keeps the path under the 108-byte socket limit


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _helper(tmp_path, allow=None, **kw):
    return helper_mod.Helper(SOCK, allow if allow is not None else [os.getuid()],
                             helper_mod.AuditLog(str(tmp_path / "log" / "audit.jsonl")), **kw)


def _audit(tmp_path):
    return [json.loads(line) for line in (tmp_path / "log" / "audit.jsonl").read_text().splitlines()]


async def _raw(payload: bytes, wait=True):
    reader, writer = await asyncio.open_unix_connection(SOCK)
    if payload:
        writer.write(payload)
        await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    return json.loads(line)


def _run(helper, coro_fn):
    async def go():
        await helper.start()
        try:
            return await coro_fn()
        finally:
            await helper.stop()
    return asyncio.run(go())


def test_hello_answers_and_is_audited(in_tmp):
    h = _helper(in_tmp)
    out = _run(h, lambda: client.hello(path=SOCK))
    assert out["ok"] is True
    reply = out["reply"]
    assert reply["type"] == "hello" and reply["helper"] == "creator-helper"
    assert reply["capabilities"] == ["hello"]
    assert reply["uid"] == os.getuid()
    entries = _audit(in_tmp)
    assert [e["type"] for e in entries] == ["start", "connection", "stop"]
    conn = entries[1]
    assert conn["peer_uid"] == os.getuid() and conn["peer_pid"] == os.getpid()
    assert conn["request_type"] == "hello" and conn["ok"] is True
    assert stat.S_IMODE(os.stat(in_tmp / "log" / "audit.jsonl").st_mode) == 0o600
    # stop removes the socket: the kill switch leaves nothing to connect to.
    assert not os.path.exists(SOCK)


def test_socket_mode_and_peer_check_decides(in_tmp):
    h = _helper(in_tmp, allow=[os.getuid() + 4242])

    async def go():
        assert stat.S_IMODE(os.stat(SOCK).st_mode) == 0o666
        return await client.hello(path=SOCK)

    out = _run(h, go)
    assert out == {"ok": False, "error": "The helper refused: not allowed", "socket": SOCK}
    conn = _audit(in_tmp)[1]
    assert conn["ok"] is False and conn["error"] == "peer uid not allowed"
    assert "request_type" not in conn   # refused before reading the request


def test_nothing_but_hello_is_answered(in_tmp):
    h = _helper(in_tmp)
    marker = in_tmp / "ran"

    async def go():
        return await client.request({"type": "run", "command": f"touch {marker}"}, path=SOCK)

    reply = _run(h, go)
    assert reply["ok"] is False and "unknown request type" in reply["error"]
    assert reply["capabilities"] == ["hello"]
    assert not marker.exists()


def test_bad_json_oversize_and_silence_are_refused(in_tmp):
    h = _helper(in_tmp, max_request_bytes=1024, read_timeout_s=0.3)

    async def go():
        bad = await _raw(b"not json\n")
        listy = await _raw(b"[1, 2]\n")
        big = await _raw(b'{"type": "hello", "pad": "' + b"x" * 5000 + b'"}\n')
        silent = await _raw(b"")
        return bad, listy, big, silent

    bad, listy, big, silent = _run(h, go)
    assert bad == {"ok": False, "error": "request must be one line of JSON"}
    assert listy == {"ok": False, "error": "request must be a JSON object"}
    assert big == {"ok": False, "error": "request too large"}
    assert silent == {"ok": False, "error": "read timeout"}


def test_refuses_to_start_without_allowed_uids_or_over_a_non_socket(in_tmp):
    with pytest.raises(ValueError):
        _helper(in_tmp, allow=[])
    Path(SOCK).write_text("precious")
    h = _helper(in_tmp)
    with pytest.raises(RuntimeError):
        asyncio.run(h.start())
    assert Path(SOCK).read_text() == "precious"


def test_stale_socket_is_replaced(in_tmp):
    import socket as _socket
    s = _socket.socket(_socket.AF_UNIX)
    s.bind(SOCK)
    s.close()
    h = _helper(in_tmp)
    assert _run(h, lambda: client.hello(path=SOCK))["ok"] is True


def test_client_explains_a_missing_socket(in_tmp):
    out = asyncio.run(client.hello(path="nope.sock"))
    assert out["ok"] is False and "systemctl status creator-helper" in out["error"]


def test_client_default_path_and_env_override(monkeypatch):
    monkeypatch.delenv("CREATOR_HELPER_SOCKET", raising=False)
    assert client.socket_path() == "/app/host-helper/helper.sock"
    monkeypatch.setenv("CREATOR_HELPER_SOCKET", "/x/y.sock")
    assert client.socket_path() == "/x/y.sock"


def test_hello_route_is_gated_and_reaches_the_helper(in_tmp, monkeypatch):
    from routes import creator_routes
    from src.creator_mode import CreatorManager

    class _Auth:
        is_configured = True

        def get_privileges(self, user):
            return {**DEFAULT_PRIVILEGES, "can_use_creator": user == "alice"}

    def req(user):
        return SimpleNamespace(state=SimpleNamespace(current_user=user), headers={},
                               app=SimpleNamespace(state=SimpleNamespace(auth_manager=_Auth())))

    monkeypatch.setattr(creator_routes, "require_user", lambda r: r.state.current_user)
    monkeypatch.setenv("CREATOR_HELPER_SOCKET", SOCK)
    mgr = CreatorManager.__new__(CreatorManager)   # the route doesn't touch it
    router = creator_routes.setup_creator_routes(mgr)
    route = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/creator/helper/hello")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(route(request=req("bob")))
    assert exc.value.status_code == 403

    h = _helper(in_tmp)
    out = _run(h, lambda: route(request=req("alice")))
    assert out["ok"] is True and out["reply"]["capabilities"] == ["hello"]


def test_unit_file_keeps_the_helper_off_the_network_and_unprivileged():
    unit = (_HELPER_PATH.parent / "creator-helper.service").read_text()
    for line in ("User=creator", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX",
                 "NoNewPrivileges=yes", "CapabilityBoundingSet=", "ProtectSystem=strict",
                 "ReadWritePaths=/srv/creator-helper", "--allow-uid 1000"):
        assert line in unit, line
    overlay = (_HELPER_PATH.parent.parent / "docker" / "creator-helper.yml").read_text()
    assert "/srv/creator-helper:/app/host-helper:ro" in overlay
