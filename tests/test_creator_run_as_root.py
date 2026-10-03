"""Creator mode Phase 5d: run_as_root inside Creator jobs.

Offered only when the job's owner may use root (a logged-in admin in a
browser session) and the root helper runs commands. The job's gate asks the
helper's watchdog: automatic commands run without a card, approval-tier ones
pause ("root" scope: once or deny, never for the whole job), and while root
is off every root command pauses until you switch it on. Refused commands go
straight to the helper, which refuses them and switches root off. The helper
itself is tested in test_creator_root_run.py; here it's a fake.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src import creator_root_helper
from src.creator_host_helper import HelperError
from src.creator_mode import (
    CREATOR_ROOT_PROMPT, ROOT_APPROVAL_REASON, RUN_AS_ROOT_TOOL, CreatorManager, CreatorResumeError,
    render_report,
)
from src.creator_root_helper import format_root_result, parse_run_as_root_args
# Shared fakes and fixtures (the autouse `isolated` fixture applies here too).
from test_creator_phase2 import (  # noqa: F401
    REPORT,
    _wait_finished,
    _wait_paused,
    isolated,
    scripted,
    session_factory,
)

APT = json.dumps({"command": "apt-get install curl"})
EDIT = json.dumps({"command": "sed -i s/80/8080/ /etc/apache2/ports.conf"})
SHADOW = json.dumps({"command": "cat /etc/shadow"})


class FakeRoot:
    """The root helper's client: hello over ask(), check_sync, run, denied."""

    def __init__(self, on=True, runs_commands=True, error=None, denials_to_off=3):
        self.on = on
        self.runs_commands = runs_commands
        self.error = error
        self.runs, self.denials, self.checks = [], [], []
        self.denials_to_off = denials_to_off

    @staticmethod
    def tier(command):
        if "shadow" in command:
            return "refused"
        return "automatic" if command.startswith("apt-get") else "approval"

    async def ask(self, payload):
        assert payload == {"type": "hello"}
        caps = ["hello", "status", "run", "denied"] if self.runs_commands else ["hello", "status"]
        return {"ok": True, "runs_commands": self.runs_commands, "capabilities": caps,
                "on": self.on, "remaining_s": 1500 if self.on else 0}

    def check_sync(self, command):
        self.checks.append(command)
        if self.error:
            raise HelperError(self.error)
        return {"ok": True, "tier": self.tier(command), "reason": "why", "root_on": self.on}

    async def run(self, command, approved=False, redact=None):
        self.runs.append({"command": command, "approved": approved, "redact": list(redact or [])})
        tier = self.tier(command)
        if tier == "refused":
            self.on = False
            return {"ok": False, "type": "run", "tier": tier, "reason": "refused", "refused_by": "/etc/shadow",
                    "root_switched_off": True, "error": "Refused by the watchdog: It names /etc/shadow. "
                                                       "Root has been switched off."}
        if not self.on:
            return {"ok": False, "tier": tier, "reason": "root_off", "error": "Root is off."}
        if tier == "approval" and not approved:
            return {"ok": False, "tier": tier, "reason": "needs_approval", "error": "needs approval"}
        return {"ok": True, "type": "run", "tier": tier, "exit_code": 0, "stdout": "done\n", "stderr": "",
                "timed_out": False, "truncated": False}

    async def denied(self, command):
        self.denials.append(command)
        off = len(self.denials) >= self.denials_to_off
        if off:
            self.on = False
        return {"ok": True, "root_switched_off": off}


def _root_card(content=EDIT, description=ROOT_APPROVAL_REASON.format(why="why")):
    return ("sse", {"type": "tool_output", "tool": RUN_AS_ROOT_TOOL, "ask_user": {
        "kind": "tool_approval", "approval_id": "ap1", "description": description,
        "action": {"tool": RUN_AS_ROOT_TOOL, "content": content}}})


def _audit(tmp_path, job_id):
    return [json.loads(line) for line in (tmp_path / "audit" / f"{job_id}.jsonl").read_text().splitlines()]


async def _through_manager(block, **kw):
    """A tool executor that runs run_as_root for real (through the manager)."""
    out = await creator_root_helper.do_run_as_root(block.content, owner=kw.get("owner"),
                                                   session_id=kw.get("session_id"))
    return "run_as_root", out


