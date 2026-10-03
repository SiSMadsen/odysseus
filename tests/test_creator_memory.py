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
    assert out["summary"] and out["facts"] == 4 and out["lessons"] == 0 and out["skill"] is None
    entries = store.load(owner="alice")
    summary = next(e for e in entries if e["id"] == out["summary"])
    assert summary["category"] == "creator" and summary["source"] == "creator"
    assert summary["session_id"] == "cr-02c59e467c0e" and summary["text"].startswith("Creator job cr-02c59e467c0e")
    facts = {e["text"]: e["category"] for e in entries if e["id"] != out["summary"]}
    assert facts == {"cowsay 3.03 is installed on the host, in /usr/games.": "fact",
                     "The user wants packages installed with apt-get.": "preference",
                     "Apache serves /var/www/html.": "fact", "one more": "fact"}   # at most 4 saved
    assert "Job report:" in calls[0]["messages"][1]["content"]
    assert "Never include passwords, tokens, keys" in calls[0]["messages"][0]["content"]
    # The same job again: nothing new (duplicates are skipped).
    again = asyncio.run(mem.after_job("cr-02c59e467c0e", "alice", REPORT_DATA, "http://x", "m", {}))
    # (Only the one fact the cap left out last time is new.)
    assert again["summary"] is None and again["facts"] == 1
    assert len(store.load(owner="alice")) == 6
    # Another job with the same task and outcome is still another job (9d).
    other = asyncio.run(mem.after_job("cr-0e81d68c313d", "alice", REPORT_DATA, "http://x", "m", {}))
    assert other["summary"] and other["facts"] == 0
    assert len(store.load(owner="alice")) == 7


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
    # A failed job: lessons only (a failure teaches too), never facts.
    assert out["summary"] and len(calls) == 1
    prompt = calls[0]["messages"][0]["content"]
    assert "LESSONS" in prompt and "FACTS" not in prompt
    out = asyncio.run(mem.after_job("cr-3", "alice", {**REPORT_DATA, "status": "stopped", "asked": "Third",
                                                      "commands": []}, "http://x", "m", {}))
    assert len(calls) == 1                                    # did nothing: nothing to learn


SKILL_JSON = {
    "name": "Install a root script and run it from cron",
    "description": "Stage a script, test it as creator, install it as root and schedule it.",
    "when_to_use": "A periodic job that needs root, e.g. regenerating a status page.",
    "procedure": ["Write the script in /srv/creator-helper/work with host_exec",
                  "Run it as creator with output in the work folder and check it",
                  "run_as_root: install -m 755 /srv/creator-helper/work/x /usr/local/bin/x",
                  "run_as_root: install the cron file into /etc/cron.d"],
    "pitfalls": ["goaccess -o needs a file name ending in .html"],
    "verification": ["curl the page and expect 200"],
    "tags": ["cron", "root", "script"], "confidence": 0.85,
}


def _skills(tmp_path, name="skills"):
    from services.memory.skills import SkillsManager
    return SkillsManager(str(tmp_path / name))


def test_a_skill_is_learned_from_the_whole_report_and_marked_creator(monkeypatch, prefs, tmp_path):
    calls = _llm(monkeypatch, json.dumps(SKILL_JSON))
    skills = _skills(tmp_path)
    prefs.update(auto_approve_skills=False)
    out = asyncio.run(CreatorMemory(skills_manager=skills).after_job(
        "cr-1", "alice", STATUS_REPORT, "http://x", "m", {}, tool_calls=4, rounds=1))
    assert out["skill"] and out["skill_note"] == "saved as draft"
    learned = skills.load(owner="alice")[0]
    assert learned["source"] == "creator" and learned["status"] == "draft" and learned["category"] == "creator"
    assert learned["pitfalls"] == ["goaccess -o needs a file name ending in .html"]
    assert learned["procedure"][2].startswith("run_as_root: install")
    # The model saw the whole report, commands and effort included (chat's
    # extractor cut it to 500 characters, which left only the task).
    sent = calls[0]["messages"][1]["content"]
    assert "Commands:" in sent and "Effort:" in sent and len(sent) > 600


def test_auto_approve_publishes_creator_skills_like_any_other(monkeypatch, prefs, tmp_path):
    _llm(monkeypatch, json.dumps(SKILL_JSON))
    skills = _skills(tmp_path)
    prefs.update(auto_approve_skills=True)
    out = asyncio.run(CreatorMemory(skills_manager=skills).after_job(
        "cr-1", "alice", STATUS_REPORT, "http://x", "m", {}, tool_calls=3))
    assert out["skill_note"] == "saved as published" and skills.load(owner="alice")[0]["status"] == "published"


