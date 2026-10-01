"""Creator mode Phase 2: instructions, the three-failures rule, progress notes
and checkpoints, real pause/resume (with stop and the time limit while
paused), and the structured report."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.auth import DEFAULT_PRIVILEGES
from core.database import CreatorJob, CreatorSecret
from routes import creator_routes
from src import creator_mode, creator_secrets
from src.creator_mode import (
    FAILURE_LIMIT,
    CreatorBusyError,
    CreatorManager,
    CreatorNotPausedError,
    CreatorResumeError,
    parse_report_sections,
)

REPORT = """Here is my report.
## What was done
Installed foo and configured bar.
## What worked
pip install foo.
## What didn't work
apt install foo (no such package).
## What's left
Nothing.
STATUS: DONE"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import src.secret_storage as secret_storage
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr("src.creator_safety.audit_dir", lambda: tmp_path / "audit")
    monkeypatch.setattr(creator_secrets, "access_log_path", lambda: tmp_path / "secret_access.jsonl")
    monkeypatch.setattr(CreatorManager, "_retire_approval", staticmethod(lambda *a: None))
    killed = []

    async def fake_kill(session_id):
        killed.append(session_id)

    monkeypatch.setattr(creator_mode, "kill_job_shell", fake_kill)
    return killed


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'creator.db'}")
    CreatorJob.__table__.create(bind=engine)
    CreatorSecret.__table__.create(bind=engine)
    return sessionmaker(bind=engine)


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def scripted(segments, calls, ran=None):
    """A fake agent loop. Each call plays the next script. Tool steps go
    through the same hooks the real loop applies (refusal check, redaction,
    result hook), so the failure rule is exercised as in production."""
    it = iter(segments)

    async def loop(**kw):
        calls.append(kw)
        for step in next(it, [("text", "ok\nSTATUS: DONE")]):
            kind = step[0]
            if kind == "text":
                yield _sse({"delta": step[1]})
            elif kind == "sse":
                yield _sse(step[1])
            elif kind == "hang":
                await asyncio.Event().wait()
            elif kind == "tool":
                _, tool, content, result = step
                yield _sse({"type": "tool_start", "tool": tool, "command": content})
                why = kw["tool_refusal_check"](tool, content)
                if why:
                    res = {"error": why, "exit_code": 1, "blocked": True}
                else:
                    if ran is not None:
                        ran.append(content)
                    res = dict(result)
                res = kw["output_redactor"](res)
                res = kw["tool_result_hook"](tool, content, res)
                yield _sse({"type": "tool_output", "tool": tool, "command": content,
                            "output": res.get("output") or res.get("error"),
                            "exit_code": res.get("exit_code")})
        yield "data: [DONE]\n\n"
    return loop


def _approval_card(command, description="Creator mode needs your OK before bash touches the protected path /etc."):
    return ("sse", {"type": "tool_output", "tool": "bash", "ask_user": {
        "kind": "tool_approval", "approval_id": "ap1", "description": description,
        "action": {"tool": "bash", "content": command}}})


def _question(q="Which database should I use?", options=("postgres", "sqlite")):
    return ("sse", {"type": "tool_output", "tool": "ask_user", "ask_user": {
        "question": q, "options": [{"label": o} for o in options]}})