# ---------------------------------------------------------------------------
# Offered only to an owner who may use root, with a helper that runs commands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("allow,helper,offered", [
    (False, FakeRoot(), False),                      # not an admin in the browser
    (True, None, False),                             # no socket (conftest points it nowhere)
    (True, FakeRoot(runs_commands=False), False),    # a 5c helper: no run
    (True, FakeRoot(), True),
])
def test_run_as_root_is_offered_only_when_allowed_and_available(session_factory, tmp_path, allow, helper,
                                                                  offered):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls),
                             root_helper=helper)
        job_id = mgr.start_job("t", "u", "m", allow_root=allow)
        await _wait_finished(mgr, job_id)
        return job_id, mgr.get_job(job_id)

    job_id, job = asyncio.run(run())
    assert (RUN_AS_ROOT_TOOL in calls[0]["relevant_tools"]) is offered
    assert (RUN_AS_ROOT_TOOL in calls[0]["forced_tools"]) is offered
    assert (CREATOR_ROOT_PROMPT in calls[0]["messages"][0]["content"]) is offered
    entries = _audit(tmp_path, job_id)
    assert entries[0]["allow_root"] is allow
    probes = [e for e in entries if e["type"] == "root_probe"]
    assert (probes[0]["ok"] if probes else False) is offered
    if offered:
        assert any("run_as_root is available (root is on (25 min left))" in n["text"]
                   for n in job["state"]["notes"])


def test_the_gate_follows_the_watchdog(session_factory):
    calls = []
    helper = FakeRoot()

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls), root_helper=helper)
        job_id = mgr.start_job("t", "u", "m", allow_root=True, protected_paths=["/etc/apache2"])
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    check, approved = calls[0]["protected_action_check"], calls[0]["caller_approved_check"]
    assert check(RUN_AS_ROOT_TOOL, APT) is None                    # automatic: no card
    assert approved(RUN_AS_ROOT_TOOL, APT) is True                 # nor from the untrusted gate
    assert check(RUN_AS_ROOT_TOOL, SHADOW) is None                 # refused: the helper refuses it
    reason = check(RUN_AS_ROOT_TOOL, json.dumps({"command": "systemctl restart apache2"}))
    assert reason == ROOT_APPROVAL_REASON.format(why="why")
    assert "protected path /etc/apache2" in check(RUN_AS_ROOT_TOOL, EDIT)   # protected paths first
    assert check(RUN_AS_ROOT_TOOL, '{"nope": 1}') is None         # the tool says what's wrong
    helper.on = False
    off = check(RUN_AS_ROOT_TOOL, APT)
    assert off.startswith("Root is off. Switch it on in the header") and "automatic: why" in off
    helper.error = "Couldn't reach the root helper"
    assert check(RUN_AS_ROOT_TOOL, APT) is None                    # the run says why
    assert check("bash", "ls") is None


# ---------------------------------------------------------------------------
# The root-scope pause
# ---------------------------------------------------------------------------

def _root_pause_run(session_factory, decisions, helper=None, cards=1, interactive=True):
    calls = []
    helper = helper or FakeRoot()
    script = [[_root_card()] for _ in range(cards)] + [[("text", REPORT)]]
    out = {}

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls),
                             tool_executor=_through_manager, root_helper=helper)
        job_id = mgr.start_job("change apache's port", "u", "m", owner="alice", allow_root=True)
        pauses = []
        for decision in decisions:
            paused = await _wait_paused(mgr, job_id)
            pauses.append(paused["state"]["pause"])
            mgr.resume_job(job_id, decision=decision, interactive=interactive)
            await asyncio.sleep(0.05)
        await _wait_finished(mgr, job_id)
        out.update(pauses=pauses, job=mgr.get_job(job_id), job_id=job_id)

    asyncio.run(run())
    return calls, helper, out


def test_root_approval_is_once_or_deny_and_tells_the_helper(session_factory, tmp_path):
    calls, helper, out = _root_pause_run(session_factory, ["approve_once"])
    pause = out["pauses"][0]
    assert pause["scope"] == "root" and pause["choices"] == ["approve_once", "deny"]
    assert helper.runs == [{"command": "sed -i s/80/8080/ /etc/apache2/ports.conf", "approved": True,
                            "redact": helper.runs[0]["redact"]}]
    cmds = out["job"]["state"]["report"]["commands"]
    assert cmds[0]["root"] == "approved" and cmds[0]["approved"] is True
    assert "(as root: approved by you)" in out["job"]["report"]
    verdicts = [e for e in _audit(tmp_path, out["job_id"]) if e["type"] == "root_verdict"]
    assert verdicts[0]["tier"] == "approval" and verdicts[0]["approved"] is True and verdicts[0]["ran"] is True
    assert "ran as root (approved by the user)" in calls[1]["messages"][-1]["content"]


