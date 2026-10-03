"""Creator mode Phase 6b: host_exec inside Creator jobs.

When the host helper answers at job start, the job is offered host_exec and
told about the host. Every host command pauses for approval ("host" scope)
until the user allows all host commands for the job; that never lifts
protected paths or the untrusted-content gate for other tools. The helper
itself is tested in test_creator_host_helper.py; here it's a fake.
"""

import asyncio
import json

import pytest

from src import creator_host_helper
from src.creator_host_helper import HelperError, format_run_result, parse_host_exec_args
from src.creator_mode import CREATOR_HOST_PROMPT, HOST_EXEC_TOOL, HOST_GATE_REASON, CreatorManager
from src.tool_capabilities import ToolRunSecurityContext
# Shared fakes and fixtures (the autouse `isolated` fixture applies here too).
from test_creator_phase2 import (  # noqa: F401
    REPORT,
    _sse,
    _wait_finished,
    _wait_paused,
    isolated,
    scripted,
    session_factory,
)

HOST_CMD = json.dumps({"command": "systemctl reload apache2"})


class FakeHelper:
    def __init__(self, capabilities=("hello", "run"), reply=None, error=None):
        self.capabilities = list(capabilities)
        self.reply = reply or {"ok": True, "type": "run", "exit_code": 0, "stdout": "reloaded\n",
                               "stderr": "", "timed_out": False, "truncated": False}
        self.error = error
        self.runs = []

    async def hello(self):
        return {"ok": True, "reply": {"user": "creator", "capabilities": self.capabilities}}

    async def run(self, command, timeout_s=120, redact=None):
        self.runs.append({"command": command, "timeout_s": timeout_s, "redact": list(redact or [])})
        if self.error:
            raise HelperError(self.error)
        return self.reply


def _host_card(content=HOST_CMD):
    return ("sse", {"type": "tool_output", "tool": HOST_EXEC_TOOL, "ask_user": {
        "kind": "tool_approval", "approval_id": "ap1", "description": HOST_GATE_REASON,
        "action": {"tool": HOST_EXEC_TOOL, "content": content}}})


def _system(call):
    return call["messages"][0]["content"]


# ---------------------------------------------------------------------------
# Offered only when the helper can run commands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("helper,offered", [
    (None, False),                              # no socket (conftest points the probe nowhere)
    (FakeHelper(capabilities=("hello",)), False),   # a 6a helper: hello only
    (FakeHelper(), True),
])
def test_host_exec_is_offered_only_when_the_helper_can_run(session_factory, tmp_path, helper, offered):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls),
                             host_helper=helper)
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return job_id

    job_id = asyncio.run(run())
    assert (HOST_EXEC_TOOL in calls[0]["relevant_tools"]) is offered
    assert (HOST_EXEC_TOOL in calls[0]["forced_tools"]) is offered
    assert (CREATOR_HOST_PROMPT in _system(calls[0])) is offered
    probe = [json.loads(line) for line in (tmp_path / "audit" / f"{job_id}.jsonl").read_text().splitlines()
             if '"host_probe"' in line][0]
    assert probe["ok"] is offered


def test_every_host_command_is_gated_until_allow_all(session_factory):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls),
                             host_helper=FakeHelper())
        job_id = mgr.start_job("t", "u", "m", protected_paths=["/etc"])
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    check, approved = calls[0]["protected_action_check"], calls[0]["caller_approved_check"]
    assert check(HOST_EXEC_TOOL, HOST_CMD) == HOST_GATE_REASON
    assert approved(HOST_EXEC_TOOL, HOST_CMD) is False
    assert check("bash", "ls") is None                     # container tools: unchanged
    assert "protected path /etc" in check(HOST_EXEC_TOOL, '{"command": "cat /etc/hosts"}')


# ---------------------------------------------------------------------------
# The host-scope pause
# ---------------------------------------------------------------------------

def _host_pause_run(session_factory, decision, protected_paths=()):
    calls, executed = [], []
    script = [[_host_card()], [("text", REPORT)]]

    async def executor(block, **kw):
        executed.append({"tool": block.tool_type, "content": block.content,
                         "allowed": kw["security_context"].decision_for(block.tool_type, block.content).allowed})
        return "host_exec", {"output": "reloaded", "exit_code": 0}

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls),
                             tool_executor=executor, host_helper=FakeHelper())
        job_id = mgr.start_job("reload apache", "u", "m", protected_paths=list(protected_paths))
        paused = await _wait_paused(mgr, job_id)
        mgr.resume_job(job_id, decision=decision)
        await _wait_finished(mgr, job_id)
        return paused, mgr.get_job(job_id)

    paused, job = asyncio.run(run())
    return calls, executed, paused["state"]["pause"], job