async def _wait_finished(mgr, job_id):
    for _ in range(300):
        if not mgr.is_running(job_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


async def _wait_paused(mgr, job_id):
    for _ in range(300):
        if mgr.is_paused(job_id):
            return mgr.get_job(job_id)
        await asyncio.sleep(0.01)
    raise AssertionError("job did not pause")


FAIL = {"error": "E: Unable to locate package foo", "exit_code": 100}


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------

def test_system_prompt_carries_the_creator_rules(session_factory):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    prompt = calls[0]["messages"][0]["content"]
    for phrase in ("different approach", "PROGRESS:", "ask_user", "only they can",
                   "What didn't work", "STATUS: DONE", "STATUS: BLOCKED"):
        assert phrase in prompt


# ---------------------------------------------------------------------------
# The three-failures rule
# ---------------------------------------------------------------------------

def test_same_command_failing_the_same_way_three_times_is_then_refused(session_factory, tmp_path):
    calls, ran = [], []
    script = [[
        ("tool", "bash", "apt install foo", FAIL),
        ("tool", "bash", "apt  install foo", FAIL),  # whitespace doesn't make it different
        ("tool", "bash", "apt install foo", FAIL),
        ("tool", "bash", "apt install foo", FAIL),  # 4th: refused, not run
        ("tool", "bash", "pip install foo", {"output": "ok", "exit_code": 0}),
        ("text", REPORT),
    ]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls, ran))
        job_id = mgr.start_job("install foo", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert ran == ["apt install foo", "apt  install foo", "apt install foo", "pip install foo"]
    outputs = [e for e in job["events"] if e["type"] == "tool_output"]
    assert "It will be refused from now on" in outputs[2]["output"]
    assert "Refused by Creator mode" in outputs[3]["output"]
    assert [e["type"] for e in job["events"]].count("failure_limit") == 1
    failures = job["state"]["report"]["failures"]
    assert len(failures) == 1 and "apt install foo" in failures[0]
    assert "Failures recorded by Creator" in job["report"]


def test_different_failures_or_commands_dont_trip_the_rule(session_factory):
    calls, ran = [], []
    other = {"error": "E: Could not get lock /var/lib/dpkg/lock", "exit_code": 100}
    script = [[
        ("tool", "bash", "apt install foo", FAIL),
        ("tool", "bash", "apt install foo", other),  # same command, different failure
        ("tool", "bash", "apt install foo", FAIL),
        ("tool", "bash", "apt install bar", FAIL),
        ("tool", "bash", "apt install foo", other),
        ("text", "STATUS: DONE"),
    ]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls, ran))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert len(ran) == 5
    assert "failure_limit" not in [e["type"] for e in job["events"]]


def test_failure_numbers_dont_make_failures_different(session_factory):
    calls, ran = [], []
    script = [[("tool", "bash", "curl x", {"error": f"timeout after {n}ms (pid {n * 7})", "exit_code": 28})
               for n in (101, 202, 303, 404)] + [("text", "STATUS: DONE")]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls, ran))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    assert len(ran) == FAILURE_LIMIT


def test_three_failures_rule_in_the_real_agent_loop(monkeypatch):
    """The hooks wired into the real loop: the 4th identical call is not
    executed and the model is told why."""
    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    responses = iter(["```bash\napt install foo\n```"] * 4 + ["Done."])
    executed = []

    async def fake_stream(*args, **kwargs):
        yield f"data: {json.dumps({'delta': next(responses, 'Done.')})}\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        executed.append(block.content)
        return "bash", dict(FAIL)

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)

    counts, refused = {}, set()

    def hook(tool, content, result):
        key = (tool, content.strip())
        counts[key] = counts.get(key, 0) + 1
        if counts[key] == 3:
            refused.add(key)
        return result

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://local.test/v1", "m", [{"role": "user", "content": "install foo"}],
            max_rounds=5, relevant_tools={"bash"},
            # Untrusted-gate approvals are not what this test is about.
            untrusted_gate_bypassed=True,
            tool_result_hook=hook,
            tool_refusal_check=lambda t, c: "refused: failed 3 times" if (t, c.strip()) in refused else None,
        )]

    chunks = asyncio.run(collect())
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    outputs = [e for e in events if e.get("type") == "tool_output"]
    assert len(executed) == 3
    assert len(outputs) == 4 and "refused: failed 3 times" in outputs[3]["output"]


# ---------------------------------------------------------------------------
# Progress notes and checkpoints
# ---------------------------------------------------------------------------

