"""load_tools: pull a tool the turn's selection missed into the tool list
mid-turn (docs/creator-plan.md, "all tools in one turn")."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.tool_loading import do_load_tools


def test_names_load_known_allowed_tools_with_usage():
    out = do_load_tools('{"names": ["resolve_contact", "web_fetch"]}')
    assert out["exit_code"] == 0
    assert out["loaded"] == ["resolve_contact", "web_fetch"]
    assert "resolve_contact" in out["output"] and "next step" in out["output"]


def test_disabled_unknown_and_policy_blocked_tools_are_refused():
    policy = SimpleNamespace(blocks=lambda name: name == "web_fetch")
    out = do_load_tools({"names": ["bash", "no_such_tool", "web_fetch", "resolve_contact"]},
                        disabled_tools={"bash"}, tool_policy=policy)
    assert out["loaded"] == ["resolve_contact"]
    assert "Not available in this run" in out["output"] and "bash" in out["output"]
    assert "No such tool: no_such_tool" in out["output"]


def test_nothing_loadable_is_an_error():
    out = do_load_tools({"names": ["bash"]}, disabled_tools={"bash"})
    assert out["loaded"] == [] and out["exit_code"] == 1


def test_bare_names_are_accepted():
    assert do_load_tools("resolve_contact, web_fetch")["loaded"] == ["resolve_contact", "web_fetch"]


def test_search_and_list_skip_disabled_tools():
    found = do_load_tools({"search": "email"}, disabled_tools={"send_email"})
    assert "read_email" in found["output"] or "list_emails" in found["output"]
    assert "- send_email:" not in found["output"]
    assert found["loaded"] == []
    everything = do_load_tools("{}")
    assert "- bash:" in everything["output"] and "- load_tools:" not in everything["output"]


def test_invalid_json_is_an_error():
    assert do_load_tools("{not json")["exit_code"] == 1


def test_load_tools_is_always_available_and_registered():
    from src.agent_tools import TOOL_TAGS
    from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS, capabilities_for_tool
    from src.tool_index import ALWAYS_AVAILABLE
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    assert "load_tools" in ALWAYS_AVAILABLE
    assert "load_tools" in TOOL_TAGS and "load_tools" in KNOWN_CAPABILITY_TOOLS
    assert any(s["function"]["name"] == "load_tools" for s in FUNCTION_TOOL_SCHEMAS)
    # No effects of its own, so the untrusted-content gate never holds it back.
    assert not capabilities_for_tool("load_tools").effects


# ---------------------------------------------------------------------------
# The real agent loop
# ---------------------------------------------------------------------------

def _run_loop(monkeypatch, disabled=None):
    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    responses = iter(['```load_tools\n{"names": ["resolve_contact"]}\n```', "Done."])
    offered = []

    async def fake_stream(*args, **kwargs):
        offered.append(json.dumps([args, kwargs], default=str))
        yield f"data: {json.dumps({'delta': next(responses, 'Done.')})}\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        if block.tool_type == "load_tools":
            return "load_tools", do_load_tools(block.content, disabled_tools=kwargs.get("disabled_tools"))
        raise AssertionError(block.tool_type)

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://local.test/v1", "m", [{"role": "user", "content": "do the thing"}],
            max_rounds=3, relevant_tools={"bash", "load_tools"},
            disabled_tools=set(disabled or ()),
        )]

    asyncio.run(collect())
    return offered


def _mentions(offered_round: str, tool: str) -> bool:
    # Native schema or the fenced-tool prompt section, whichever the route uses.
    return f'\\"name\\": \\"{tool}\\"' in offered_round or f"```{tool}```" in offered_round


def test_loaded_tool_is_offered_from_the_next_round(monkeypatch):
    offered = _run_loop(monkeypatch)
    assert len(offered) >= 2
    assert not _mentions(offered[0], "resolve_contact")
    assert _mentions(offered[1], "resolve_contact")


def test_disabled_tool_is_not_loaded_into_the_next_round(monkeypatch):
    offered = _run_loop(monkeypatch, disabled={"resolve_contact"})
    assert len(offered) >= 2
    assert not _mentions(offered[1], "resolve_contact")
