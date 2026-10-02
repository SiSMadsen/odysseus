"""Creator root helper, Phase 5b: the root switch. The helper
(host_helper/root_helper.py, run here on temporary sockets with fake clocks),
the client in src/creator_root_helper.py, and the /api/creator/root routes."""

import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.auth import DEFAULT_PRIVILEGES
from src import creator_root_helper as client

_HELPER_PATH = Path(__file__).resolve().parent.parent / "host_helper" / "root_helper.py"
_spec = importlib.util.spec_from_file_location("root_helper", _HELPER_PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

KEY = b"12345678901234567890"
# Relative paths keep the sockets under the 108-byte limit.
SOCK = "s/root.sock"
CONTROL = "c/control.sock"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for d in ("s", "c", "etc"):
        (tmp_path / d).mkdir(mode=0o700)
        os.chmod(tmp_path / d, 0o700)
    key_file = tmp_path / "etc" / "totp.key"
    key_file.write_text(base64.b32encode(KEY).decode() + "\n")
    os.chmod(key_file, 0o600)
    return tmp_path


def _parts(tmp_path, clock, max_minutes=90):
    audit = mod.AuditLog(str(tmp_path / "log" / "audit.jsonl"))
    verifier = mod.TotpVerifier(str(tmp_path / "etc" / "totp.key"), str(tmp_path / "state" / "state.json"),
                                clock=clock)
    switch = mod.RootSwitch(verifier, audit, max_minutes=max_minutes, clock=clock, wall=clock)
    return switch, audit


def _helper(tmp_path, clock, allow=None, control=None):
    switch, audit = _parts(tmp_path, clock)
    me = os.getuid()
    return mod.RootHelper(switch, audit, SOCK, allow if allow is not None else [me], CONTROL,
                          control_uids=control if control is not None else [me])


def _code(clock, offset_steps=0):
    return mod.hotp(KEY, mod.time_step(clock()) + offset_steps)


def _audit_text(tmp_path):
    return (tmp_path / "log" / "audit.jsonl").read_text()


def _audit(tmp_path):
    return [json.loads(line) for line in _audit_text(tmp_path).splitlines()]


def _run(helper, coro_fn):
    async def go():
        await helper.start()
        try:
            return await coro_fn()
        finally:
            await helper.stop()
    return asyncio.run(go())


async def _raw(path, payload: dict):
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    return json.loads(line)


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("t, expected", [
    (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
    (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130"),
])
def test_totp_matches_rfc_6238_vectors(t, expected):
    assert mod.hotp(KEY, mod.time_step(t), digits=8) == expected


def test_key_text_round_trips_and_the_link_is_what_apps_read():
    text = mod.new_key_text()
    assert len(mod.parse_key(text)) == mod.KEY_BYTES
    assert mod.parse_key(" ".join(text[i:i + 4] for i in range(0, len(text), 4)).lower()) == mod.parse_key(text)
    uri = mod.otpauth_uri(text, "root@box")
    assert uri.startswith("otpauth://totp/Odysseus%20Creator:root%40box?secret=" + text)
    assert "digits=6" in uri and "period=30" in uri
    for bad in ("", "not base32!", "AAAA"):
        with pytest.raises(ValueError):
            mod.parse_key(bad)


def test_a_key_file_others_could_read_is_refused(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    assert switch.status()["totp_ready"] is True
    os.chmod(in_tmp / "etc" / "totp.key", 0o644)
    st = switch.status()
    assert st["totp_ready"] is False and "0600" in st["totp_problem"]
    out = switch.enable_with_code(_code(clock), 30)
    assert out["ok"] is False and out["reason"] == "no_key"
    (in_tmp / "etc" / "totp.key").unlink()
    assert "setup-totp" in switch.status()["totp_problem"]


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def test_right_code_turns_root_on_until_the_time_is_up(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    assert switch.status()["on"] is False
    code = _code(clock)
    out = switch.enable_with_code(code, 30)
    assert out["ok"] is True and out["on"] is True
    assert out["remaining_s"] == 1800 and out["minutes"] == 30 and out["expires_at"]
    clock.t += 29 * 60
    assert switch.status()["remaining_s"] == 60
    clock.t += 60
    switch.tick()
    assert switch.status()["on"] is False
    entries = _audit(in_tmp)
    assert [e["type"] for e in entries] == ["root_on", "root_off"]
    assert entries[1]["why"] == "expired"
    assert code not in _audit_text(in_tmp)


def test_codes_one_step_off_work_two_steps_off_dont(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    assert switch.enable_with_code(_code(clock, -2), 5)["reason"] == "wrong"
    assert switch.enable_with_code(_code(clock, 2), 5)["reason"] == "wrong"
    assert switch.enable_with_code(_code(clock, -1), 5)["ok"] is True


def test_a_code_works_once_even_across_a_restart(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    code = _code(clock)
    assert switch.enable_with_code(code, 5)["ok"] is True
    switch.revoke("odysseus")
    again = switch.enable_with_code(code, 5)
    assert again["ok"] is False and again["reason"] == "reused"
    # A new helper process (same state file) remembers it, and starts off.
    restarted, _ = _parts(in_tmp, clock)
    assert restarted.status()["on"] is False
    assert restarted.enable_with_code(code, 5)["reason"] == "reused"
    # An earlier code (still inside the drift window) is refused as well.
    assert restarted.enable_with_code(_code(clock, -1), 5)["reason"] == "reused"
    clock.t += 30
    assert restarted.enable_with_code(_code(clock), 5)["ok"] is True
    assert oct(os.stat(in_tmp / "state" / "state.json").st_mode & 0o777) == "0o600"


def test_unreadable_state_refuses_every_code(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    (in_tmp / "state").mkdir()
    (in_tmp / "state" / "state.json").write_text("{not json")
    out = switch.enable_with_code(_code(clock), 5)
    assert out["ok"] is False and out["reason"] == "state"


def test_five_wrong_codes_lock_enable_for_fifteen_minutes(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    wrong = "000000" if _code(clock) != "000000" else "111111"
    for left in (4, 3, 2, 1):
        out = switch.enable_with_code(wrong, 5)
        assert out["reason"] == "wrong" and out["attempts_left"] == left
    out = switch.enable_with_code(wrong, 5)
    assert out["attempts_left"] == 0 and out["locked_s"] == mod.LOCKOUT_S
    # Locked: even the right code is refused, without being checked or used up.
    out = switch.enable_with_code(_code(clock), 5)
    assert out["reason"] == "locked" and "Try again in 15 min" in out["error"]
    assert switch.status()["locked_s"] == mod.LOCKOUT_S
    clock.t += mod.LOCKOUT_S
    st = switch.status()
    assert st["locked_s"] == 0 and st["attempts_left"] == mod.MAX_BAD_CODES
    assert switch.enable_with_code(_code(clock), 5)["ok"] is True
    assert "locked" in [e["type"] for e in _audit(in_tmp)]


def test_a_right_code_resets_the_wrong_count(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    wrong = "000000" if _code(clock) != "000000" else "111111"
    for _ in range(4):
        switch.enable_with_code(wrong, 5)
    assert switch.enable_with_code(_code(clock), 5)["ok"] is True
    assert switch.status()["attempts_left"] == mod.MAX_BAD_CODES


@pytest.mark.parametrize("code", ["12345", "1234567", "12a456", "", None, 123456])
def test_malformed_codes_are_refused_without_counting(in_tmp, code):
    switch, _ = _parts(in_tmp, Clock())
    out = switch.enable_with_code(code, 5)
    assert out["reason"] == "bad_request"
    assert switch.status()["attempts_left"] == mod.MAX_BAD_CODES


def test_minutes_are_whole_and_at_most_ninety(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    for bad in (0, 91, -5, "30", 30.0, True, None):
        out = switch.enable_with_code(_code(clock), bad)
        assert out["reason"] == "bad_request", bad
    assert switch.enable_with_code(_code(clock), 90)["remaining_s"] == 90 * 60
    # --max-minutes can lower the cap, never raise it.
    assert _parts(in_tmp, clock, max_minutes=500)[0].max_minutes == 90
    low, _ = _parts(in_tmp, clock, max_minutes=20)
    assert low.enable_from_control(21)["reason"] == "bad_request"


def test_enabling_again_while_on_starts_a_new_window(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    switch.enable_with_code(_code(clock), 15)
    clock.t += 600
    out = switch.enable_with_code(_code(clock), 30)
    assert out["ok"] is True and out["remaining_s"] == 1800


def test_revoke_needs_no_code_and_is_logged_only_when_root_was_on(in_tmp):
    clock = Clock()
    switch, _ = _parts(in_tmp, clock)
    assert switch.revoke("odysseus")["was_on"] is False
    switch.enable_with_code(_code(clock), 15)
    out = switch.revoke("odysseus")
    assert out["ok"] is True and out["was_on"] is True and out["on"] is False
    assert [e["type"] for e in _audit(in_tmp)] == ["root_on", "root_off"]
    assert _audit(in_tmp)[1] == {**_audit(in_tmp)[1], "why": "revoked", "via": "odysseus"}


# ---------------------------------------------------------------------------
# Sockets
# ---------------------------------------------------------------------------

def test_hello_status_enable_revoke_over_the_socket(in_tmp):
    clock = Clock()
    h = _helper(in_tmp, clock)
    code = _code(clock)

    async def go():
        hello = await _raw(SOCK, {"type": "hello"})
        assert hello["ok"] and hello["helper"] == "creator-root-helper"
        assert hello["capabilities"] == ["hello", "status", "enable", "revoke"]
        assert hello["runs_commands"] is False and hello["on"] is False
        assert (await _raw(SOCK, {"type": "run", "command": "id"}))["ok"] is False
        on = await _raw(SOCK, {"type": "enable", "code": code, "minutes": 20})
        assert on["ok"] and on["on"] and on["remaining_s"] == 1200
        assert (await _raw(SOCK, {"type": "status"}))["on"] is True
        off = await _raw(SOCK, {"type": "revoke"})
        assert off["ok"] and off["was_on"] and not off["on"]

    _run(h, go)
    assert not os.path.exists(SOCK) and not os.path.exists(CONTROL)
    assert oct(os.stat(in_tmp / "log" / "audit.jsonl").st_mode & 0o777) == "0o600"
    text = _audit_text(in_tmp)
    assert code not in text
    entries = _audit(in_tmp)
    enable = next(e for e in entries if e.get("request_type") == "enable")
    assert enable["minutes"] == 20 and enable["peer_uid"] == os.getuid() and "code" not in enable
    assert entries[-1] == {**entries[-1], "type": "stop", "root_was_on": False}


def test_a_restart_starts_with_root_off(in_tmp):
    clock = Clock()
    h = _helper(in_tmp, clock)
    _run(h, lambda: _raw(SOCK, {"type": "enable", "code": _code(clock), "minutes": 20}))
    assert _audit(in_tmp)[-1]["root_was_on"] is True
    h2 = _helper(in_tmp, clock)
    assert _run(h2, lambda: _raw(SOCK, {"type": "status"}))["on"] is False


def test_other_uids_are_refused_before_reading(in_tmp):
    clock = Clock()
    h = _helper(in_tmp, clock, allow=[os.getuid() + 12345], control=[os.getuid() + 12345])

    async def go():
        return [await _raw(SOCK, {"type": "status"}), await _raw(CONTROL, {"type": "status"})]

    assert _run(h, go) == [{"ok": False, "error": "not allowed"}] * 2
    refused = [e for e in _audit(in_tmp) if e["type"] == "connection"]
    assert len(refused) == 2 and all(e["error"] == "peer uid not allowed" and "request_type" not in e
                                     for e in refused)


def test_control_socket_switches_without_a_code_and_has_no_hello(in_tmp):
    clock = Clock()
    h = _helper(in_tmp, clock)

    async def go():
        on = await _raw(CONTROL, {"type": "enable", "minutes": 10})
        assert on["ok"] and on["remaining_s"] == 600
        assert (await _raw(SOCK, {"type": "status"}))["on"] is True
        assert (await _raw(CONTROL, {"type": "enable", "minutes": 91}))["ok"] is False
        assert (await _raw(CONTROL, {"type": "hello"}))["ok"] is False
        assert (await _raw(CONTROL, {"type": "revoke"}))["was_on"] is True

    _run(h, go)
    assert {"type": "root_on", "via": "control"}.items() <= next(
        e for e in _audit(in_tmp) if e["type"] == "root_on").items()
    assert oct(os.stat(in_tmp / "c").st_mode & 0o777) == "0o700"


def test_terminal_command_talks_to_the_control_socket(in_tmp, capsys):
    clock = Clock()
    h = _helper(in_tmp, clock)

    async def go():
        on = mod._parse_args(["on", "45", "--control-socket", CONTROL])
        assert await asyncio.to_thread(mod._control, on) == 0
        st = mod._parse_args(["status", "--control-socket", CONTROL])
        assert await asyncio.to_thread(mod._control, st) == 0
        off = mod._parse_args(["off", "--control-socket", CONTROL])
        assert await asyncio.to_thread(mod._control, off) == 0

    _run(h, go)
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Root is ON until") and "(45 min left)" in out[0]
    assert out[2] == "Root is off."
    assert mod._parse_args(["on"]).minutes == 30


def test_socket_folders_must_be_closed(in_tmp):
    clock = Clock()
    os.chmod(in_tmp / "s", 0o755)
    h = _helper(in_tmp, clock)
    with pytest.raises(RuntimeError, match="0700"):
        asyncio.run(h.start())
    os.chmod(in_tmp / "s", 0o700)
    with pytest.raises(RuntimeError, match="owned by root"):
        mod.check_socket_folder(SOCK, os.getuid() + 1)


def test_root_and_nobody_are_never_allowed_on_the_odysseus_socket(in_tmp):
    switch, audit = _parts(in_tmp, Clock())
    with pytest.raises(ValueError, match="uid 0"):
        mod.RootHelper(switch, audit, SOCK, [1000, 0], CONTROL)
    with pytest.raises(ValueError, match="no --allow-uid"):
        mod.RootHelper(switch, audit, SOCK, [], CONTROL)


def test_unit_file_and_overlay():
    unit = (_HELPER_PATH.parent / "creator-root-helper.service").read_text()
    for line in ("User=root", "serve --allow-uid 1000", "NoNewPrivileges=yes", "CapabilityBoundingSet=\n",
                 "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX\n", "ProtectSystem=strict",
                 "ReadWritePaths=/srv/creator-root\n", "ProtectHome=yes", "RuntimeDirectoryMode=0700",
                 "StateDirectoryMode=0700", "LogsDirectoryMode=0700"):
        assert line in unit, line
    overlay = (_HELPER_PATH.parent.parent / "docker" / "creator-root-helper.yml").read_text()
    assert "/srv/creator-root:/app/root-helper:ro" in overlay
    # The control socket's folder is never mounted.
    assert "/run/creator-root" not in overlay.split("services:")[1]


# ---------------------------------------------------------------------------
# Client and routes
# ---------------------------------------------------------------------------

def test_client_without_the_helper(monkeypatch, in_tmp):
    monkeypatch.delenv("CREATOR_ROOT_SOCKET", raising=False)
    assert client.socket_path() == "/app/root-helper/root.sock"
    out = asyncio.run(client.status(path="/nonexistent/x/root.sock"))
    assert out["installed"] is False and out["available"] is False and "COMPOSE_FILE" in out["error"]
    out = asyncio.run(client.status(path=str(in_tmp / "s" / "root.sock")))
    assert out["installed"] is True and "systemctl status creator-root-helper" in out["error"]


class _Auth:
    is_configured = True

    def get_privileges(self, user):
        return {**DEFAULT_PRIVILEGES, "can_use_creator": user in ("alice", "carol")}

    def is_admin(self, user):
        return user == "alice"


def _routes(monkeypatch):
    from routes import creator_routes
    from src.creator_mode import CreatorManager
    monkeypatch.setattr(creator_routes, "require_user", lambda r: r.state.current_user)
    monkeypatch.setenv("CREATOR_ROOT_SOCKET", SOCK)
    router = creator_routes.setup_creator_routes(CreatorManager.__new__(CreatorManager))
    return {(r.path, list(r.methods)[0]): r.endpoint for r in router.routes
            if getattr(r, "path", "").startswith("/api/creator/root/")}


def _req(user, token=False, auth=True):
    return SimpleNamespace(state=SimpleNamespace(current_user=user, api_token=token), headers={},
                           app=SimpleNamespace(state=SimpleNamespace(auth_manager=_Auth() if auth else None)))


def test_root_routes_are_for_logged_in_admins_only(monkeypatch, in_tmp):
    routes = _routes(monkeypatch)
    status = routes[("/api/creator/root/status", "GET")]
    for req in (_req("bob"), _req("carol"), _req("alice", token=True), _req("", auth=False)):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(status(request=req))
        assert exc.value.status_code == 403


def test_root_routes_reach_the_helper(monkeypatch, in_tmp):
    routes = _routes(monkeypatch)
    clock = Clock()
    h = _helper(in_tmp, clock)
    status = routes[("/api/creator/root/status", "GET")]
    enable = routes[("/api/creator/root/enable", "POST")]
    revoke = routes[("/api/creator/root/revoke", "POST")]
    body = SimpleNamespace
    wrong = "000000" if _code(clock) != "000000" else "111111"

    async def go():
        st = await status(request=_req("alice"))
        assert st["available"] is True and st["installed"] is True and st["on"] is False
        with pytest.raises(HTTPException) as exc:
            await enable(body=body(code=wrong, minutes=30), request=_req("alice"))
        assert exc.value.status_code == 400 and "isn't right" in exc.value.detail
        st = await enable(body=body(code=_code(clock), minutes=30), request=_req("alice"))
        assert st["on"] is True and st["remaining_s"] == 1800
        st = await revoke(request=_req("alice"))
        assert st["on"] is False

    _run(h, go)


def test_root_routes_without_the_helper(monkeypatch, in_tmp):
    routes = _routes(monkeypatch)
    st = asyncio.run(routes[("/api/creator/root/status", "GET")](request=_req("alice")))
    assert st["available"] is False and st["installed"] is True
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes[("/api/creator/root/revoke", "POST")](request=_req("alice")))
    assert exc.value.status_code == 503


def test_enable_request_validates_code_and_minutes():
    from pydantic import ValidationError
    from routes import creator_routes
    from src.creator_mode import CreatorManager
    router = creator_routes.setup_creator_routes(CreatorManager.__new__(CreatorManager))
    route = next(r for r in router.routes if getattr(r, "path", "") == "/api/creator/root/enable")
    model = route.body_field.type_ if hasattr(route.body_field, "type_") else route.body_field.field_info.annotation
    assert model(code="123456", minutes=90).minutes == 90
    for bad in ({"code": "12345", "minutes": 5}, {"code": "12345a", "minutes": 5},
                {"code": "123456", "minutes": 0}, {"code": "123456", "minutes": 91}):
        with pytest.raises(ValidationError):
            model(**bad)