def test_approving_one_command_doesnt_approve_another(session_factory):
    async def run():
        helper = FakeRoot()
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([[_root_card()]], []),
                             root_helper=helper)
        job_id = mgr.start_job("t", "u", "m", owner="alice", allow_root=True)
        await _wait_paused(mgr, job_id)
        mgr._live[job_id]["root_approved"] = "something else"
        out = await creator_root_helper.do_run_as_root(EDIT, owner="alice", session_id=job_id)
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)
        return helper, out

    helper, out = asyncio.run(run())
    assert helper.runs[0]["approved"] is False
    assert out["exit_code"] == 1 and "needs approval" in out["error"]


def test_denials_are_counted_by_the_helper_and_three_switch_root_off(session_factory):
    calls, helper, out = _root_pause_run(session_factory, ["deny", "deny", "deny"], cards=3)
    assert helper.runs == [] and len(helper.denials) == 3
    notes = [n["text"] for n in out["job"]["state"]["notes"]]
    assert "Root switched off: 3 root commands were denied in a row." in notes
    assert "Root has now been switched off" in calls[3]["messages"][-1]["content"]
    assert "Root has now been switched off" not in calls[2]["messages"][-1]["content"]


def test_a_root_command_cant_be_approved_with_an_api_token(session_factory):
    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([[_root_card()]], []),
                             tool_executor=_through_manager, root_helper=FakeRoot())
        job_id = mgr.start_job("t", "u", "m", owner="alice", allow_root=True)
        await _wait_paused(mgr, job_id)
        with pytest.raises(CreatorResumeError, match="only be approved in the browser"):
            mgr.resume_job(job_id, decision="approve_once", interactive=False)
        mgr.resume_job(job_id, decision="deny", interactive=False)   # denying is fine
        await _wait_finished(mgr, job_id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# run_as_root itself
# ---------------------------------------------------------------------------

def _direct(session_factory, helper, *commands, owner="alice", allow_root=True):
    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([[_root_card()]], []),
                             root_helper=helper)
        job_id = mgr.start_job("t", "u", "m", owner="alice", allow_root=allow_root,
                               headers={"Authorization": "Bearer endpoint-key-123456789"})
        await _wait_paused(mgr, job_id)
        outs = [await creator_root_helper.do_run_as_root(c, owner=owner, session_id=job_id) for c in commands]
        nojob = await creator_root_helper.do_run_as_root(APT, owner=owner, session_id="cr-000000000000")
        mgr.stop_job(job_id)
        await _wait_finished(mgr, job_id)
        return outs, nojob, mgr.get_job(job_id)

    return asyncio.run(run())


def test_automatic_runs_unapproved_with_secrets_sent_for_blanking(session_factory):
    helper = FakeRoot()
    (out,), nojob, _ = _direct(session_factory, helper, APT)
    assert out["exit_code"] == 0 and out["root"] == "automatic"
    assert out["output"] == "done\n[run_as_root: ran as root (automatic)]"
    assert helper.runs[0]["approved"] is False and helper.runs[0]["command"] == "apt-get install curl"
    assert "endpoint-key-123456789" in helper.runs[0]["redact"]
    assert nojob["exit_code"] == 1 and "running Creator job" in nojob["error"]


def test_refused_and_root_off_and_errors(session_factory):
    helper = FakeRoot()
    (refused, after), _, job = _direct(session_factory, helper, SHADOW, APT)
    assert refused["root"] == "refused" and "Don't try to get around the watchdog" in refused["error"]
    assert any("refused by the watchdog (/etc/shadow); root has been switched off" in n["text"]
               for n in job["state"]["notes"])
    assert after["exit_code"] == 1 and "the job then pauses" in after["error"]   # root went off
    (out,), _, _ = _direct(session_factory, FakeRoot(error=None), APT, owner="bob")
    assert "running Creator job" in out["error"]
    (out,), _, _ = _direct(session_factory, None, APT, allow_root=False)
    assert "isn't connected" in out["error"]

    class Down(FakeRoot):
        async def run(self, *a, **k):
            raise HelperError("Couldn't connect")

    (out,), _, _ = _direct(session_factory, Down(), APT)
    assert out == {"error": "Root helper: Couldn't connect", "exit_code": 1, "root": "not_run"}


