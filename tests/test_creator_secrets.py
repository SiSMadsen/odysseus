"""Creator mode Phase 4: the Secrets section — storage, the server-side on/off
switch behind get_secret, request logging, and redaction of secret values."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.auth import DEFAULT_PRIVILEGES
from core.database import CreatorJob, CreatorSecret
from routes import creator_routes
from src import creator_mode, creator_secrets
from src.creator_mode import CreatorManager
from src.creator_secrets import SecretError, SecretStore, do_get_secret

SECRET = "hunter2-very-secret-value"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own encryption key, audit/access logs in tmp, no real tmux kills."""
    import src.secret_storage as secret_storage
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr("src.creator_safety.audit_dir", lambda: tmp_path / "audit")
    monkeypatch.setattr(creator_secrets, "access_log_path", lambda: tmp_path / "secret_access.jsonl")

    async def no_kill(session_id):
        return None

    monkeypatch.setattr(creator_mode, "kill_job_shell", no_kill)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'creator.db'}")
    CreatorJob.__table__.create(bind=engine)
    CreatorSecret.__table__.create(bind=engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def store(session_factory):
    return SecretStore(session_factory)


def _access_log(tmp_path):
    path = tmp_path / "secret_access.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def _audit(tmp_path, job_id):
    path = tmp_path / "audit" / f"{job_id}.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()]


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _hang_loop(calls=None):
    async def loop(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        await asyncio.Event().wait()
        yield "data: [DONE]\n\n"
    return loop


async def _wait_finished(mgr, job_id):
    for _ in range(200):
        if not mgr.is_running(job_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_secret_storage_check_values_are_generic_and_encrypted(store, session_factory):
    from src.secret_storage import decrypt, encrypt
    token = encrypt("any arbitrary string, not an email password")
    assert token.startswith("enc:") and decrypt(token) == "any arbitrary string, not an email password"

    created = store.create("alice", "github_token", SECRET, "for pushing", enabled=False)
    assert "value" not in created
    assert created["enabled"] is False

    db = session_factory()
    raw = db.execute(text("SELECT value FROM creator_secrets")).scalar()
    db.close()
    assert raw.startswith("enc:") and SECRET not in raw


def test_list_never_includes_values(store):
    store.create("alice", "a", SECRET, enabled=True)
    listed = store.list("alice")
    assert [s["name"] for s in listed] == ["a"]
    assert SECRET not in json.dumps(listed)
    assert store.list("bob") == []


def test_create_validates_names_and_duplicates(store):
    store.create("alice", "db_pass", SECRET)
    with pytest.raises(SecretError):
        store.create("alice", "db_pass", "other-value-123")
    with pytest.raises(SecretError):
        store.create("alice", "has space", SECRET)
    with pytest.raises(SecretError):
        store.create("alice", "empty", "")
    # Same name is fine for another owner, and in single-user mode once.
    store.create("bob", "db_pass", SECRET)
    store.create("", "solo", SECRET)
    with pytest.raises(SecretError):
        store.create("", "solo", SECRET)


def test_update_keeps_value_when_blank_and_delete_is_owner_scoped(store, session_factory):
    s = store.create("alice", "k", SECRET)
    store.update("alice", s["id"], value="", description="new desc", enabled=True)
    assert store.request_secret("alice", "k", job_id="cr-000000000000", job_running=True)["value"] == SECRET
    store.update("alice", s["id"], value="replacement-value")
    assert store.request_secret("alice", "k", job_id="cr-000000000000", job_running=True)["value"] == "replacement-value"
    assert store.update("bob", s["id"], enabled=False) is None
    assert store.delete("bob", s["id"]) is False
    assert store.delete("alice", s["id"]) is True
    assert store.list("alice") == []


# ---------------------------------------------------------------------------
# The on/off switch
# ---------------------------------------------------------------------------

def test_switched_off_secret_is_refused_and_value_never_returned(store, tmp_path):
    store.create("alice", "db_pass", SECRET, enabled=False)
    decision = store.request_secret("alice", "db_pass", job_id="cr-000000000000", job_running=True)
    assert decision["allowed"] is False
    assert "switched off" in decision["reason"]
    assert "value" not in decision
    assert SECRET not in json.dumps(decision)
    assert store.list("alice")[0]["last_used"] is None

    log = _access_log(tmp_path)
    assert log[-1]["allowed"] is False and log[-1]["name"] == "db_pass"
    assert SECRET not in (tmp_path / "secret_access.jsonl").read_text()


def test_switched_on_secret_is_handed_over_and_logged(store, tmp_path):
    store.create("alice", "db_pass", SECRET, enabled=True)
    decision = store.request_secret("alice", "db_pass", job_id="cr-000000000000", job_running=True)
    assert decision == {"allowed": True, "value": SECRET}
    assert store.list("alice")[0]["last_used"] is not None
    assert _access_log(tmp_path)[-1]["allowed"] is True
    # The log records the request, never the value.
    assert SECRET not in (tmp_path / "secret_access.jsonl").read_text()


def test_switching_off_takes_effect_on_the_next_request(store):
    s = store.create("alice", "db_pass", SECRET, enabled=True)
    assert store.request_secret("alice", "db_pass", job_id="cr-000000000000", job_running=True)["allowed"]
    store.update("alice", s["id"], enabled=False)
    assert not store.request_secret("alice", "db_pass", job_id="cr-000000000000", job_running=True)["allowed"]


def test_refused_outside_a_running_creator_job_or_for_another_owner(store, tmp_path):
    store.create("alice", "db_pass", SECRET, enabled=True)
    outside = store.request_secret("alice", "db_pass", job_id=None, job_running=False)
    assert outside["allowed"] is False and "Creator job" in outside["reason"]
    other = store.request_secret("bob", "db_pass", job_id="cr-000000000000", job_running=True)
    assert other["allowed"] is False and "No secret" in other["reason"]
    assert [e["allowed"] for e in _access_log(tmp_path)] == [False, False]


def test_get_secret_tool_through_a_running_job(session_factory, tmp_path):
    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=_hang_loop())
        off = mgr.secrets.create("alice", "off_one", "switched-off-value-1", enabled=False)
        mgr.secrets.create("alice", "on_one", SECRET, enabled=True)
        job_id = mgr.start_job("t", "u", "m", owner="alice")
        await asyncio.sleep(0.02)
        results = {
            "off": await do_get_secret('{"name": "off_one"}', owner="alice", session_id=job_id),
            "on": await do_get_secret("on_one", owner="alice", session_id=job_id),
            "wrong_owner": await do_get_secret("on_one", owner="bob", session_id=job_id),
            "not_a_job": await do_get_secret("on_one", owner="alice", session_id="some-chat-session"),
            "no_name": await do_get_secret("{}", owner="alice", session_id=job_id),
        }
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)
        # After the job ends, its id no longer works either.
        results["after_end"] = await do_get_secret("on_one", owner="alice", session_id=job_id)
        return job_id, results

    job_id, r = asyncio.run(run())
    assert r["off"]["exit_code"] == 1 and "switched off" in r["off"]["error"]
    assert "switched-off-value-1" not in json.dumps(r["off"])
    assert r["on"] == {"output": SECRET, "exit_code": 0}
    for key in ("wrong_owner", "not_a_job", "no_name", "after_end"):
        assert r[key]["exit_code"] == 1 and "output" not in r[key], key

    requests = [e for e in _audit(tmp_path, job_id) if e["type"] == "secret_request"]
    assert [(e["name"], e["allowed"]) for e in requests] == [("off_one", False), ("on_one", True)]
    audit_text = (tmp_path / "audit" / f"{job_id}.jsonl").read_text()
    assert SECRET not in audit_text and "switched-off-value-1" not in audit_text


def test_creator_run_always_offers_get_secret(session_factory):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=_hang_loop(calls))
        job_id = mgr.start_job("t", "u", "m", owner="alice")
        await asyncio.sleep(0.02)
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    assert "get_secret" in calls[0]["forced_tools"]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def test_stored_secret_values_are_redacted_from_everything_a_run_stores(session_factory, tmp_path):
    """Values of the owner's secrets — switched on or off — never reach the
    event log, audit log, report or error, even if the agent dug one up
    some other way."""
    calls = []

    async def loop(**kwargs):
        calls.append(kwargs)
        yield _sse({"type": "tool_start", "tool": "bash", "command": f"echo {SECRET}"})
        yield _sse({"type": "tool_output", "tool": "bash", "command": f"echo {SECRET}",
                    "output": f"{SECRET}\nswitched-off-value-1", "exit_code": 0})
        yield _sse({"delta": f"The password is {SECRET}."})

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=loop)
        mgr.secrets.create("alice", "on_one", SECRET, enabled=True)
        mgr.secrets.create("alice", "off_one", "switched-off-value-1", enabled=False)
        job_id = mgr.start_job("t", "u", "m", owner="alice")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    stored = json.dumps(job) + (tmp_path / "audit" / f"{job['id']}.jsonl").read_text()
    assert SECRET not in stored
    assert "switched-off-value-1" not in stored
    assert "The password is [REDACTED]." in job["report"]

    # And the hook the agent loop applies to tool results before the model
    # reads them blanks both values, known values only.
    redact = calls[0]["output_redactor"]
    out = redact({"output": f"x {SECRET} y switched-off-value-1 DB_PASSWORD=abc12345", "exit_code": 0})
    assert out["output"] == "x [REDACTED] y [REDACTED] DB_PASSWORD=abc12345"


