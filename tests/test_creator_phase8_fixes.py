"""Fixes from the Phase 8 live checklist (docs/creator-plan.md)."""

import asyncio
import json

import pytest

from src.creator_mode import CreatorManager
from src.creator_secrets import do_get_secret
# Shared fakes and fixtures (the autouse `isolated` fixture applies here too).
from test_creator_phase2 import (  # noqa: F401
    REPORT,
    _wait_finished,
    _wait_paused,
    isolated,
    scripted,
    session_factory,
)

SECRET = "tok-9f8e7d6c5b4a3210"


def _card(tool, content):
    return ("sse", {"type": "tool_output", "tool": tool, "ask_user": {
        "kind": "tool_approval", "approval_id": "ap1",
        "description": "External untrusted context has already influenced this run.",
        "action": {"tool": tool, "content": content}}})


def _approved_run(session_factory, tmp_path, tool, content, executor):
    calls = []
    script = [[_card(tool, content)], [("text", REPORT)]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls),
                             tool_executor=executor)
        mgr.secrets.create("alice", "TEST_TOKEN", SECRET, enabled=True)
        job_id = mgr.start_job("t", "u", "m", owner="alice")
        await _wait_paused(mgr, job_id)
        mgr.resume_job(job_id, decision="approve_once")
        await _wait_finished(mgr, job_id)
        return job_id, mgr.get_job(job_id)

    job_id, job = asyncio.run(run())
    stored = (tmp_path / "audit" / f"{job_id}.jsonl").read_text() + json.dumps(job)
    return calls, stored


def test_approved_get_secret_hands_the_value_to_the_model_only(session_factory, tmp_path):
    """Phase 8 test 5 (job cr-3e30e949a8e2): get_secret ran as an approved
    action and the model got "[REDACTED]"."""
    async def executor(block, **kw):
        return "get_secret", await do_get_secret(block.content, owner="alice", session_id=kw["session_id"])

    calls, stored = _approved_run(session_factory, tmp_path, "get_secret", '{"name": "TEST_TOKEN"}', executor)
    told = calls[1]["messages"][-1]["content"]
    assert SECRET in told and "[REDACTED]" not in told
    assert SECRET not in stored and "[REDACTED]" in stored


def test_other_approved_actions_still_hide_secrets_from_the_model(session_factory, tmp_path):
    async def executor(block, **kw):
        return "bash", {"output": f"config: token={SECRET}", "exit_code": 0}

    calls, stored = _approved_run(session_factory, tmp_path, "bash", "cat app.conf", executor)
    told = calls[1]["messages"][-1]["content"]
    assert SECRET not in told and "token=[REDACTED]" in told
    assert SECRET not in stored


def test_creator_turns_the_missing_workspace_stop_off(session_factory):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([], calls))
        job_id = mgr.start_job("create that file in the workspace", "u", "m")
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    assert calls[0]["stop_on_missing_workspace"] is False



@pytest.mark.parametrize("flag,expect_canned", [(None, True), (False, False)])
def test_real_loop_missing_workspace_stop_can_be_turned_off(monkeypatch, flag, expect_canned):
    """Phase 8 test 3 (job cr-42c6fd9b6a56): "…create that file in the
    workspace" ended at chat's canned reply, with no model call."""
    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    model_calls = []

    async def fake_stream(*args, **kwargs):
        model_calls.append(1)
        yield f"data: {json.dumps({'delta': 'Which file name should I use?'})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    kwargs = {} if flag is None else {"stop_on_missing_workspace": flag}

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://local.test/v1", "m",
            [{"role": "user", "content": "Ask me which file name to use, then create that file in the workspace."}],
            max_rounds=1, relevant_tools={"bash"}, forced_tools={"bash"}, teacher_escalation=False, **kwargs)]

    text = "".join(asyncio.run(collect()))
    assert ("No active workspace is set" in text) is expect_canned
    assert bool(model_calls) is (not expect_canned)