@pytest.mark.parametrize("reply, note", [
    ({"skip": "a one-off check, nothing to reuse"}, "declined: a one-off check, nothing to reuse"),
    ({**SKILL_JSON, "confidence": 0.3}, "too unsure (0.30 < 0.6)"),
    ({**SKILL_JSON, "procedure": ["only one step"]}, "the model's skill had no name or too few steps"),
    ("Sorry, I can't do that.", "the model's answer had no JSON object: Sorry, I can't do that."),
])
def test_why_no_skill_is_always_said(monkeypatch, prefs, tmp_path, reply, note):
    _llm(monkeypatch, reply if isinstance(reply, str) else json.dumps(reply))
    skills = _skills(tmp_path)
    out = asyncio.run(CreatorMemory(skills_manager=skills).after_job(
        "cr-1", "alice", STATUS_REPORT, "http://x", "m", {}, tool_calls=4))
    assert out["skill"] is None and out["skill_note"] == note and skills.load(owner="alice") == []


def test_duplicates_and_the_gates_say_why_too(monkeypatch, prefs, tmp_path):
    _llm(monkeypatch, json.dumps(SKILL_JSON))
    skills = _skills(tmp_path)
    mem = CreatorMemory(skills_manager=skills)
    asyncio.run(mem.after_job("cr-1", "alice", STATUS_REPORT, "http://x", "m", {}, tool_calls=4))
    again = asyncio.run(mem.after_job("cr-2", "alice", STATUS_REPORT, "http://x", "m", {}, tool_calls=4))
    assert again["skill"] is None and "already exists" in again["skill_note"]
    renamed = {**SKILL_JSON, "name": "Install a root script and run it from cron jobs"}
    _llm(monkeypatch, json.dumps(renamed))
    near = asyncio.run(mem.after_job("cr-3", "alice", STATUS_REPORT, "http://x", "m", {}, tool_calls=4))
    assert near["skill"] is None and "nearly the same as the existing skill" in near["skill_note"]
    assert len(skills.load(owner="alice")) == 1
    for report, kw, note in (
        ({**STATUS_REPORT, "status": "stopped"}, {"tool_calls": 9}, "only finished jobs teach skills"),
        (STATUS_REPORT, {"tool_calls": 1, "rounds": 1}, "too few steps to be a procedure"),
    ):
        assert asyncio.run(mem.after_job("cr-4", "alice", report, "u", "m", {}, **kw))["skill_note"] == note
    prefs.update(auto_skills=False)
    assert asyncio.run(mem.after_job("cr-5", "alice", STATUS_REPORT, "u", "m", {}, tool_calls=9))["skill_note"] \
        == "auto skills is off in your preferences"


def test_learn_a_skill_on_demand_from_a_finished_job(monkeypatch, prefs, session_factory, tmp_path):
    _llm(monkeypatch, json.dumps(SKILL_JSON))
    skills = _skills(tmp_path)
    prefs.update(auto_skills=False)   # the button works whatever the setting

    async def run():
        mgr = CreatorManager(session_factory=session_factory, agent_loop=scripted([[("text", REPORT)]], []),
                             memory=CreatorMemory(skills_manager=skills))
        job_id = mgr.start_job("Build a status page", "u", "m", owner="alice")
        await _wait_finished(mgr, job_id)
        return job_id, await mgr.learn_skill(job_id, "http://x", "m", {})

    job_id, out = asyncio.run(run())
    assert out["skill"] and out["note"] == "saved as published"
    log = [json.loads(line) for line in (tmp_path / "audit" / f"{job_id}.jsonl").read_text().splitlines()]
    assert any(e["type"] == "skill_learned" and e["by"] == "button" and e["skill"] == out["skill"] for e in log)


def test_the_learn_skill_route_is_for_your_finished_jobs(monkeypatch):
    from fastapi import HTTPException
    from routes import creator_routes
    seen = []

    class Mgr:
        def get_job(self, job_id):
            return {"id": job_id, "owner": "alice", "status": {"cr-000000000001": "done"}.get(job_id, "stopped")}

        async def learn_skill(self, job_id, url, model, headers):
            seen.append((job_id, url, model))
            return {"skill": "x", "status": "draft", "note": "saved as draft"}

    monkeypatch.setattr(creator_routes, "require_user", lambda r: r.state.current_user)
    monkeypatch.setattr(creator_routes, "_resolve_creator_endpoint", lambda u, e, m: ("http://x", "m", {}))
    router = creator_routes.setup_creator_routes(Mgr())
    route = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/creator/learn-skill/{job_id}")
    req = lambda user: SimpleNamespace(state=SimpleNamespace(current_user=user), headers={},
                                       app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)))
    assert asyncio.run(route(job_id="cr-000000000001", request=req("alice")))["skill"] == "x"
    assert seen == [("cr-000000000001", "http://x", "m")]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(route(job_id="cr-000000000002", request=req("alice")))   # not finished
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(route(job_id="cr-000000000001", request=req("bob")))     # not yours
    assert exc.value.status_code == 404


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


# ---------------------------------------------------------------------------
# Lessons (learned by doing)
# ---------------------------------------------------------------------------