def test_secret_handed_out_mid_run_is_redacted_afterwards(session_factory, tmp_path):
    """A secret created after the run started isn't in the initial redactor;
    get_secret adds it, so everything stored after that is scrubbed."""
    late = "late-added-secret-value"

    async def loop(**kwargs):
        mgr = creator_mode.get_active_manager()
        mgr.secrets.create("alice", "late", late, enabled=True)
        got = await do_get_secret("late", owner="alice", session_id=kwargs["session_id"])
        assert got["output"] == late
        assert kwargs["output_redactor"]({"output": late})["output"] == "[REDACTED]"
        yield _sse({"type": "tool_output", "tool": "bash", "command": "cat creds",
                    "output": f"token={late}", "exit_code": 0})
        yield _sse({"delta": f"used {late}"})

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=loop)
        job_id = mgr.start_job("t", "u", "m", owner="alice")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["status"] == "done", job["error"]
    stored = json.dumps(job) + (tmp_path / "audit" / f"{job['id']}.jsonl").read_text()
    assert late not in stored
    assert "used [REDACTED]" in job["report"]


def test_agent_loop_scrubs_tool_results_before_the_model_sees_them(monkeypatch):
    """The real agent loop with a fake model: a bash result containing a
    secret is scrubbed in the tool_output event and in what the model reads
    next round; get_secret's own result is passed through."""
    import src.agent_loop as agent_loop
    from src.creator_safety import Redactor

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    responses = iter([
        "```get_secret\non_one\n```",
        "```bash\ncat config\n```",
        "Done.",
    ])
    seen_by_model = []

    async def fake_stream(*args, **kwargs):
        # Everything the model is handed this round, whatever the call shape.
        seen_by_model.append(json.dumps([args, kwargs], default=str))
        yield f"data: {json.dumps({'delta': next(responses, 'Done.')})}\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        if block.tool_type == "get_secret":
            return "get_secret", {"output": SECRET, "exit_code": 0}
        return "bash", {"output": f"password={SECRET}", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)

    redactor = Redactor([SECRET])

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://local.test/v1", "m",
            [{"role": "user", "content": "deploy"}],
            max_rounds=3,
            relevant_tools={"bash", "get_secret"},
            output_redactor=redactor.known_obj,
        )]

    chunks = asyncio.run(collect())
    events = [json.loads(c[6:]) for c in chunks
              if c.startswith("data: ") and not c.startswith("data: [DONE]")]
    outputs = {e["tool"]: e.get("output") for e in events if e.get("type") == "tool_output"}
    assert outputs["get_secret"] == SECRET          # handing it over is the tool's job
    assert outputs["bash"] == "password=[REDACTED]"
    # Round 3 is what the model sees after the bash result.
    assert "password=[REDACTED]" in seen_by_model[2]
    assert f"password={SECRET}" not in seen_by_model[2]


