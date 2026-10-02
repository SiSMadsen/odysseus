"""Creator host helper, Phase 6: the helper itself
(host_helper/creator_helper.py, run here on a temporary socket), the client
in src/creator_host_helper.py, and the connection-test route. 6a: hello and
the connection rules. 6b: run."""

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
    kw.setdefault("workdir", str(tmp_path / "work"))
    kw.setdefault("home", str(tmp_path / "home"))
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
    assert reply["capabilities"] == ["hello", "run"]
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


def test_unknown_request_types_are_refused(in_tmp):
    h = _helper(in_tmp)
    reply = _run(h, lambda: client.request({"type": "shell", "command": "id"}, path=SOCK))
    assert reply["ok"] is False and "unknown request type" in reply["error"]
    assert reply["capabilities"] == ["hello", "run"]


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
    # Folder there, socket missing: the helper isn't running.
    out = asyncio.run(client.hello(path="nope.sock"))
    assert out["ok"] is False and "systemctl status creator-helper" in out["error"]
    assert "isn't mounted" not in out["error"]
    # No folder: the compose overlay isn't mounting it.
    out = asyncio.run(client.hello(path="missing-dir/helper.sock"))
    assert "isn't mounted" in out["error"]
    assert "docker compose config | grep host-helper" in out["error"]
    assert "COMPOSE_FILE=docker-compose.yml:docker/creator-helper.yml" in out["error"]


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
    assert out["ok"] is True and out["reply"]["capabilities"] == ["hello", "run"]


def test_unit_file_keeps_the_helper_unprivileged_and_narrow():
    unit = (_HELPER_PATH.parent / "creator-helper.service").read_text()
    for line in ("User=creator", "NoNewPrivileges=yes", "CapabilityBoundingSet=",
                 "ProtectSystem=strict", "ProtectHome=yes", "RestrictSUIDSGID=yes",
                 "ReadWritePaths=/srv/creator-helper /var/www/html", "--allow-uid 1000"):
        assert line in unit, line
    # 6b decisions: network allowed for commands; no JIT-breaking MDWE.
    assert "\nPrivateNetwork=yes" not in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in unit
    assert "\nMemoryDenyWriteExecute=yes" not in unit
    overlay = (_HELPER_PATH.parent.parent / "docker" / "creator-helper.yml").read_text()
    assert "/srv/creator-helper:/app/host-helper:ro" in overlay


def test_polkit_rule_covers_only_apache_and_not_stop():
    rule = (_HELPER_PATH.parent / "50-creator-apache.rules").read_text()
    assert 'subject.user != "creator"' in rule
    assert 'action.lookup("unit") != "apache2.service"' in rule
    assert '"reload"' in rule and '"restart"' in rule
    for verb in ('"stop"', '"enable"', '"disable"', '"mask"', '"kill"'):
        assert verb not in rule, verb


# ---------------------------------------------------------------------------
# 6b: run
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); count it as gone.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except (OSError, IndexError):
        return False


def _wait_gone(pid: int, seconds: float = 3.0) -> bool:
    import time as _t
    end = _t.time() + seconds
    while _t.time() < end:
        if not _pid_alive(pid):
            return True
        _t.sleep(0.05)
    return False


def test_run_returns_output_and_uses_a_clean_environment(in_tmp, monkeypatch):
    monkeypatch.setenv("ODY_TEST_LEAK", "should-not-be-seen")
    h = _helper(in_tmp)
    cmd = ('echo "out:$HOME:$PWD:${ODY_TEST_LEAK:-none}"; echo err >&2; '
           'touch made; stat -c %a made; cat; exit 3')
    r = _run(h, lambda: client.run(cmd, timeout_s=10, path=SOCK))
    assert r["ok"] is True and r["exit_code"] == 3
    lines = r["stdout"].splitlines()
    assert lines[0] == f"out:{in_tmp / 'home'}:{in_tmp / 'work'}:none"
    assert lines[1] == "644"            # umask 022
    assert r["stderr"] == "err\n"
    assert r["timed_out"] is False and r["disconnected"] is False
    assert (in_tmp / "work" / "made").exists()