def test_allow_all_lifts_only_the_host_gate(session_factory):
    calls, executed, pause, job = _host_pause_run(session_factory, "approve_job")
    assert pause["scope"] == "host" and pause["protected"] is False
    assert pause["choices"] == ["approve_once", "approve_job", "deny"]
    assert executed == [{"tool": HOST_EXEC_TOOL, "content": HOST_CMD, "allowed": True}]
    later = calls[1]
    assert later["protected_action_check"](HOST_EXEC_TOOL, '{"command": "ls"}') is None
    assert later["caller_approved_check"](HOST_EXEC_TOOL, '{"command": "ls"}') is True
    assert later["caller_approved_check"]("bash", "ls") is False
    # The untrusted-content gate for everything else stays as it was.
    assert later["untrusted_gate_bypassed"] is False
    assert "all further host commands" in later["messages"][-1]["content"]
    assert job["status"] == "done"


def test_allow_once_keeps_asking(session_factory):
    calls, executed, pause, job = _host_pause_run(session_factory, "approve_once")
    assert len(executed) == 1
    later = calls[1]
    assert later["protected_action_check"](HOST_EXEC_TOOL, '{"command": "ls"}') == HOST_GATE_REASON
    assert later["caller_approved_check"](HOST_EXEC_TOOL, '{"command": "ls"}') is False


def test_deny_runs_nothing(session_factory):
    calls, executed, pause, job = _host_pause_run(session_factory, "deny")
    assert executed == []
    assert "DENIED" in calls[1]["messages"][-1]["content"]


def test_protected_path_in_a_host_command_is_still_once_only(session_factory):
    calls, executed = [], []
    card = _host_card(json.dumps({"command": "cat /etc/apache2/apache2.conf"}))

    async def executor(block, **kw):
        executed.append(block.content)
        return "host_exec", {"output": "x", "exit_code": 0}

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([[card]], calls),
                             tool_executor=executor, host_helper=FakeHelper())
        job_id = mgr.start_job("t", "u", "m", protected_paths=["/etc"])
        paused = await _wait_paused(mgr, job_id)
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)
        return paused["state"]["pause"]

    pause = asyncio.run(run())
    assert pause["scope"] == "protected" and pause["choices"] == ["approve_once", "deny"]


def test_security_context_caller_approval_never_beats_a_protected_path():
    ctx = ToolRunSecurityContext(
        external_untrusted_context_seen=True,
        protected_action_check=lambda t, c: "protected" if "/etc" in str(c) else None,
        caller_approved_check=lambda t, c: t == HOST_EXEC_TOOL,
    )
    assert ctx.decision_for(HOST_EXEC_TOOL, "ls /srv").allowed is True      # skips the untrusted gate
    assert ctx.decision_for(HOST_EXEC_TOOL, "cat /etc/x").allowed is False  # protected wins
    assert ctx.decision_for("bash", "ls").allowed is False                  # others: untrusted gate


# ---------------------------------------------------------------------------
# run_on_host / the tool entry point
# ---------------------------------------------------------------------------

def test_host_exec_runs_through_the_job_with_its_secrets_blanked(session_factory):
    key = "endpoint-api-key-123456789"
    helper = FakeHelper()

    async def run():
        mgr = CreatorManager(session_factory=session_factory,
                             agent_loop=scripted([[_host_card()]], []), host_helper=helper)
        job_id = mgr.start_job("t", "u", "m", owner="alice", headers={"Authorization": f"Bearer {key}"})
        await _wait_paused(mgr, job_id)   # alive (paused counts as running)
        mine = await creator_host_helper.do_host_exec(
            '{"command": "apachectl -t", "timeout_s": 30}', owner="alice", session_id=job_id)
        other = await creator_host_helper.do_host_exec("ls", owner="bob", session_id=job_id)
        nojob = await creator_host_helper.do_host_exec("ls", owner="alice", session_id="cr-000000000000")
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)
        return mine, other, nojob

    mine, other, nojob = asyncio.run(run())
    assert mine == {"output": "reloaded", "exit_code": 0}
    assert helper.runs[0]["command"] == "apachectl -t" and helper.runs[0]["timeout_s"] == 30
    assert key in helper.runs[0]["redact"]
    assert other["exit_code"] == 1 and "running Creator job" in other["error"]
    assert nojob["exit_code"] == 1 and len(helper.runs) == 1


@pytest.mark.parametrize("helper,expect", [
    (None, "isn't connected"),
    (FakeHelper(error="Couldn't connect to the helper: No such file"), "Host helper: Couldn't connect"),
    (FakeHelper(reply={"ok": False, "error": "busy: another command is running"}), "refused: busy"),
])
def test_host_exec_errors_are_plain(session_factory, helper, expect):
    async def run():
        # Offer it (probe ok) but let run() fail, or no helper at all.
        probe = helper or FakeHelper(capabilities=("hello",))
        mgr = CreatorManager(session_factory=session_factory,
                             agent_loop=scripted([[_host_card()]], []), host_helper=probe)
        job_id = mgr.start_job("t", "u", "m", owner="alice")
        await _wait_paused(mgr, job_id)
        out = await creator_host_helper.do_host_exec("ls", owner="alice", session_id=job_id)
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)
        return out

    out = asyncio.run(run())
    assert out["exit_code"] == 1 and expect in out["error"]