# ---------------------------------------------------------------------------
# Tripwire for the files that hold every secret
# ---------------------------------------------------------------------------

def test_key_file_and_database_are_always_protected(session_factory, monkeypatch):
    from src.constants import APP_KEY_FILE
    from src.creator_safety import make_protected_action_check

    paths = creator_secrets.secret_store_tripwire_paths()
    assert APP_KEY_FILE in paths and ".app_key" in paths

    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=_hang_loop(calls))
        job_id = mgr.start_job("t", "u", "m", owner="alice")  # no protected paths configured
        await asyncio.sleep(0.02)
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    check = calls[0]["protected_action_check"]
    assert check("bash", f"cat {APP_KEY_FILE}")
    assert check("bash", "cat ../.app_key")
    assert check("bash", "ls -la") is None


def test_tripwire_includes_sqlite_file_and_sidecars(monkeypatch):
    import core.database as database
    monkeypatch.setattr(database, "_sqlite_db_path", lambda url: "/data/app.db")
    paths = creator_secrets.secret_store_tripwire_paths()
    for p in ("/data/app.db", "app.db", "/data/app.db-wal", "app.db-shm"):
        assert p in paths


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class _AuthMgr:
    is_configured = True

    def get_privileges(self, user):
        return {**DEFAULT_PRIVILEGES, "can_use_creator": user in ("alice", "bob")}


