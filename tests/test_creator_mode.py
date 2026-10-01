"""Creator mode Phase 1: engine, job record, routes, privilege."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.auth import ADMIN_PRIVILEGES, DEFAULT_PRIVILEGES
from core.database import CreatorJob
from core.middleware import INTERNAL_TOOL_USER
from routes import creator_routes
from src import creator_mode
from src.creator_mode import CreatorManager


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'creator.db'}")
    CreatorJob.__table__.create(bind=engine)
    return sessionmaker(bind=engine)


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
    assert job["report"] == "Listed the files."
    assert job["owner"] == "alice"
    assert job["finished_at"]
    assert [e["type"] for e in job["events"]] == ["round", "tool_start", "tool_output"]
    assert job["events"][2]["output"] == "a\nb"

    kw = calls[0]
    assert kw["max_rounds"] == creator_mode.CREATOR_MAX_ROUNDS
    assert kw["max_tool_calls"] == creator_mode.CREATOR_MAX_TOOL_CALLS
    assert kw["workload"] == "background"
    assert kw["owner"] == "alice"
    assert kw["disabled_tools"] == {"bash_x"}
    assert kw["messages"][-1] == {"role": "user", "content": "list files"}


def test_stop_cancels_running_job(session_factory):
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


def test_approval_request_ends_job_as_blocked(session_factory, monkeypatch):
    retired = []
    monkeypatch.setattr(CreatorManager, "_retire_approval",
                        staticmethod(lambda aid, owner, jid: retired.append(aid)))
    chunks = [_sse({"type": "tool_output", "tool": "bash",
                    "ask_user": {"kind": "tool_approval", "approval_id": "ap1"}}),
              _sse({"delta": "should not be reached"})]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=_fake_loop(chunks))
        job_id = mgr.start_job("t", "u", "m", owner="alice")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["status"] == "blocked"
    assert retired == ["ap1"]
    assert job["report"] is None


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


def _request(user, privs):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_AuthMgr(privs))),
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
    body = SimpleNamespace(task="do it", endpoint_id=None, model=None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start(body=body, request=_request("carol", {})))
    assert exc.value.status_code == 403


def test_privilege_check_fails_closed_when_key_missing(routed):
    _, router = routed
    start = _route(router, "/api/creator/start", "POST")
    body = SimpleNamespace(task="do it", endpoint_id=None, model=None)
    legacy = {k: v for k, v in DEFAULT_PRIVILEGES.items() if k != "can_use_creator"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start(body=body, request=_request("dave", {"dave": legacy})))
    assert exc.value.status_code == 403


def test_internal_tool_user_cannot_start(routed):
    _, router = routed
    start = _route(router, "/api/creator/start", "POST")
    body = SimpleNamespace(task="do it", endpoint_id=None, model=None)
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
        out = await start(body=SimpleNamespace(task="do it", endpoint_id=None, model=None),
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
    assert rep["report"] == "done"


def test_app_api_blocks_creator_routes():
    from src.tools.system import _APP_API_BLOCKLIST_PREFIXES
    assert any("/api/creator/start".startswith(p) for p in _APP_API_BLOCKLIST_PREFIXES)