def test_progress_notes_are_saved_and_carried_across_a_checkpoint(session_factory, monkeypatch):
    monkeypatch.setattr(creator_mode, "AUTO_NOTE_EVERY", 2)
    calls = []
    script = [
        [("text", "Starting.\nPROGRESS: checked disk space, 40G free\n"),
         ("tool", "bash", "df -h", {"output": "40G", "exit_code": 0}),
         ("text", "PROGRESS: apt is broken, will try pip\n"),
         ("tool", "bash", "pip --version", {"output": "pip 24", "exit_code": 0}),
         ("sse", {"type": "rounds_exhausted", "rounds": 30})],
        [("text", "PROGRESS: done with pip\n" + REPORT)],
    ]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("set up foo", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["status"] == "done"
    notes = [n["text"] for n in job["state"]["notes"]]
    assert "checked disk space, 40G free" in notes
    assert "apt is broken, will try pip" in notes
    assert "done with pip" in notes
    assert any(n["source"] == "auto" and n["text"].startswith("Checkpoint: 2 tool calls") for n in job["state"]["notes"])
    # The second segment starts from a rebuilt context: task, notes, commands.
    second = calls[1]["messages"]
    assert second[1] == {"role": "user", "content": "set up foo"}
    rebuilt = second[-1]["content"]
    assert "Checkpoint" in rebuilt
    assert "apt is broken, will try pip" in rebuilt
    assert "df -h" in rebuilt and "pip --version" in rebuilt
    assert job["state"]["segments"] == 2


# ---------------------------------------------------------------------------
# Pause and resume
# ---------------------------------------------------------------------------

def test_question_pauses_and_answer_resumes(session_factory):
    calls = []
    script = [[("text", "I need to know."), _question()], [("text", REPORT)]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("set up a db", "u", "m", owner="alice")
        paused = await _wait_paused(mgr, job_id)
        with pytest.raises(CreatorBusyError):  # a paused job holds the slot
            mgr.start_job("another", "u", "m", owner="bob")
        with pytest.raises(CreatorResumeError):  # a question takes an answer, not a decision
            mgr.resume_job(job_id, decision="approve_once")
        mgr.resume_job(job_id, answer="postgres, please")
        with pytest.raises(CreatorNotPausedError):
            mgr.resume_job(job_id, answer="again")
        await _wait_finished(mgr, job_id)
        return paused, mgr.get_job(job_id)

    paused, job = asyncio.run(run())
    assert paused["status"] == "paused"
    assert paused["state"]["pause"]["kind"] == "question"
    assert paused["state"]["pause"]["question"] == "Which database should I use?"
    assert paused["state"]["pause"]["options"] == ["postgres", "sqlite"]
    assert job["status"] == "done"
    assert "postgres, please" in calls[1]["messages"][-1]["content"]
    types = [e["type"] for e in job["events"]]
    assert types.index("paused") < types.index("resumed")


def test_status_blocked_line_pauses_too(session_factory):
    calls = []
    script = [[("text", "I can't reach the server.\nSTATUS: BLOCKED: need the VPN password")],
              [("text", REPORT)]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("t", "u", "m")
        paused = await _wait_paused(mgr, job_id)
        mgr.resume_job(job_id, answer="")
        await _wait_finished(mgr, job_id)
        return paused, mgr.get_job(job_id)

    paused, job = asyncio.run(run())
    assert paused["state"]["pause"] == {**paused["state"]["pause"], "kind": "blocked",
                                       "question": "need the VPN password"}
    assert "without an answer" in calls[1]["messages"][-1]["content"]
    assert job["status"] == "done"


def test_protected_path_pause_approve_once_runs_exactly_that_action(session_factory, tmp_path):
    calls, executed = [], []
    script = [[_approval_card("rm /etc/hosts.bak")], [("text", REPORT)]]

    async def executor(block, **kw):
        ctx = kw["security_context"]
        executed.append({
            "tool": block.tool_type, "content": block.content, "session_id": kw["session_id"],
            "this_allowed": ctx.decision_for(block.tool_type, block.content).allowed,
            "other_allowed": ctx.decision_for("bash", "rm /etc/passwd").allowed,
        })
        return "bash", {"output": "removed", "exit_code": 0}

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls),
                             tool_executor=executor)
        job_id = mgr.start_job("clean up", "u", "m", protected_paths=["/etc"])
        paused = await _wait_paused(mgr, job_id)
        with pytest.raises(CreatorResumeError):  # protected: no blanket approval
            mgr.resume_job(job_id, decision="approve_job")
        mgr.resume_job(job_id, decision="approve_once")
        await _wait_finished(mgr, job_id)
        return job_id, paused, mgr.get_job(job_id)

    job_id, paused, job = asyncio.run(run())
    pause = paused["state"]["pause"]
    assert pause["kind"] == "approval" and pause["protected"] is True
    assert pause["action"] == {"tool": "bash", "command": "rm /etc/hosts.bak"}
    assert pause["choices"] == ["approve_once", "deny"]
    assert executed == [{"tool": "bash", "content": "rm /etc/hosts.bak", "session_id": job_id,
                         "this_allowed": True, "other_allowed": False}]
    cont = calls[1]["messages"][-1]["content"]
    assert "APPROVED" in cont and "removed" in cont
    assert calls[1]["untrusted_gate_bypassed"] is False
    assert job["status"] == "done"
    cmds = job["state"]["report"]["commands"]
    assert cmds[-1]["command"] == "rm /etc/hosts.bak" and cmds[-1]["approved"] is True
    audit = (tmp_path / "audit" / f"{job_id}.jsonl").read_text()
    assert '"type": "paused"' in audit and '"type": "resumed"' in audit


def test_untrusted_gate_approve_job_lifts_the_gate_for_later_segments(session_factory):
    calls, executed = [], []
    card = _approval_card("make deploy", "External untrusted context has already influenced this run.")
    script = [[card], [("text", REPORT)]]

    async def executor(block, **kw):
        executed.append(block.content)
        return "bash", {"output": "deployed", "exit_code": 0}

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls),
                             tool_executor=executor)
        job_id = mgr.start_job("deploy", "u", "m")
        paused = await _wait_paused(mgr, job_id)
        mgr.resume_job(job_id, decision="approve_job")
        await _wait_finished(mgr, job_id)
        return paused

    paused = asyncio.run(run())
    assert paused["state"]["pause"]["choices"] == ["approve_once", "approve_job", "deny"]
    assert executed == ["make deploy"]
    assert calls[0]["untrusted_gate_bypassed"] is False
    assert calls[1]["untrusted_gate_bypassed"] is True