def _request(user):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_AuthMgr())),
    )


def _route(router, path, method):
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def test_secret_routes_are_write_only_owner_scoped_and_gated(session_factory, monkeypatch):
    monkeypatch.setattr(creator_routes, "require_user", lambda request: request.state.current_user)
    mgr = CreatorManager(session_factory=session_factory, agent_loop=_hang_loop())
    router = creator_routes.setup_creator_routes(mgr)
    create = _route(router, "/api/creator/secrets", "POST")
    listing = _route(router, "/api/creator/secrets", "GET")
    patch = _route(router, "/api/creator/secrets/{secret_id}", "PATCH")
    delete = _route(router, "/api/creator/secrets/{secret_id}", "DELETE")

    async def run():
        made = await create(body=SimpleNamespace(name="tok", value=SECRET, description="", enabled=False),
                            request=_request("alice"))
        listed = await listing(request=_request("alice"))
        toggled = await patch(secret_id=made["id"],
                              body=SimpleNamespace(name=None, value=None, description=None, enabled=True),
                              request=_request("alice"))
        errors = []
        for coro in (
            listing(request=_request("carol")),  # no can_use_creator
            patch(secret_id=made["id"], body=SimpleNamespace(name=None, value=None, description=None,
                                                             enabled=False), request=_request("bob")),
            delete(secret_id=made["id"], request=_request("bob")),
            create(body=SimpleNamespace(name="tok", value="x" * 10, description="", enabled=False),
                   request=_request("alice")),  # duplicate
        ):
            with pytest.raises(HTTPException) as exc:
                await coro
            errors.append(exc.value.status_code)
        bob_list = await listing(request=_request("bob"))
        return made, listed, toggled, errors, bob_list

    made, listed, toggled, errors, bob_list = asyncio.run(run())
    assert SECRET not in json.dumps([made, listed, toggled])
    assert toggled["enabled"] is True
    assert errors == [403, 404, 404, 400]
    assert bob_list == {"secrets": []}


def test_get_secret_is_registered_as_a_tool():
    from src.agent_tools import TOOL_TAGS
    from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    assert "get_secret" in TOOL_TAGS
    assert "get_secret" in KNOWN_CAPABILITY_TOOLS
    assert any(s["function"]["name"] == "get_secret" for s in FUNCTION_TOOL_SCHEMAS)