def test_run_time_limit_kills_the_command(in_tmp):
    h = _helper(in_tmp)
    r = _run(h, lambda: client.run("echo $$ > pid; sleep 30", timeout_s=1, path=SOCK))
    assert r["timed_out"] is True and r["duration_s"] < 5
    assert _wait_gone(int((in_tmp / "work" / "pid").read_text()))


def test_run_leaves_nothing_running(in_tmp):
    h = _helper(in_tmp)
    r = _run(h, lambda: client.run("sleep 300 & echo $! > bg; echo started", timeout_s=10, path=SOCK))
    assert r["exit_code"] == 0 and r["stdout"] == "started\n"
    assert r["duration_s"] < 5
    assert _wait_gone(int((in_tmp / "work" / "bg").read_text()))


def test_closing_the_connection_stops_the_command(in_tmp):
    h = _helper(in_tmp)
    marker = in_tmp / "work" / "finished"

    async def go():
        task = asyncio.ensure_future(client.run("echo $$ > pid; sleep 3; touch finished", timeout_s=30, path=SOCK))
        pidfile = in_tmp / "work" / "pid"
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        task.cancel()   # what Creator's Stop does to the host_exec call
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.3)
        return int(pidfile.read_text())

    pid = _run(h, go)
    assert _wait_gone(pid)
    import time as _t
    _t.sleep(3.2)
    assert not marker.exists()
    run_entry = [e for e in _audit(in_tmp) if e.get("request_type") == "run"][0]
    assert run_entry["disconnected"] is True


def test_output_is_capped_but_counted(in_tmp):
    h = _helper(in_tmp, output_cap_bytes=1000)
    r = _run(h, lambda: client.run("head -c 50000 /dev/zero | tr '\\0' a", timeout_s=10, path=SOCK))
    assert len(r["stdout"]) == 1000 and r["stdout_bytes"] == 50000 and r["truncated"] is True


def test_one_command_at_a_time(in_tmp):
    h = _helper(in_tmp)

    async def go():
        first = asyncio.ensure_future(client.run("sleep 1; echo first", timeout_s=10, path=SOCK))
        await asyncio.sleep(0.3)
        second = await client.run("echo second", timeout_s=10, path=SOCK)
        return await first, second

    first, second = _run(h, go)
    assert first["stdout"] == "first\n"
    assert second == {"ok": False, "error": "busy: another command is running"}


def test_secrets_are_blanked_in_the_audit_log_not_in_the_reply(in_tmp):
    h = _helper(in_tmp)
    r = _run(h, lambda: client.run("echo token=hunter2-secret", timeout_s=10, redact=["hunter2-secret", "ab"], path=SOCK))
    assert r["stdout"] == "token=hunter2-secret\n"   # Odysseus scrubs it before the model sees it
    log = (in_tmp / "log" / "audit.jsonl").read_text()
    assert "hunter2-secret" not in log
    entry = [e for e in _audit(in_tmp) if e.get("request_type") == "run"][0]
    assert entry["command"] == "echo token=[REDACTED]"
    assert entry["stdout_preview"] == "token=[REDACTED]\n"
    assert entry["exit_code"] == 0


def test_run_requests_are_validated(in_tmp):
    parse = helper_mod.Helper.parse_run
    with pytest.raises(ValueError):
        parse({"command": "  "}, 600)
    with pytest.raises(ValueError):
        parse({"command": "x" * 100_001}, 600)
    with pytest.raises(ValueError):
        parse({"command": "ls", "timeout_s": "soon"}, 600)
    with pytest.raises(ValueError):
        parse({"command": "ls", "redact": "nope"}, 600)
    with pytest.raises(ValueError):
        parse({"command": "a\x00b"}, 600)
    assert parse({"command": "ls", "timeout_s": 99999}, 600)[1] == 600
    assert parse({"command": "ls", "timeout_s": 0}, 600)[1] == 1
    assert parse({"command": "ls"}, 600)[1] == 120
    # Values under 4 characters aren't blanked (they'd blank half the log).
    assert parse({"command": "ls", "redact": ["abc", "abcd", 5]}, 600)[2] == ["abcd"]
    h = _helper(in_tmp)
    r = _run(h, lambda: client.request({"type": "run", "command": ""}, path=SOCK))
    assert r["ok"] is False and "command" in r["error"]
