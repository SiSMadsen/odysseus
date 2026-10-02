"""Creator mode: engine, job record, routes, privilege (Phase 1) and the
safety net — time limit, stop, live log, audit log, protected paths, one job
at a time (Phase 3)."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.auth import ADMIN_PRIVILEGES, DEFAULT_PRIVILEGES
from core.database import CreatorJob, CreatorSecret
from core.middleware import INTERNAL_TOOL_USER
from routes import creator_routes
from src import creator_mode
from src.creator_mode import CreatorManager


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'creator.db'}")
    CreatorJob.__table__.create(bind=engine)
    CreatorSecret.__table__.create(bind=engine)
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def killed_shells(tmp_path, monkeypatch):
    """Audit logs go to a temp dir; tmux kills are recorded, not run."""
    monkeypatch.setattr("src.creator_safety.audit_dir", lambda: tmp_path / "audit")
    killed = []

    async def fake_kill(session_id):
        killed.append(session_id)

    monkeypatch.setattr(creator_mode, "kill_job_shell", fake_kill)
    return killed


def _audit_entries(tmp_path, job_id):
    path = tmp_path / "audit" / f"{job_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _fake_loop(chunks, calls=None, hang=False):
    async def loop(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        for c in chunks:
            yield c
        if hang:
            await asyncio.Event().wait()
        yield "data: [DONE]\n\n"
    return loop


async def _wait_finished(mgr, job_id):
    for _ in range(200):
        if not mgr.is_running(job_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


def test_privilege_off_by_default_and_on_for_admins():
    assert DEFAULT_PRIVILEGES["can_use_creator"] is False
    assert ADMIN_PRIVILEGES["can_use_creator"] is True


def test_job_runs_agent_loop_with_high_caps_and_records_report(session_factory):
    calls = []
    chunks = [
        _sse({"type": "agent_step", "round": 1}),
        _sse({"type": "tool_start", "tool": "bash", "command": "ls"}),
        _sse({"type": "tool_output", "tool": "bash", "command": "ls", "output": "a\nb", "exit_code": 0}),
        _sse({"delta": "thinking...", "thinking": True}),
        _sse({"delta": "Listed "}),
        _sse({"delta": "the files."}),
        _sse({"type": "metrics", "data": {}}),
    ]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop(chunks, calls))
        job_id = mgr.start_job("list files", "http://x/v1/chat/completions", "m",
                               owner="alice", disabled_tools={"bash_x"})
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["status"] == "done"
    assert "Listed the files." in job["report"]
    assert job["owner"] == "alice"
    assert job["finished_at"]
    assert [e["type"] for e in job["events"]] == ["round", "tool_start", "tool_output"]
    assert job["events"][2]["output"] == "a\nb"

    kw = calls[0]
    assert kw["max_rounds"] == creator_mode.SEGMENT_ROUNDS
    assert kw["max_tool_calls"] == creator_mode.CREATOR_MAX_TOOL_CALLS
    assert kw["workload"] == "background"
    assert kw["owner"] == "alice"
    assert kw["disabled_tools"] == {"bash_x"}
    assert kw["messages"][-1] == {"role": "user", "content": "list files"}


def test_stop_cancels_running_job(session_factory, killed_shells):
    async def run():
        mgr = CreatorManager(session_factory=session_factory,
                             agent_loop=_fake_loop([_sse({"type": "agent_step", "round": 1})], hang=True))
        job_id = mgr.start_job("long task", "u", "m", owner="alice")
        await asyncio.sleep(0.05)
        assert mgr.is_running(job_id)
        assert mgr.stop_job(job_id) is True
        await _wait_finished(mgr, job_id)
        assert mgr.stop_job(job_id) is False
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["status"] == "stopped"
    assert job["events"][-1]["type"] == "stopped"
    # The job's tmux shell is killed, so a running command dies with it.
    assert job["id"] in killed_shells


def test_loop_error_marks_job_error(session_factory):
    async def broken(**kwargs):
        yield _sse({"type": "agent_step", "round": 1})
        raise RuntimeError("model unreachable")

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=broken)
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["status"] == "error"
    assert "model unreachable" in job["error"]


# Approval requests no longer end the job as "blocked": Phase 2 pauses it.
# See tests/test_creator_phase2.py.


def test_time_limit_stops_job_as_timeout(session_factory, killed_shells, monkeypatch):
    monkeypatch.setattr(creator_mode, "_SECONDS_PER_MINUTE", 0.05)

    async def run():
        mgr = CreatorManager(session_factory=session_factory,
                             agent_loop=_fake_loop([_sse({"delta": "partial"})], hang=True))
        job_id = mgr.start_job("long task", "u", "m", owner="alice", max_minutes=1)
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["status"] == "timeout"
    assert "1 minute" in job["error"]
    assert "partial" in job["report"]
    assert job["events"][-1]["type"] == "timeout"
    assert job["id"] in killed_shells


def test_time_limit_defaults_to_setting_and_is_clamped(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 30)
    assert creator_mode.default_max_minutes() == 30
    assert creator_mode.clamp_minutes(0) == creator_mode.MIN_MAX_MINUTES
    assert creator_mode.clamp_minutes(10**6) == creator_mode.MAX_MAX_MINUTES
    assert creator_mode.clamp_minutes("junk") == creator_mode.DEFAULT_MAX_MINUTES


def test_only_one_job_at_a_time(session_factory):
    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop([], hang=True))
        first = mgr.start_job("a", "u", "m", owner="alice")
        with pytest.raises(creator_mode.CreatorBusyError):
            mgr.start_job("b", "u", "m", owner="bob")
        mgr.stop_job(first)
        await _wait_finished(mgr, first)
        second = mgr.start_job("b", "u", "m", owner="bob")
        mgr.stop_job(second)
        await _wait_finished(mgr, second)
        return first, second

    first, second = asyncio.run(run())
    assert first != second


def test_audit_log_and_events_are_redacted(session_factory, tmp_path):
    key = "my-endpoint-api-key-123456"
    chunks = [
        _sse({"type": "tool_start", "tool": "bash", "command": "env",
              "full_command": "env"}),
        _sse({"type": "tool_output", "tool": "bash", "command": "env", "exit_code": 0,
              "output": f"OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\nKEY={key}\nHOME=/root"}),
        _sse({"delta": f"Your key is {key}."}),
    ]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop(chunks))
        job_id = mgr.start_job("show env", "u", "m", owner="alice",
                               headers={"Authorization": f"Bearer {key}"})
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    entries = _audit_entries(tmp_path, job["id"])
    assert [e["type"] for e in entries] == ["job_start", "host_probe", "tool_start", "tool_output", "job_end"]
    assert entries[1]["ok"] is False   # no host helper in tests
    assert entries[3]["exit_code"] == 0 and "HOME=/root" in entries[3]["output"]
    assert entries[-1]["status"] == "done"

    stored = json.dumps(entries) + json.dumps(job)
    assert key not in stored
    assert "sk-abcdefghijklmnopqrstuvwx" not in stored
    assert "[REDACTED]" in job["events"][-1]["output"]
    assert "Your key is [REDACTED]." in job["report"]

    import os, stat
    mode = stat.S_IMODE(os.stat(tmp_path / "audit" / f"{job['id']}.jsonl").st_mode)
    assert mode == 0o600


def test_redactor_patterns():
    from src.creator_safety import Redactor
    r = Redactor(["supersecretvalue"])
    out = r.text(
        "a supersecretvalue b\n"
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
        "DB_PASSWORD=hunter2hunter2\n"
        "ghp_abcdefghijklmnopqrstuvwxyz0123\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n"
        "plain text stays"
    )
    for secret in ("supersecretvalue", "abcdefghijklmnopqrstuvwxyz", "hunter2hunter2", "ghp_", "MIIE"):
        assert secret not in out
    assert "plain text stays" in out
    assert "DB_PASSWORD=[REDACTED]" in out


def test_protected_path_check_matches_whole_paths():
    from src.creator_safety import make_protected_action_check
    assert make_protected_action_check([]) is None
    check = make_protected_action_check(["/etc", "/home/me/photos/"])
    assert check("bash", "cat /etc/passwd")
    assert check("bash", "ls /etc")
    assert check("write_file", {"path": "/home/me/photos/a.jpg"})
    assert "/home/me/photos" in check("bash", "rm -rf '/home/me/photos'")
    assert check("bash", "ls /etcetera") is None
    assert check("bash", "ls /home/x/etc") is None
    assert check("bash", "ls /home/me/photosynth") is None


def test_protected_check_beats_approval_bypass():
    from src.creator_safety import make_protected_action_check
    from src.tool_capabilities import ToolRunSecurityContext
    ctx = ToolRunSecurityContext(
        approval_gate_bypassed=True,
        protected_action_check=make_protected_action_check(["/etc"]),
    )
    decision = ctx.decision_for("bash", "rm /etc/hosts")
    assert not decision.allowed
    assert "/etc" in decision.reason
    assert ctx.decision_for("bash", "ls /tmp").allowed


def test_protected_paths_reach_the_agent_loop(session_factory):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop([], calls))
        a = mgr.start_job("t", "u", "m", protected_paths=["/etc"])
        await _wait_finished(mgr, a)
        b = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, b)

    asyncio.run(run())
    assert calls[0]["protected_action_check"]("bash", "cat /etc/x")
    # Without configured paths, only the always-on secret-store tripwire
    # (Phase 4) applies.
    assert calls[1]["protected_action_check"]("bash", "cat /etc/x") is None
    assert calls[1]["protected_action_check"]("bash", "cat .app_key")


# A protected-path request now pauses the job and says what it wanted to
# run: see test_protected_path_pause_* in tests/test_creator_phase2.py.


def test_live_events_have_increasing_seq(session_factory):
    chunks = [_sse({"type": "agent_step", "round": n}) for n in (1, 2, 3)]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop(chunks, hang=True))
        job_id = mgr.start_job("t", "u", "m")
        await asyncio.sleep(0.05)
        after_one = mgr.live_events_after(job_id, 1)
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)
        return after_one, mgr.live_events_after(job_id, 0)

    after_one, finished = asyncio.run(run())
    assert [e["seq"] for e in after_one] == [2, 3]
    assert finished is None


def test_orphaned_running_jobs_marked_interrupted(session_factory):
    db = session_factory()
    db.add(CreatorJob(id="cr-aaaaaaaaaaaa", owner="alice", task="t", status="running"))
    db.commit()
    db.close()
    mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop([]))
    assert mgr.get_job("cr-aaaaaaaaaaaa")["status"] == "interrupted"


def test_get_job_rejects_malformed_ids(session_factory):
    mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop([]))
    assert mgr.get_job("../etc") is None
    assert mgr.get_job("rp-123") is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class _AuthMgr:
    is_configured = True

    def __init__(self, privs):
        self._privs = privs

    def get_privileges(self, user):
        return self._privs.get(user, dict(DEFAULT_PRIVILEGES))


async def _connected():
    return False


def _request(user, privs):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_AuthMgr(privs))),
        is_disconnected=_connected,
    )


def _route(router, path, method):
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


@pytest.fixture
def routed(session_factory, monkeypatch):
    monkeypatch.setattr(creator_routes, "require_user", lambda request: request.state.current_user)
    monkeypatch.setattr(creator_routes, "_resolve_creator_endpoint",
                        lambda user, endpoint_id, model: ("http://x/v1/chat/completions", "m", {}))
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop([_sse({"delta": "done"})]))
    return mgr, creator_routes.setup_creator_routes(mgr)


ALLOWED = {"alice": {**DEFAULT_PRIVILEGES, "can_use_creator": True},
           "bob": {**DEFAULT_PRIVILEGES, "can_use_creator": True}}


def test_start_requires_privilege(routed):
    _, router = routed
    start = _route(router, "/api/creator/start", "POST")
    body = SimpleNamespace(task="do it", endpoint_id=None, model=None, max_minutes=None, approve_untrusted=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start(body=body, request=_request("carol", {})))
    assert exc.value.status_code == 403


def test_privilege_check_fails_closed_when_key_missing(routed):
    _, router = routed
    start = _route(router, "/api/creator/start", "POST")
    body = SimpleNamespace(task="do it", endpoint_id=None, model=None, max_minutes=None, approve_untrusted=False)
    legacy = {k: v for k, v in DEFAULT_PRIVILEGES.items() if k != "can_use_creator"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start(body=body, request=_request("dave", {"dave": legacy})))
    assert exc.value.status_code == 403


def test_internal_tool_user_cannot_start(routed):
    _, router = routed
    start = _route(router, "/api/creator/start", "POST")
    body = SimpleNamespace(task="do it", endpoint_id=None, model=None, max_minutes=None, approve_untrusted=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start(body=body, request=_request(INTERNAL_TOOL_USER, ALLOWED)))
    assert exc.value.status_code == 403


def test_start_status_report_and_owner_scope(routed):
    mgr, router = routed
    start = _route(router, "/api/creator/start", "POST")
    status = _route(router, "/api/creator/status/{job_id}", "GET")
    report = _route(router, "/api/creator/report/{job_id}", "GET")
    stop = _route(router, "/api/creator/stop/{job_id}", "POST")

    async def run():
        out = await start(body=SimpleNamespace(task="do it", endpoint_id=None, model=None, max_minutes=None, approve_untrusted=False),
                          request=_request("alice", ALLOWED))
        job_id = out["job_id"]
        await _wait_finished(mgr, job_id)
        st = await status(job_id=job_id, request=_request("alice", ALLOWED), since=0)
        rep = await report(job_id=job_id, request=_request("alice", ALLOWED))
        for fn, kwargs in ((status, {"since": 0}), (report, {}), (stop, {})):
            with pytest.raises(HTTPException) as exc:
                await fn(job_id=job_id, request=_request("bob", ALLOWED), **kwargs)
            assert exc.value.status_code == 404
        return st, rep

    st, rep = asyncio.run(run())
    assert st["status"] == "done"
    assert st["has_report"] is True
    assert "done" in rep["report"]


def test_jobs_list_is_owner_scoped_newest_first(routed, session_factory):
    from datetime import datetime
    mgr, router = routed
    jobs = _route(router, "/api/creator/jobs", "GET")
    db = session_factory()
    for job_id, owner, day in (("cr-000000000001", "alice", 1), ("cr-000000000002", "bob", 2),
                               ("cr-000000000003", "alice", 3), ("cr-000000000004", None, 4)):
        db.add(CreatorJob(id=job_id, owner=owner, task="t" * 400, status="done",
                          started_at=datetime(2026, 9, day), report="r" if day == 3 else None))
    db.commit()
    db.close()

    out = asyncio.run(jobs(request=_request("alice", ALLOWED), limit=50))["jobs"]
    assert [j["job_id"] for j in out] == ["cr-000000000003", "cr-000000000001"]
    assert out[0]["has_report"] is True and out[1]["has_report"] is False
    assert len(out[0]["task"]) < 400 and "events" not in out[0]
    assert out[0]["started_at"] == "2026-09-03T00:00:00Z"
    assert len(asyncio.run(jobs(request=_request("alice", ALLOWED), limit=0))["jobs"]) == 1
    # Auth off: the empty owner sees only ownerless jobs.
    assert [j["job_id"] for j in mgr.list_jobs("")] == ["cr-000000000004"]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(jobs(request=_request("carol", {}), limit=50))
    assert exc.value.status_code == 403


def test_second_start_is_409_while_a_job_runs(session_factory, monkeypatch):
    monkeypatch.setattr(creator_routes, "require_user", lambda request: request.state.current_user)
    monkeypatch.setattr(creator_routes, "_resolve_creator_endpoint",
                        lambda user, endpoint_id, model: ("u", "m", {}))
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop([], hang=True))
    start = _route(creator_routes.setup_creator_routes(mgr), "/api/creator/start", "POST")
    body = SimpleNamespace(task="do it", endpoint_id=None, model=None, max_minutes=5, approve_untrusted=False)

    async def run():
        out = await start(body=body, request=_request("alice", ALLOWED))
        with pytest.raises(HTTPException) as exc:
            await start(body=body, request=_request("bob", ALLOWED))
        mgr.stop_job(out["job_id"])
        await _wait_finished(mgr, out["job_id"])
        return out, exc.value

    out, err = asyncio.run(run())
    assert out["max_minutes"] == 5
    assert err.status_code == 409
    assert out["job_id"] not in err.detail


def test_stream_sends_live_events_then_final(session_factory, monkeypatch):
    monkeypatch.setattr(creator_routes, "require_user", lambda request: request.state.current_user)
    monkeypatch.setattr(creator_routes, "_STREAM_POLL_S", 0.01)

    async def slow_loop(**kwargs):
        yield _sse({"type": "tool_start", "tool": "bash", "command": "ls"})
        await asyncio.sleep(0.05)
        yield _sse({"type": "tool_output", "tool": "bash", "command": "ls", "output": "x", "exit_code": 0})
        yield _sse({"delta": "ok"})

    mgr = CreatorManager(session_factory=session_factory, agent_loop=slow_loop)
    stream = _route(creator_routes.setup_creator_routes(mgr), "/api/creator/stream/{job_id}", "GET")

    async def run():
        job_id = mgr.start_job("t", "u", "m", owner="alice")
        resp = await stream(job_id=job_id, request=_request("alice", ALLOWED), since=0)
        msgs = []
        async for chunk in resp.body_iterator:
            msgs.append(json.loads(chunk[len("data: "):]))
        return msgs

    msgs = asyncio.run(run())
    assert [m.get("type") for m in msgs[:-1]] == ["tool_start", "tool_output"]
    assert [m["seq"] for m in msgs[:-1]] == [1, 2]
    assert msgs[-1] == {"final": True, "status": "done", "error": None}


def test_creator_settings_have_defaults():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["creator_max_minutes"] == 60
    assert DEFAULT_SETTINGS["creator_protected_paths"] == []


def test_app_api_blocks_creator_routes():
    from src.tools.system import _APP_API_BLOCKLIST_PREFIXES
    assert any("/api/creator/start".startswith(p) for p in _APP_API_BLOCKLIST_PREFIXES)