def test_tool_arguments_and_result_format():
    assert parse_host_exec_args('{"command": "ls -l", "timeout_s": 9999}') == ("ls -l", 600)
    assert parse_host_exec_args("uptime") == ("uptime", 120)
    assert parse_host_exec_args({"command": "df", "timeout_s": "x"}) == ("df", 120)
    with pytest.raises(ValueError):
        parse_host_exec_args('{"timeout_s": 5}')
    out = format_run_result({"exit_code": 2, "stdout": "a\n", "stderr": "b\n", "timed_out": False})
    assert out == {"output": "a\n[stderr]\nb", "exit_code": 2}
    slow = format_run_result({"exit_code": -15, "stdout": "", "stderr": "", "timed_out": True, "duration_s": 5})
    assert slow["exit_code"] == -15 and "time limit" in slow["output"]
    killed_clean = format_run_result({"exit_code": 0, "stdout": "", "stderr": "", "timed_out": True})
    assert killed_clean["exit_code"] == 124
    cut = format_run_result({"exit_code": 0, "stdout": "x", "stderr": "", "truncated": True,
                             "stdout_bytes": 999999, "stderr_bytes": 0})
    assert "output cut" in cut["output"]


# ---------------------------------------------------------------------------
# Through the real agent loop
# ---------------------------------------------------------------------------

def _real_loop_run(monkeypatch, protected, approved):
    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(agent_loop, "create_pending_tool_approval", lambda **kw: {"approval_id": "ap1"}, raising=False)
    responses = iter([f"```host_exec\n{HOST_CMD}\n```", "Done."])
    executed = []

    async def fake_stream(*args, **kwargs):
        yield f"data: {json.dumps({'delta': next(responses, 'Done.')})}\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        executed.append((block.tool_type, block.content))
        return "host_exec", {"output": "reloaded", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://local.test/v1", "m", [{"role": "user", "content": "reload apache"}],
            max_rounds=3, relevant_tools={HOST_EXEC_TOOL}, forced_tools={HOST_EXEC_TOOL},
            # As after a first command in a real run: the untrusted gate is armed.
            external_untrusted_context_seen=True,
            protected_action_check=lambda t, c: HOST_GATE_REASON if (protected and t == HOST_EXEC_TOOL) else None,
            caller_approved_check=lambda t, c: approved and t == HOST_EXEC_TOOL,
        )]

    chunks = asyncio.run(collect())
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    return executed, events


def test_real_loop_holds_a_host_command_for_approval(monkeypatch):
    executed, events = _real_loop_run(monkeypatch, protected=True, approved=False)
    assert executed == []
    asks = [e["ask_user"] for e in events if isinstance(e.get("ask_user"), dict)]
    assert asks and asks[0]["kind"] == "tool_approval"
    assert asks[0]["action"]["tool"] == HOST_EXEC_TOOL
    assert asks[0]["description"] == HOST_GATE_REASON


def test_real_loop_runs_a_host_command_after_allow_all(monkeypatch):
    # Allow-all: the host gate is off and the caller's approval covers the
    # armed untrusted gate, so the command runs without a new card.
    executed, events = _real_loop_run(monkeypatch, protected=False, approved=True)
    assert executed and executed[0][0] == HOST_EXEC_TOOL
    assert not [e for e in events if isinstance(e.get("ask_user"), dict)]


def test_real_loop_without_caller_approval_still_hits_the_untrusted_gate(monkeypatch):
    executed, events = _real_loop_run(monkeypatch, protected=False, approved=False)
    assert executed == []
    asks = [e["ask_user"] for e in events if isinstance(e.get("ask_user"), dict)]
    assert asks and asks[0]["action"]["tool"] == HOST_EXEC_TOOL
    assert "External untrusted context" in asks[0]["description"]


# ---------------------------------------------------------------------------
# Found in the first host run (2026-10-02)
# ---------------------------------------------------------------------------

def test_creator_turns_teacher_escalation_off(session_factory):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    assert calls[0]["teacher_escalation"] is False