def test_report_marks_root_commands():
    md = render_report({"status": "done", "commands": [
        {"n": 1, "tool": RUN_AS_ROOT_TOOL, "command": APT, "ok": True, "exit_code": 0, "root": "automatic"},
        {"n": 2, "tool": RUN_AS_ROOT_TOOL, "command": EDIT, "ok": True, "exit_code": 0, "root": "approved",
         "approved": True},
        {"n": 3, "tool": RUN_AS_ROOT_TOOL, "command": SHADOW, "ok": False, "exit_code": 1, "root": "refused"},
    ]})
    assert "1. [run_as_root] `apt-get install curl` — ok (as root: automatic)" in md
    assert "2. [run_as_root] `sed -i s/80/8080/ /etc/apache2/ports.conf` — ok (as root: approved by you)" in md
    assert "3. [run_as_root] `cat /etc/shadow` — failed, exit 1 (as root: refused by the watchdog)" in md


def test_tool_arguments_and_result_format():
    assert parse_run_as_root_args('{"command": " apt-get update "}') == "apt-get update"
    assert parse_run_as_root_args("apt-get update") == "apt-get update"
    with pytest.raises(ValueError):
        parse_run_as_root_args('{"x": 1}')
    out = format_root_result({"tier": "approval", "exit_code": 0, "stdout": "a\n", "stderr": "",
                              "timed_out": True, "duration_s": 900})
    assert out["root"] == "approved" and "[run_as_root: stopped at the time limit" in out["output"]


def test_run_as_root_is_registered_like_host_exec():
    # Through agent_tools: importing src.tool_schemas first hits an existing import cycle.
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import capabilities_for_action
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    assert capabilities_for_action(RUN_AS_ROOT_TOOL, APT).known
    assert RUN_AS_ROOT_TOOL in BUILTIN_TOOL_DESCRIPTIONS
    assert any(s["function"]["name"] == RUN_AS_ROOT_TOOL for s in FUNCTION_TOOL_SCHEMAS)


def test_execute_tool_block_reaches_run_as_root(monkeypatch):
    from src import tool_execution
    seen = {}

    async def fake(content, owner=None, session_id=None):
        seen.update(content=content, owner=owner, session_id=session_id)
        return {"output": "x", "exit_code": 0}

    monkeypatch.setattr(creator_root_helper, "do_run_as_root", fake)
    block = SimpleNamespace(tool_type=RUN_AS_ROOT_TOOL, content=APT)
    from src.tool_capabilities import ToolRunSecurityContext
    desc, result = asyncio.run(tool_execution.execute_tool_block(
        block, owner="alice", session_id="cr-1", security_context=ToolRunSecurityContext()))
    assert desc == "run_as_root" and seen == {"content": APT, "owner": "alice", "session_id": "cr-1"}


# ---------------------------------------------------------------------------
# Routes: who gets root in a job, and who may approve a root command
# ---------------------------------------------------------------------------

def test_routes_offer_root_only_to_admins_in_the_browser(monkeypatch):
    from routes import creator_routes
    from test_creator_root_helper import _req

    seen = {}

    class Mgr:
        def start_job(self, **kw):
            seen.setdefault("allow_root", []).append(kw["allow_root"])
            return "cr-000000000001"

        def get_job(self, job_id):
            return {"id": job_id, "owner": "alice", "status": "done"}

        def resume_job(self, job_id, decision=None, answer=None, interactive=True):
            seen.setdefault("interactive", []).append(interactive)
            return {"resumed": True}

    monkeypatch.setattr(creator_routes, "require_user", lambda r: r.state.current_user)
    monkeypatch.setattr(creator_routes, "_resolve_creator_endpoint", lambda u, e, m: ("http://x", "m", {}))
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    router = creator_routes.setup_creator_routes(Mgr())
    route = {(r.path, list(r.methods)[0]): r.endpoint for r in router.routes if hasattr(r, "path")}
    start = route[("/api/creator/start", "POST")]
    resume = route[("/api/creator/resume/{job_id}", "POST")]
    body = SimpleNamespace(task="t", endpoint_id=None, model=None, max_minutes=None, approve_untrusted=False,
                           approve_host=False, follow_up_of=None)

    async def go():
        await start(body=body, request=_req("alice"))                 # admin, browser
        await start(body=body, request=_req("carol"))                 # Creator, not admin
        await start(body=body, request=_req("alice", token=True))     # admin, API token
        rb = SimpleNamespace(decision="approve_once", answer=None)
        await resume(job_id="cr-000000000001", body=rb, request=_req("alice"))
        await resume(job_id="cr-000000000001", body=rb, request=_req("alice", token=True))

    asyncio.run(go())
    assert seen == {"allow_root": [True, False, False], "interactive": [True, False]}
