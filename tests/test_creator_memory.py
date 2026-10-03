"""Creator mode Phase 9: memory and skills shared with the rest of Odysseus
(src/creator_memory.py and its wiring in src/creator_mode.py).

Into a job: the memories chat would choose for the task, in every segment,
and skills matched on the task. Out of a job: one summary memory, durable
facts (finished jobs), and a skill (finished jobs with enough steps), each
following the owner's preferences. For chat: the read-only creator_jobs tool.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src import creator_memory
from src.creator_memory import CreatorMemory, do_creator_jobs, job_summary, report_text
from src.creator_mode import CREATOR_CORE_TOOLS, CreatorManager
from src.memory import MemoryManager
from src.prompt_security import untrusted_context_message
# Shared fakes and fixtures (the autouse `isolated` fixture applies here too).
from test_creator_phase2 import (  # noqa: F401
    REPORT,
    _wait_finished,
    isolated,
    scripted,
    session_factory,
)

REPORT_DATA = {
    "asked": "Install cowsay on the host and tell me its version.",
    "status": "done",
    "started_at": "2026-10-03T10:16:51Z",
    "finished_at": "2026-10-03T10:18:03Z",
    "done": "- Installed `cowsay` with run_as_root (apt-get install -y cowsay).\n- Checked the version.",
    "worked": "The install worked; version 3.03+dfsg2-8.",
    "didnt_work": "",
    "left": "",
    "commands": [
        {"n": 1, "tool": "run_as_root", "command": '{"command": "apt-get install -y cowsay"}', "ok": True,
         "exit_code": 0},
        {"n": 2, "tool": "host_exec", "command": "dpkg-query -W cowsay", "ok": True, "exit_code": 0},
    ],
}


@pytest.fixture
def prefs(monkeypatch):
    values = {}
    monkeypatch.setattr(creator_memory, "_prefs", lambda owner: dict(values))
    # Chat's skill extractor reads them itself.
    import routes.prefs_routes as prefs_routes
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda owner=None: dict(values))
    return values


@pytest.fixture
def store(tmp_path):
    (tmp_path / "mem").mkdir()
    return MemoryManager(str(tmp_path / "mem"))


def _llm(monkeypatch, reply):
    calls = []

    async def fake(url, model, messages, **kw):
        calls.append({"url": url, "model": model, "messages": messages})
        return reply(messages) if callable(reply) else reply

    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", fake)
    monkeypatch.setattr(creator_memory.CreatorMemory, "_task_endpoint",
                        lambda self, u, m, h, o: (u, m, h))
    return calls


# ---------------------------------------------------------------------------
# What a job leaves
# ---------------------------------------------------------------------------

def test_the_summary_says_when_what_and_how_it_went():
    text = job_summary("cr-02c59e467c0e", REPORT_DATA)
    assert text.startswith("Creator job cr-02c59e467c0e on 2026-10-03 (finished): Install cowsay on the host "
                           "and tell me its version. Result: Installed cowsay with run_as_root")
    assert "`" not in text and "run_as_root (apt-get install -y cowsay). Checked the version." in text
    stopped = job_summary("cr-1", {**REPORT_DATA, "status": "stopped", "done": "", "worked": ""})
    assert "(stopped by the user)" in stopped and "Result:" not in stopped
    long = job_summary("cr-1", {**REPORT_DATA, "asked": "x " * 400, "done": "word " * 400})
    assert len(long) < 600


def test_report_text_has_the_task_sections_and_commands():
    text = report_text(REPORT_DATA)
    assert text.startswith("Task: Install cowsay")
    assert "What was done:" in text and "What didn't work" not in text
    assert "- [run_as_root] " in text and "-> ok" in text


def test_after_job_saves_a_summary_and_facts(monkeypatch, prefs, store):
    calls = _llm(monkeypatch, json.dumps([
        {"text": "cowsay 3.03 is installed on the host, in /usr/games.", "category": "fact"},
        {"text": "The user wants packages installed with apt-get.", "category": "preference"},
        {"text": "x", "category": "fact"},                                   # too short
        {"text": "Apache serves /var/www/html.", "category": "nonsense"},    # category tidied
        {"text": "one more", "category": "fact"},
        {"text": "and one too many, past the limit of four", "category": "fact"},
    ]))
    mem = CreatorMemory(memory_manager=store)
    out = asyncio.run(mem.after_job("cr-02c59e467c0e", "alice", REPORT_DATA, "http://x", "m", {}))
    assert out["summary"] and out["facts"] == 3 and out["skill"] is None
    entries = store.load(owner="alice")
    summary = next(e for e in entries if e["id"] == out["summary"])
    assert summary["category"] == "creator" and summary["source"] == "creator"
    assert summary["session_id"] == "cr-02c59e467c0e" and summary["text"].startswith("Creator job cr-02c59e467c0e")
    facts = {e["text"]: e["category"] for e in entries if e["id"] != out["summary"]}
    assert facts == {"cowsay 3.03 is installed on the host, in /usr/games.": "fact",
                     "The user wants packages installed with apt-get.": "preference",
                     "Apache serves /var/www/html.": "fact"}
    assert "Job report:" in calls[0]["messages"][1]["content"]
    assert "never include those" in calls[0]["messages"][0]["content"]
    # The same job again: nothing new (duplicates are skipped).
    again = asyncio.run(mem.after_job("cr-02c59e467c0e", "alice", REPORT_DATA, "http://x", "m", {}))
    assert again["summary"] is None and again["facts"] == 0
    assert len(store.load(owner="alice")) == 4


def test_after_job_follows_the_owners_preferences(monkeypatch, prefs, store):
    calls = _llm(monkeypatch, "[]")
    mem = CreatorMemory(memory_manager=store)
    prefs.update(memory_enabled=False)
    out = asyncio.run(mem.after_job("cr-1", "alice", REPORT_DATA, "http://x", "m", {}))
    assert out["skipped"] == "memory off" and store.load(owner="alice") == [] and calls == []
    prefs.update(memory_enabled=True, auto_memory=False)
    out = asyncio.run(mem.after_job("cr-1", "alice", REPORT_DATA, "http://x", "m", {}))
    assert out["summary"] and calls == []                     # the summary, but no fact extraction
    prefs.update(auto_memory=True)
    out = asyncio.run(mem.after_job("cr-2", "alice", {**REPORT_DATA, "status": "error", "asked": "Other task"},
                                    "http://x", "m", {}))
    assert out["summary"] and calls == []                     # no facts from a failed job


def test_a_skill_is_learned_from_a_finished_job_and_marked_creator(monkeypatch, prefs, tmp_path):
    from services.memory.skills import SkillsManager
    _llm(monkeypatch, json.dumps({
        "title": "Install a Debian package as root with Creator", "problem": "p", "solution": "s",
        "steps": ["run_as_root apt-get install -y <pkg>", "check with dpkg-query -W <pkg>", "report the version"],
        "tags": ["apt", "debian", "install"], "confidence": 0.9}))
    skills = SkillsManager(str(tmp_path / "skills"))
    mem = CreatorMemory(skills_manager=skills)
    prefs.update(auto_approve_skills=False)
    out = asyncio.run(mem.after_job("cr-1", "alice", REPORT_DATA, "http://x", "m", {}, tool_calls=2, rounds=1))
    learned = skills.load(owner="alice")
    assert out["skill"] and len(learned) == 1
    assert learned[0]["source"] == "creator" and learned[0]["status"] == "draft"
    # Too few steps, or not finished: nothing to learn.
    skills2 = SkillsManager(str(tmp_path / "skills2"))
    mem2 = CreatorMemory(skills_manager=skills2)
    asyncio.run(mem2.after_job("cr-2", "alice", REPORT_DATA, "http://x", "m", {}, tool_calls=1, rounds=1))
    asyncio.run(mem2.after_job("cr-3", "alice", {**REPORT_DATA, "status": "stopped"}, "http://x", "m", {},
                               tool_calls=5, rounds=3))
    prefs.update(auto_skills=False)
    asyncio.run(mem2.after_job("cr-4", "alice", REPORT_DATA, "http://x", "m", {}, tool_calls=5, rounds=3))
    assert skills2.load(owner="alice") == []


def test_auto_approve_publishes_creator_skills_like_any_other(monkeypatch, prefs, tmp_path):
    from services.memory.skills import SkillsManager
    _llm(monkeypatch, json.dumps({"title": "Reload Apache after an edit", "steps": ["a", "b", "c"],
                                  "tags": ["apache"], "confidence": 0.9}))
    skills = SkillsManager(str(tmp_path / "skills"))
    prefs.update(auto_approve_skills=True)
    asyncio.run(CreatorMemory(skills_manager=skills).after_job(
        "cr-1", "alice", REPORT_DATA, "http://x", "m", {}, tool_calls=3))
    assert skills.load(owner="alice")[0]["status"] == "published"


def test_after_job_never_raises(monkeypatch, prefs):
    class Broken:
        def load_all_for_update(self):
            raise OSError("disk gone")

    _llm(monkeypatch, "not json at all")
    out = asyncio.run(CreatorMemory(memory_manager=Broken()).after_job("cr-1", "a", REPORT_DATA, "u", "m", {}))
    assert out["summary"] is None and out["facts"] == 0


# ---------------------------------------------------------------------------
# Into a job
# ---------------------------------------------------------------------------

class FakeProcessor:
    def __init__(self):
        self.calls = []

    def build_context_preface(self, **kw):
        self.calls.append(kw)
        return ([{"role": "system", "content": "policy"},
                 untrusted_context_message("saved memory: pinned context", "- Name is Sam"),
                 untrusted_context_message("saved memory: retrieved context", "- Apache serves /var/www/html"),
                 untrusted_context_message("retrieved documents", "not memory")], [], [])


def test_context_messages_are_the_memories_chat_would_pick(prefs):
    proc = FakeProcessor()
    msgs = CreatorMemory(chat_processor=proc).context_messages("Fix the status page", "alice")
    assert [m["metadata"]["source"] for m in msgs] == ["saved memory: pinned context",
                                                         "saved memory: retrieved context"]
    kw = proc.calls[0]
    assert kw["message"] == "Fix the status page" and kw["owner"] == "alice"
    assert kw["use_memory"] is True and kw["use_rag"] is False and kw["use_web"] is False
    prefs.update(memory_enabled=False)
    assert CreatorMemory(chat_processor=proc).context_messages("x", "alice") == []
    assert CreatorMemory().context_messages("x", "alice") == []


class FakeMemory:
    def __init__(self):
        self.after = []

    def context_messages(self, task, owner):
        return [untrusted_context_message("saved memory: retrieved context", "- Apache serves /var/www/html")]

    async def after_job(self, job_id, owner, report, url, model, headers, tool_calls=0, rounds=0):
        self.after.append({"job_id": job_id, "owner": owner, "report": report, "tool_calls": tool_calls})
        return {"summary": "m1", "facts": 2, "skill": "install-a-package"}


def test_a_job_gets_memories_every_segment_skills_on_its_task_and_leaves_memories(session_factory, tmp_path):
    calls = []
    memory = FakeMemory()
    script = [[("text", "PROGRESS: working")], [("text", REPORT)]]

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted(script, calls), memory=memory)
        job_id = mgr.start_job("Fix the status page", "u", "m", owner="alice")
        await _wait_finished(mgr, job_id)
        for _ in range(50):
            if memory.after:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        return job_id, mgr.get_job(job_id)

    job_id, job = asyncio.run(run())
    first = calls[0]["messages"]
    assert first[0]["role"] == "system" and first[1]["metadata"]["source"] == "saved memory: retrieved context"
    assert first[-1]["content"] == "Fix the status page"
    assert calls[0]["skill_query"] == "Fix the status page"
    assert {"manage_memory", "creator_jobs"} <= set(calls[0]["relevant_tools"])
    assert {"manage_memory", "creator_jobs"} <= CREATOR_CORE_TOOLS
    assert any("saved memories" in n["text"] for n in job["state"]["notes"])
    after = memory.after[0]
    assert after["job_id"] == job_id and after["owner"] == "alice" and after["report"]["asked"] == "Fix the status page"
    log = [json.loads(line) for line in (tmp_path / "audit" / f"{job_id}.jsonl").read_text().splitlines()]
    assert {"type": "remembered", "summary": "m1", "facts": 2, "skill": "install-a-package"}.items() \
        <= next(e for e in log if e["type"] == "remembered").items()


def test_a_checkpoint_keeps_the_memories(session_factory):
    calls = []

    async def run():
        mgr = CreatorManager(session_factory=session_factory, memory=FakeMemory(),
                             agent_loop=scripted([[("sse", {"type": "rounds_exhausted"})], [("text", REPORT)]], calls))
        job_id = mgr.start_job("Fix it", "u", "m", owner="alice")
        await _wait_finished(mgr, job_id)

    asyncio.run(run())
    assert len(calls) == 2   # the checkpoint started a second segment
    later = calls[1]
    assert any((m.get("metadata") or {}).get("source") == "saved memory: retrieved context" for m in later["messages"])
    assert later["messages"][-1]["content"].startswith("[Creator mode — continuing the same task]")
    assert later["skill_query"] == "Fix it"


def test_the_agent_loop_matches_skills_on_the_query_it_is_given(monkeypatch):
    import src.agent_loop as agent_loop
    from services.memory import skills as skills_mod
    seen = []
    monkeypatch.setattr(skills_mod.SkillsManager, "get_relevant_skills",
                        lambda self, q, **kw: seen.append(q) or [])
    monkeypatch.setattr(skills_mod.SkillsManager, "load", lambda self, owner=None: [])
    msgs = [{"role": "user", "content": "Fix the status page"},
            {"role": "user", "content": "[Creator mode — continuing] notes…"}]
    agent_loop._build_system_prompt(msgs, "m", None, None, owner="alice", skill_query="Fix the status page")
    agent_loop._build_system_prompt(msgs, "m", None, None, owner="alice")
    assert seen == ["Fix the status page", "[Creator mode — continuing] notes…"]


# ---------------------------------------------------------------------------
# creator_jobs (chat)
# ---------------------------------------------------------------------------

def test_creator_jobs_lists_and_reads_only_your_jobs(session_factory):
    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([[("text", REPORT)]], []))
        mine = mgr.start_job("Build a status page", "u", "m", owner="alice")
        await _wait_finished(mgr, mine)
        return mine

    mine = asyncio.run(run())
    listed = do_creator_jobs('{"action": "list"}', owner="alice")
    assert listed["exit_code"] == 0 and mine in listed["output"] and "Build a status page" in listed["output"]
    assert do_creator_jobs("{}", owner="bob")["output"] == "You have no Creator jobs yet."
    read = do_creator_jobs({"action": "read", "id": mine}, owner="alice")
    assert read["exit_code"] == 0 and read["output"].startswith("# Creator report")
    assert do_creator_jobs(mine, owner="alice")["output"] == read["output"]        # a bare id reads it
    assert "No Creator job" in do_creator_jobs({"action": "read", "id": mine}, owner="bob")["error"]
    assert "Give the job id" in do_creator_jobs({"action": "read", "id": "nope"}, owner="alice")["error"]
    assert "Unknown action" in do_creator_jobs({"action": "delete"}, owner="alice")["error"]


def test_creator_jobs_is_registered_read_only_and_its_results_are_outside_text():
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import ResultIntegrity, ToolEffect, capabilities_for_action
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    caps = capabilities_for_action("creator_jobs", '{"action": "list"}')
    assert caps.known and caps.effects == {ToolEffect.READ_PRIVATE}
    assert caps.result_integrity == ResultIntegrity.EXTERNAL_UNTRUSTED
    assert "creator_jobs" in BUILTIN_TOOL_DESCRIPTIONS
    assert any(s["function"]["name"] == "creator_jobs" for s in FUNCTION_TOOL_SCHEMAS)


def test_execute_tool_block_reaches_creator_jobs(monkeypatch):
    from src import tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    seen = {}
    monkeypatch.setattr(creator_memory, "do_creator_jobs",
                        lambda content, owner=None: seen.update(content=content, owner=owner) or {"output": "x",
                                                                                                    "exit_code": 0})
    block = SimpleNamespace(tool_type="creator_jobs", content='{"action": "list"}')
    desc, _ = asyncio.run(tool_execution.execute_tool_block(
        block, owner="alice", session_id="s1", security_context=ToolRunSecurityContext()))
    assert desc == "creator_jobs" and seen == {"content": '{"action": "list"}', "owner": "alice"}