@pytest.mark.parametrize("flag,expect_teacher", [(None, True), (False, False)])
def test_real_loop_skips_the_teacher_when_asked(monkeypatch, flag, expect_teacher):
    import src.agent_loop as agent_loop
    import src.teacher_escalation as teacher
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    called = []

    async def fake_teacher(**kw):
        called.append(kw)
        yield f"data: {json.dumps({'delta': 'teacher was here'})}\n\n"

    async def fake_stream(*args, **kwargs):
        yield f"data: {json.dumps({'delta': 'Done.'})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(teacher, "run_teacher_inline", fake_teacher)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    kwargs = {} if flag is None else {"teacher_escalation": flag}

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://local.test/v1", "m",
            # A real task: a bare "hi" takes the loop's fast path, which has no teacher step.
            [{"role": "user", "content": "check the disk usage on this server and report it"}],
            max_rounds=1, relevant_tools={"bash"}, forced_tools={"bash"}, **kwargs)]

    chunks = asyncio.run(collect())
    assert bool(called) is expect_teacher
    assert any("teacher was here" in c for c in chunks) is expect_teacher


def test_notes_from_separate_rounds_dont_run_together(session_factory):
    calls = []
    script = [[
        ("text", "PROGRESS: edited in place, backup in the work dir."),
        ("tool", "bash", "ls", {"output": "x", "exit_code": 0}),
        ("text", "The new title is shorter.PROGRESS: checked the bytes against the backup"),
        ("sse", {"type": "agent_step", "round": 2}),
        ("text", "PROGRESS: reloaded Apache\n" + REPORT),
    ]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls))
        job_id = mgr.start_job("t", "u", "m")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    notes = [n["text"] for n in job["state"]["notes"] if n.get("source") == "agent"]
    assert notes == ["edited in place, backup in the work dir.",
                     "checked the bytes against the backup",
                     "reloaded Apache"]


def test_report_shows_host_commands_plainly():
    from src.creator_mode import render_report
    md = render_report({"status": "done", "commands": [
        {"n": 1, "tool": HOST_EXEC_TOOL, "command": json.dumps({"command": "systemctl reload apache2"}),
         "ok": True, "exit_code": 0, "approved": True},
        {"n": 2, "tool": HOST_EXEC_TOOL, "command": "{not json", "ok": True, "exit_code": 0},
        {"n": 3, "tool": "bash", "command": '{"command": "x"}', "ok": True, "exit_code": 0},
    ]})
    assert "1. [host_exec] `systemctl reload apache2` — ok (approved by you)" in md
    assert "2. [host_exec] `{not json` — ok" in md
    assert '3. [bash] `{"command": "x"}` — ok' in md


def test_report_marks_commands_run_under_allow_all(session_factory):
    calls = []
    script = [[_host_card()],
              [("tool", HOST_EXEC_TOOL, '{"command": "uptime"}', {"output": "up", "exit_code": 0}),
               ("text", REPORT)]]

    async def executor(block, **kw):
        return "host_exec", {"output": "reloaded", "exit_code": 0}

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls),
                             tool_executor=executor, host_helper=FakeHelper())
        job_id = mgr.start_job("t", "u", "m")
        await _wait_paused(mgr, job_id)
        mgr.resume_job(job_id, decision="approve_job")
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    cmds = job["state"]["report"]["commands"]
    assert cmds[0].get("approved") is True and "allowed_all" not in cmds[0]
    assert cmds[1].get("allowed_all") is True
    assert "`uptime` — ok (allowed: all host commands)" in job["report"]
    assert "`systemctl reload apache2` — ok (approved by you)" in job["report"]


def test_allow_all_host_commands_up_front(session_factory, tmp_path):
    """The composer's "Allow all host commands up front": host_exec doesn't
    ask, from the first command. It's its own choice: it lifts neither the
    untrusted-content gate nor protected paths. (Asked for after the 5b live
    test, where "approve untrusted up front" still asked at the first host
    command.)"""
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls),
                             host_helper=FakeHelper())
        job_id = mgr.start_job("t", "u", "m", protected_paths=["/etc"], approve_host=True)
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id), job_id

    job, job_id = asyncio.run(run())
    check, approved = calls[0]["protected_action_check"], calls[0]["caller_approved_check"]
    assert check(HOST_EXEC_TOOL, HOST_CMD) is None
    assert approved(HOST_EXEC_TOOL, HOST_CMD) is True
    assert approved("bash", "ls") is False                 # not the untrusted gate
    assert "protected path /etc" in check(HOST_EXEC_TOOL, '{"command": "cat /etc/hosts"}')
    assert any(n["text"].startswith("All host commands are allowed for this job") for n in job["state"]["notes"])
    start = json.loads((tmp_path / "audit" / f"{job_id}.jsonl").read_text().splitlines()[0])
    assert start["approve_host"] is True and start["approve_untrusted"] is False


def test_allow_all_host_up_front_says_nothing_without_the_helper(session_factory):
    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], []))
        job_id = mgr.start_job("t", "u", "m", approve_host=True)
        await _wait_finished(mgr, job_id)
        return mgr.get_job(job_id)

    job = asyncio.run(run())
    assert not any("host commands" in n["text"] for n in job["state"]["notes"])