def test_approve_untrusted_at_start_lifts_the_gate_from_the_first_segment(session_factory, tmp_path):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls))
        lifted = mgr.start_job("t", "u", "m", approve_untrusted=True)
        await _wait_finished(mgr, lifted)
        default = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, default)
        return lifted, mgr.get_job(lifted)

    lifted, job = asyncio.run(run())
    assert calls[0]["untrusted_gate_bypassed"] is True
    assert calls[1]["untrusted_gate_bypassed"] is False  # off unless asked for
    assert job["state"]["gate_bypassed"] is True
    start = json.loads((tmp_path / "audit" / f"{lifted}.jsonl").read_text().splitlines()[0])
    assert start["approve_untrusted"] is True


def test_approve_untrusted_never_lifts_protected_paths(session_factory):
    """With the gate lifted up front, a protected path still pauses and still
    only offers approve_once."""
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory,
                             agent_loop=scripted([[_approval_card("rm /etc/hosts")]], calls))
        job_id = mgr.start_job("t", "u", "m", protected_paths=["/etc"], approve_untrusted=True)
        paused = await _wait_paused(mgr, job_id)
        check = calls[0]["protected_action_check"]
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)
        return paused, check

    paused, check = asyncio.run(run())
    assert check("bash", "rm /etc/hosts")  # the loop still gets the protected check
    assert paused["state"]["pause"]["choices"] == ["approve_once", "deny"]