STATUS_REPORT = {
    **REPORT_DATA,
    "asked": "Build a server status page with goaccess.",
    "commands": [
        {"n": 1, "tool": "run_as_root", "command": '{"command": "install odystatus"}', "ok": True,
         "exit_code": 0, "root": "approved", "approved": True},
        {"n": 2, "tool": "run_as_root", "command": '{"command": "/usr/local/bin/odystatus"}', "ok": False,
         "exit_code": 1, "root": "approved", "approved": True},
        {"n": 3, "tool": "run_as_root", "command": '{"command": "systemctl reload apache2"}', "ok": True,
         "exit_code": 0, "root": "approved", "approved": True},
        {"n": 4, "tool": "run_as_root", "command": '{"command": "apt-get install -y goaccess"}', "ok": True,
         "exit_code": 0, "root": "automatic"},
    ],
    "failures": ["[run_as_root] `x` failed 3 times the same way: boom"],
}


def test_the_report_shows_the_effort_so_lessons_can_cut_it():
    text = report_text(STATUS_REPORT)
    assert "-> failed (exit 1), as root: approved" in text and "-> ok, as root: automatic" in text
    assert "Effort: 4 commands, 1 failed, 3 needed the user's approval (3 of them root commands)." in text
    assert "Repeated failures:\n- [run_as_root] `x` failed 3 times" in text


def test_lessons_are_saved_marked_and_capped(monkeypatch, prefs, store):
    _llm(monkeypatch, json.dumps([
        {"text": "goaccess -o needs a file name ending in .html.", "category": "lesson"},
        {"text": "Creator lesson: reloading Apache needs no root; use host_exec.", "category": "lesson"},
        {"text": "index.html uses CRLF line endings; edit it byte for byte.", "category": "lesson"},
        {"text": "a fourth lesson, past the limit", "category": "lesson"},
        {"text": "goaccess 1.7 is installed.", "category": "fact"},
    ]))
    out = asyncio.run(CreatorMemory(memory_manager=store).after_job(
        "cr-9", "alice", STATUS_REPORT, "http://x", "m", {}))
    assert out["lessons"] == 3 and out["facts"] == 1
    lessons = [e["text"] for e in store.load(owner="alice") if e["category"] == "lesson"]
    assert lessons == ["Creator lesson: goaccess -o needs a file name ending in .html.",
                       "Creator lesson: reloading Apache needs no root; use host_exec.",
                       "Creator lesson: index.html uses CRLF line endings; edit it byte for byte."]


def test_a_stopped_job_still_leaves_lessons_but_no_facts(monkeypatch, prefs, store):
    _llm(monkeypatch, json.dumps([
        {"text": "Root can't see the host helper's /tmp: stage files in the work folder.", "category": "lesson"},
        {"text": "This should be ignored: a fact from an unfinished job.", "category": "fact"},
    ]))
    out = asyncio.run(CreatorMemory(memory_manager=store).after_job(
        "cr-8", "alice", {**STATUS_REPORT, "status": "stopped"}, "http://x", "m", {}))
    assert out["lessons"] == 1 and out["facts"] == 0
    assert [e["category"] for e in store.load(owner="alice")] == ["creator", "lesson"]


def test_a_job_is_given_the_lessons_that_match_its_task(prefs, store):
    entries = store.load_all_for_update()
    for text, cat in (("Creator lesson: goaccess -o needs a file name ending in .html.", "lesson"),
                      ("Creator lesson: reloading apache needs no root; use host_exec.", "lesson"),
                      ("Creator lesson: postgres dumps need the postgres user.", "lesson"),
                      ("Apache serves /var/www/html.", "fact")):
        entries.append(store.add_entry(text, category=cat, owner="alice"))
    store.save(entries)

    class Proc(FakeProcessor):
        def build_context_preface(self, **kw):
            return ([untrusted_context_message("saved memory: retrieved context",
                                               "- Creator lesson: postgres dumps need the postgres user.")], [], [])

    msgs = CreatorMemory(memory_manager=store, chat_processor=Proc()).context_messages(
        "Add the goaccess traffic report to the apache status page", "alice")
    block = msgs[-1]
    assert block["metadata"]["source"] == "saved memory: creator lessons"
    assert "goaccess -o needs" in block["content"] and "reloading apache" in block["content"]
    assert "postgres" not in block["content"]       # unrelated, and already in the memory block
    assert CreatorMemory(memory_manager=store, chat_processor=Proc()).lessons_for("Bake a cake", "alice") == []
    assert CreatorMemory(memory_manager=store).lessons_for("goaccess", "bob") == []   # only your own


def test_lessons_use_chats_retrieval_when_it_has_one(prefs, store):
    entries = store.load_all_for_update()
    entries.append(store.add_entry("Creator lesson: a.", category="lesson", owner="alice"))
    store.save(entries)
    asked = []

    class Proc:
        def _hybrid_retrieve(self, message, mem_entries, k=5):
            asked.append((message, k, [e["text"] for e in mem_entries]))
            return mem_entries

    assert CreatorMemory(memory_manager=store, chat_processor=Proc()).lessons_for("task", "alice") == ["Creator lesson: a."]
    assert asked == [("task", 6, ["Creator lesson: a."])]