def test_start_route_passes_approve_untrusted(session_factory, monkeypatch):
    monkeypatch.setattr(creator_routes, "require_user", lambda request: request.state.current_user)
    monkeypatch.setattr(creator_routes, "_resolve_creator_endpoint", lambda u, e, m: ("u", "m", {}))
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    calls = []
    mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls))
    start = _route(creator_routes.setup_creator_routes(mgr), "/api/creator/start", "POST")

    async def run():
        out = await start(body=SimpleNamespace(task="t", endpoint_id=None, model=None, max_minutes=None,
                                               approve_untrusted=True), request=_request("alice"))
        await _wait_finished(mgr, out["job_id"])
        return out

    out = asyncio.run(run())
    assert out["approve_untrusted"] is True
    assert calls[0]["untrusted_gate_bypassed"] is True


def test_deny_does_not_run_the_action(session_factory):
    calls, executed = [], []
    script = [[_approval_card("rm -rf /etc/nginx")], [("text", REPORT)]]

    async def executor(block, **kw):
        executed.append(block.content)
        return "bash", {"output": "", "exit_code": 0}

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls),
                             tool_executor=executor)
        job_id = mgr.start_job("t", "u", "m", protected_paths=["/etc"])
        await _wait_paused(mgr, job_id)
        mgr.resume_job(job_id, decision="deny", answer="use a backup instead")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert executed == []
    cont = calls[1]["messages"][-1]["content"]
    assert "DENIED" in cont and "use a backup instead" in cont
    assert job["status"] == "done"


def test_stop_while_paused(session_factory, isolated):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory,
                             agent_loop=scripted([[_question()]], calls))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_paused(mgr, job_id)
        assert mgr.stop_job(job_id) is True
        await _wait_finished(mgr, job_id)
        with pytest.raises(CreatorNotPausedError):
            mgr.resume_job(job_id, answer="too late")
        # The slot is free again.
        other = mgr.start_job("next", "u", "m")
        await _wait_finished(mgr, other)
        return job_id, mgr.get_job(job_id)

    job_id, job = asyncio.run(run())
    assert job["status"] == "stopped"
    assert job["state"]["pause"] is None
    assert job_id in isolated  # its shell was killed
    assert len(calls) == 2     # the stopped job never ran another segment


def test_time_limit_counts_paused_time(session_factory, monkeypatch):
    monkeypatch.setattr(creator_mode, "_SECONDS_PER_MINUTE", 0.1)
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory,
                             agent_loop=scripted([[_question()]], calls))
        job_id = mgr.start_job("t", "u", "m", max_minutes=1)
        paused = await _wait_paused(mgr, job_id)
        await _wait_finished(mgr, job_id)  # nobody answers
        return paused, mgr.get_job(job_id)

    paused, job = asyncio.run(run())
    assert paused["state"]["deadline_at"]
    assert job["status"] == "timeout"
    assert "What's left" in job["report"] and "timeout" in job["report"]


def test_creator_sends_no_temperature_like_chat(session_factory):
    """Found in the smoke test: the loop's 0.3 default made Anthropic return
    400 'temperature is deprecated' for claude-sonnet-5-5."""
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls))
        job_id = mgr.start_job("t", "u", "claude-sonnet-5-5")
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    assert "temperature" in calls[0] and calls[0]["temperature"] is None


def test_none_temperature_is_left_out_of_the_anthropic_request():
    from src.llm_core import _build_anthropic_payload
    payload = _build_anthropic_payload(
        "claude-sonnet-5-5", [{"role": "user", "content": "hi"}], None, 100)
    # Not even null: the API rejects "temperature": null (second smoke run).
    assert "temperature" not in payload


def test_failed_model_request_ends_job_as_error_not_done(session_factory):
    """Found in the smoke test: a provider failure (agent_terminal failed)
    used to be recorded as a finished job with an empty report."""
    calls = []
    script = [[
        ("sse", {"type": "agent_terminal", "data": {
            "failed": True, "failure": {"status": 400, "message": "model not found: claude-x"}}}),
        ("sse", {"error": "HTTP 400 from provider"}),
    ]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["status"] == "error"
    assert "model not found: claude-x" in job["error"]
    assert "Stopped: error" in job["report"]


def test_raw_sse_error_on_the_first_request_ends_job_as_error(session_factory):
    """Found in the second smoke run: a first-round provider failure arrives
    as a raw `event: error` chunk, with no agent_terminal event."""
    calls = []
    raw = ('event: error\ndata: {"status": 400, "text": "Anthropic returned HTTP 400: '
           '`temperature` is deprecated for this model.", "raw": "{}"}\n\n')

    async def loop(**kw):
        calls.append(kw)
        yield raw

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=loop)
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["status"] == "error"
    assert "temperature` is deprecated" in job["error"]


def test_orphaned_paused_job_marked_interrupted(session_factory):
    db = session_factory()
    db.add(CreatorJob(id="cr-bbbbbbbbbbbb", owner="alice", task="t", status="paused"))
    db.commit()
    db.close()
    mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], []))
    assert mgr.get_job("cr-bbbbbbbbbbbb")["status"] == "interrupted"


# ---------------------------------------------------------------------------
# Structured report
# ---------------------------------------------------------------------------

def test_structured_report_has_every_section_and_the_exact_commands(session_factory):
    calls = []
    script = [[
        ("tool", "bash", "apt install foo", FAIL),
        ("tool", "bash", "pip install foo", {"output": "Successfully installed foo", "exit_code": 0}),
        ("text", REPORT),
    ]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("install foo", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    data = job["state"]["report"]
    assert data["asked"] == "install foo"
    assert data["status"] == "done"
    assert data["done"] == "Installed foo and configured bar."
    assert data["worked"] == "pip install foo."
    assert data["didnt_work"] == "apt install foo (no such package)."
    assert data["left"] == "Nothing."
    assert [(c["command"], c["ok"], c["exit_code"]) for c in data["commands"]] == [
        ("apt install foo", False, 100), ("pip install foo", True, 0)]
    assert "STATUS" not in json.dumps({k: data[k] for k in ("done", "worked", "didnt_work", "left", "other")})

    report = job["report"]
    for heading in ("## What was asked", "## What was done", "## What worked",
                    "## What didn't work", "## What's left", "## Exact commands run", "## Progress notes"):
        assert heading in report
    assert "`apt install foo` — failed, exit 100" in report
    assert "`pip install foo` — ok" in report


def test_report_fills_gaps_when_the_agent_skips_sections(session_factory):
    calls = []
    script = [[("text", "All good, finished.")]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert "_The agent did not write this section" in job["report"]
    assert "## Other notes from the agent\n\nAll good, finished." in job["report"]


def test_report_logs_file_writes_as_target_and_size_and_keeps_commands_exact(session_factory):
    """From the first real run: a write_file line held the whole file."""
    calls = []
    body = "# Folder report\n\nTotal files: 0\n"
    script = [[
        ("tool", "write_file", f"/app/data/agent_workspace/report.md\n{body}", {"output": "Wrote", "exit_code": 0}),
        ("tool", "bash", "ls -la /tmp &&\n  echo done", {"output": "done", "exit_code": 0}),
        ("text", REPORT),
    ]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    cmds = [c["command"] for c in job["state"]["report"]["commands"]]
    assert cmds[0] == f"/app/data/agent_workspace/report.md (+{len(body)} chars of content)"
    assert cmds[1] == "ls -la /tmp && echo done"
    assert "Total files" not in job["report"]


def test_progress_lines_are_not_repeated_under_other_notes(session_factory):
    """From the first real run: the last PROGRESS line showed up twice."""
    calls = []
    script = [[("text", "PROGRESS: checked report.md\n" + REPORT)]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert job["report"].count("checked report.md") == 1
    assert job["state"]["notes"][-1]["text"] == "checked report.md"


def test_heading_run_into_a_progress_line_is_still_a_section(session_factory):
    """From the /var/www/html run: 'PROGRESS: ...layout.## What was done'
    swallowed the heading into the note and emptied the section."""
    calls = []
    text = ("PROGRESS: The grep returned nothing. I'll look at the layout.## What was done\n"
            "Edited one paragraph.\n## What worked\nByte-level replace.\n"
            "## What didn't work\ngrep <p.\n## What's left\nNothing. STATUS: DONE")
    script = [[("text", text)]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    data = job["state"]["report"]
    assert data["done"] == "Edited one paragraph."
    assert data["left"] == "Nothing."
    assert job["state"]["notes"][-1]["text"] == "The grep returned nothing. I'll look at the layout."
    assert "STATUS" not in job["report"]


def test_inline_status_blocked_still_pauses(session_factory):
    calls = []
    script = [[("text", "I can't continue. STATUS: BLOCKED: need the SSH password")], [("text", REPORT)]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("t", "u", "m")
        paused = await _wait_paused(mgr, job_id)
        mgr.resume_job(job_id, answer="")
        await _wait_finished(mgr, job_id)
        return paused

    paused = asyncio.run(run())
    assert paused["state"]["pause"]["question"] == "need the SSH password"


def test_parse_report_sections_accepts_heading_variants():
    out = parse_report_sections(
        "intro\n### What Was Done\na\n## **What did not work**\nb\n# What is left\nc\nSTATUS: DONE")
    assert out == {"other": "intro", "done": "a", "didnt_work": "b", "left": "c"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class _AuthMgr:
    is_configured = True

    def get_privileges(self, user):
        return {**DEFAULT_PRIVILEGES, "can_use_creator": True}


def _request(user):
    async def connected():
        return False
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user), headers={},
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_AuthMgr())),
        is_disconnected=connected,
    )


def _route(router, path, method):
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def test_resume_status_and_report_routes(session_factory, monkeypatch):
    monkeypatch.setattr(creator_routes, "require_user", lambda request: request.state.current_user)
    calls, executed = [], []

    async def executor(block, **kw):
        executed.append(block.content)
        return "bash", {"output": "done", "exit_code": 0}

    mgr = CreatorManager(session_factory=session_factory,
                         agent_loop=scripted([[_approval_card("rm /etc/x")], [("text", REPORT)]], calls),
                         tool_executor=executor)
    router = creator_routes.setup_creator_routes(mgr)
    status = _route(router, "/api/creator/status/{job_id}", "GET")
    resume = _route(router, "/api/creator/resume/{job_id}", "POST")
    report = _route(router, "/api/creator/report/{job_id}", "GET")

    async def run():
        job_id = mgr.start_job("t", "u", "m", owner="alice", protected_paths=["/etc"])
        await _wait_paused(mgr, job_id)
        st = await status(job_id=job_id, request=_request("alice"), since=0)
        errors = []
        for user, body in (("bob", {"decision": "approve_once"}),        # not their job
                           ("alice", {"decision": "approve_job"}),       # not allowed here
                           ("alice", {"decision": "maybe"})):            # nonsense
            with pytest.raises(HTTPException) as exc:
                await resume(job_id=job_id, body=SimpleNamespace(answer=None, **body),
                             request=_request(user))
            errors.append(exc.value.status_code)
        with pytest.raises(HTTPException) as exc:
            await report(job_id=job_id, request=_request("alice"))
        errors.append(exc.value.status_code)  # no report while paused
        out = await resume(job_id=job_id, body=SimpleNamespace(decision="approve_once", answer=None),
                           request=_request("alice"))
        await _wait_finished(mgr, job_id)
        with pytest.raises(HTTPException) as exc:
            await resume(job_id=job_id, body=SimpleNamespace(decision="approve_once", answer=None),
                         request=_request("alice"))
        errors.append(exc.value.status_code)  # not paused any more
        rep = await report(job_id=job_id, request=_request("alice"))
        return st, errors, out, rep

    st, errors, out, rep = asyncio.run(run())
    assert st["status"] == "paused"
    assert st["pause"]["kind"] == "approval" and st["pause"]["action"]["command"] == "rm /etc/x"
    assert st["deadline_at"]
    assert errors == [404, 400, 400, 409, 409]
    assert out == {"resumed": True, "kind": "approval"}
    assert executed == ["rm /etc/x"]
    assert rep["report_data"]["done"] == "Installed foo and configured bar."
    assert "## Exact commands run" in rep["report"]
