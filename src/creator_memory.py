"""Creator mode and the shared Odysseus memory and skills (Phase 9 of
docs/creator-plan.md).

Into a job: at its start, the owner's saved memories that matter for the
task (pinned ones and ones retrieved for it, chosen exactly as chat chooses
them), given to the model as outside context, like in chat. Skills come in
through the agent loop, matched on the task (see creator_mode).

Out of a job, when it has ended (in the background, never holding the job up):
- one short memory per job (date, id, outcome, the task, what was done), so a
  normal chat can answer "what did Creator do on the web server?";
- durable facts about the machine from the report (paths, services,
  versions, what changed), by the model, with chat's de-duplication;
- a skill, by chat's skill extractor, after a finished job with enough
  steps. It follows the owner's Skills settings (auto-approve or draft) and is
  marked as learned from Creator.
All of it respects the owner's preferences (memory on, auto memory, auto
skills) and is built from the report, which already has secret values
blanked.

And for normal chat: `creator_jobs`, a read-only tool that lists the owner's
Creator jobs and reads one's report. It can't start, approve or stop jobs.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MEMORY_CATEGORY = "creator"
MEMORY_SOURCE = "creator"
SKILL_SOURCE = "creator"
# A job needs at least this many commands for a skill to be worth extracting
# (chat's extractor wants 2 rounds or 2 tool calls).
SKILL_MIN_COMMANDS = 2
MAX_FACTS = 4
MAX_LESSONS = 3
LESSON_CATEGORY = "lesson"
LESSON_PREFIX = "Creator lesson: "
# Lessons given to a job, matched on its task.
MAX_LESSONS_IN = 6
SUMMARY_TASK_CHARS = 160
SUMMARY_DONE_CHARS = 280
# What the fact extractor and skill extractor are shown of a report.
EXTRACT_REPORT_CHARS = 6000

FACTS_RULES = (
    "FACTS (category 'fact', 'project' or 'preference'): DURABLE facts about "
    "the machine and its setup. Good: where things are (web root, config "
    "files, log paths), which services/software and versions are installed, "
    "what was changed and is still in effect, how the user wants things done. "
    "Bad: one-off command output, temporary states. "
    f"At most {MAX_FACTS}.\n"
)
LESSONS_RULES = (
    "LESSONS (category 'lesson'): what the job learned by DOING that would "
    "make the next job on this machine faster or smoother. Good: a tool's "
    "quirk and its fix (\"goaccess -o needs a file name ending in .html\"), a "
    "pitfall that cost a retry (\"index.html uses CRLF line endings: edit it "
    "byte for byte\"), a step that turned out unnecessary or could be done "
    "with less privilege (\"reloading Apache needs no root: use host_exec\"), "
    "a way to need fewer root approvals. Each one general enough to reuse, "
    "with the concrete detail that makes it useful. Bad: restating the task, "
    "praise, vague advice (\"be careful\"), anything only true for this one "
    f"run. At most {MAX_LESSONS}; none is fine.\n"
)


def knowledge_prompt(facts: bool) -> str:
    """The extraction prompt: facts and lessons, or (for a job that didn't
    finish) lessons only."""
    return (
        "You read the report of an admin job an AI agent (Creator) ran on the "
        "user's own server, and extract what is worth remembering for later "
        "jobs and chats.\n\n"
        + (FACTS_RULES if facts else "")
        + LESSONS_RULES
        + "\nNever include passwords, tokens, keys or anything like a secret, "
        "even if shown. Each item one short sentence (under 25 words) that "
        "makes sense on its own; only what the report shows. If nothing is "
        "worth keeping, return [].\n"
        "Return a JSON array of objects with 'text' and 'category'. Only JSON, "
        "no fences."
    )


# Kept for callers that only want facts.
FACTS_SYSTEM_PROMPT = knowledge_prompt(True)

SKILL_MIN_CONFIDENCE = 0.6
SKILL_CATEGORY = "creator"
SKILL_SYSTEM_PROMPT = (
    "You read the full report of a FINISHED admin job an AI agent (Creator) ran "
    "on the user's own server: the task, what was done, the exact commands "
    "(with which ran as root and which needed the user's approval), what "
    "failed and how it was fixed.\n\n"
    "Decide whether it shows a REUSABLE method for similar jobs on this "
    "machine. Generalise the specific task into the method behind it (e.g. "
    "\"Install a root script and run it from cron\", \"Add a page to the web "
    "root and link it from the main page\"), keeping the machine's real paths "
    "and tools.\n\n"
    "If it does, return ONE JSON object:\n"
    '{"name": "<under 10 words>", "description": "<one sentence>", '
    '"when_to_use": "<one sentence>", "procedure": ["<3-8 concrete steps: tools, '
    'paths, which run as creator with host_exec and which as root with '
    'run_as_root, staging files in /srv/creator-helper/work, testing as creator '
    'before installing as root>"], "pitfalls": ["<what went wrong here and how '
    'to avoid it>"], "verification": ["<how to check it worked>"], '
    '"tags": ["<3-5 keywords>"], "confidence": <0.0-1.0: how reliable and reusable>}\n'
    "If it doesn't (a one-off, nothing transferable, the job barely did "
    'anything), return {"skip": "<one short reason>"}.\n'
    "Never include passwords, tokens or keys. Only JSON, no fences."
)


def _prefs(owner: Optional[str]) -> dict:
    try:
        from routes.prefs_routes import _load_for_user
        return _load_for_user(owner or None) or {}
    except Exception:
        return {}


def _first(text: str, limit: int) -> str:
    """The text on one line, cut at a sentence end before `limit` if possible."""
    # Markdown marks only: names like run_as_root keep their underscores.
    lines = [re.sub(r"^\s*(?:#+|>|[-*+]|\d+\.)\s+", "", line) for line in (text or "").splitlines()]
    flat = re.sub(r"\s+", " ", re.sub(r"`|\*\*", "", " ".join(lines))).strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[:end + 1] if end > limit // 2 else cut.rstrip() + "…").strip()


def job_summary(job_id: str, report: Dict[str, Any]) -> str:
    """The one memory a job leaves: date, id, outcome, task, what was done."""
    status = report.get("status") or "?"
    label = {"done": "finished", "stopped": "stopped by the user", "timeout": "hit its time limit",
             "limit": "hit its step limit", "error": "ended with an error"}.get(status, status)
    when = (report.get("finished_at") or report.get("started_at") or "")[:10] \
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    task = _first(report.get("asked") or "", SUMMARY_TASK_CHARS)
    done = _first(report.get("done") or report.get("worked") or "", SUMMARY_DONE_CHARS)
    text = f"Creator job {job_id} on {when} ({label}): {task}"
    if done:
        text += f" Result: {done}"
    return text


def report_text(report: Dict[str, Any]) -> str:
    """The report as the extractors read it: task, sections, commands."""
    parts = [f"Task: {report.get('asked') or ''}", f"Status: {report.get('status')}"]
    for key, heading in (("done", "What was done"), ("worked", "What worked"),
                         ("didnt_work", "What didn't work"), ("left", "What's left")):
        if report.get(key):
            parts.append(f"{heading}:\n{report[key]}")
    commands = report.get("commands") or []
    if commands:
        lines = []
        for c in commands[-30:]:
            mark = "ok" if c.get("ok") else f"failed (exit {c.get('exit_code')})"
            how = (f", as root: {c['root']}" if c.get("root")
                   else ", approved by the user" if c.get("approved") else "")
            lines.append(f"- [{c.get('tool')}] {str(c.get('command') or '')[:200]} -> {mark}{how}")
        parts.append("Commands:\n" + "\n".join(lines))
        clicks = sum(1 for c in commands if c.get("approved"))
        root_clicks = sum(1 for c in commands if c.get("root") == "approved")
        failed = sum(1 for c in commands if not c.get("ok"))
        parts.append(f"Effort: {len(commands)} commands, {failed} failed, {clicks} needed the user's "
                     f"approval ({root_clicks} of them root commands).")
    if report.get("failures"):
        parts.append("Repeated failures:\n" + "\n".join(f"- {f}" for f in report["failures"][:10]))
    return "\n\n".join(parts)[:EXTRACT_REPORT_CHARS]


class CreatorMemory:
    """The app's memory store, memory index, chat processor and skills, as
    Creator jobs use them. Every part is optional: without one, that part
    is skipped."""

    def __init__(self, memory_manager=None, memory_vector=None, chat_processor=None, skills_manager=None):
        self.memory_manager = memory_manager
        self.memory_vector = memory_vector
        self.chat_processor = chat_processor
        self.skills_manager = skills_manager

    # -- into a job ----------------------------------------------------------

    def context_messages(self, task: str, owner: Optional[str]) -> List[dict]:
        """The saved-memory messages chat would add for this task (outside
        context, user role), or [] when memory is off for the owner."""
        if self.chat_processor is None or not _prefs(owner).get("memory_enabled", True):
            return []
        try:
            preface, _, _ = self.chat_processor.build_context_preface(
                message=task, session=None, use_web=False, use_rag=False, use_memory=True,
                owner=owner or None, agent_mode=True, use_skills=False,
            )
        except Exception:
            logger.warning("Creator: could not load memories for the job", exc_info=True)
            return []
        msgs = [m for m in preface
                if m.get("role") != "system"
                and str((m.get("metadata") or {}).get("source", "")).startswith("saved memory")]
        lessons = self.lessons_for(task, owner, already=" ".join(str(m.get("content", "")) for m in msgs))
        if lessons:
            from src.prompt_security import untrusted_context_message
            msgs.append(untrusted_context_message(
                "saved memory: creator lessons",
                "Lessons from earlier Creator jobs on this machine (learned by doing; "
                "use them to avoid retries and unneeded root approvals):\n"
                + "\n".join(f"- {t}" for t in lessons)))
        return msgs

    def lessons_for(self, task: str, owner: Optional[str], already: str = "") -> List[str]:
        """The owner's Creator lessons that match the task, best first,
        leaving out any the memory block already has."""
        if self.memory_manager is None:
            return []
        try:
            entries = [e for e in self.memory_manager.load(owner=owner or None)
                       if e.get("category") == LESSON_CATEGORY]
        except Exception:
            return []
        if not entries:
            return []
        picked = []
        retrieve = getattr(self.chat_processor, "_hybrid_retrieve", None)
        if callable(retrieve):
            try:
                picked = retrieve(task, entries, k=MAX_LESSONS_IN)
            except Exception:
                picked = []
        if not picked:
            words = set(re.findall(r"[a-z0-9_.-]{3,}", task.lower()))
            scored = sorted(((len(words & set(re.findall(r"[a-z0-9_.-]{3,}", e["text"].lower()))), e)
                             for e in entries), key=lambda x: -x[0])
            picked = [e for score, e in scored if score > 0][:MAX_LESSONS_IN]
        return [e["text"] for e in picked if e.get("text") and e["text"] not in already]

    # -- out of a job --------------------------------------------------------

    async def after_job(self, job_id: str, owner: Optional[str], report: Dict[str, Any],
                        endpoint_url: str, model: str, headers: Optional[dict],
                        tool_calls: int = 0, rounds: int = 0) -> Dict[str, Any]:
        """Everything a job leaves behind. Never raises; returns what it did
        (for the job's audit log)."""
        prefs = _prefs(owner)
        out: Dict[str, Any] = {"summary": None, "facts": 0, "lessons": 0, "skill": None}
        if self.memory_manager is None or not prefs.get("memory_enabled", True):
            out["skipped"] = "memory off"
        else:
            try:
                # Skipped only if this job already has one: two jobs with the
                # same task are still two jobs (9d).
                out["summary"] = self.save_memory(job_summary(job_id, report), owner, job_id, per_job=True)
            except Exception:
                logger.warning("Creator: could not save the job's memory", exc_info=True)
            # Facts only from a finished job; lessons from any job that did
            # something (a failure teaches too).
            finished = report.get("status") == "done"
            if prefs.get("auto_memory", True) and (finished or report.get("commands")):
                try:
                    facts, lessons = await self.extract_knowledge(
                        job_id, owner, report, endpoint_url, model, headers, facts=finished)
                    out["facts"], out["lessons"] = facts, lessons
                except Exception:
                    logger.warning("Creator: fact/lesson extraction failed", exc_info=True)
        # Why a job leaves no skill is always recorded (the audit log's
        # `remembered` line), so "none" can be told apart from a bug.
        if self.skills_manager is None:
            out["skill_note"] = "skills aren't available"
        elif report.get("status") != "done":
            out["skill_note"] = "only finished jobs teach skills"
        elif not prefs.get("auto_skills", True):
            out["skill_note"] = "auto skills is off in your preferences"
        elif tool_calls < SKILL_MIN_COMMANDS and rounds < 2:
            out["skill_note"] = "too few steps to be a procedure"
        else:
            try:
                learned = await self.learn_skill(job_id, owner, report, endpoint_url, model, headers)
                out["skill"], out["skill_note"] = learned["skill"], learned["note"]
            except Exception as e:
                logger.warning("Creator: skill extraction failed", exc_info=True)
                out["skill_note"] = f"failed: {str(e)[:200]}"
        return out

    def _task_endpoint(self, endpoint_url, model, headers, owner):
        try:
            from src.task_endpoint import resolve_task_endpoint
            return resolve_task_endpoint(endpoint_url, model, headers, owner=owner or None)
        except Exception:
            return endpoint_url, model, headers

    def save_memory(self, text: str, owner: Optional[str], job_id: str,
                    category: str = MEMORY_CATEGORY, per_job: bool = False) -> Optional[str]:
        """Adds one memory unless it's a duplicate (with `per_job`: unless this
        job already left one in `category`). Returns its id or None."""
        from services.memory.memory_extractor import _is_text_duplicate
        entries = self.memory_manager.load_all_for_update()
        mine = [e for e in entries if e.get("owner") == (owner or None) or e.get("owner") is None] \
            if owner else entries
        if per_job:
            if any(e.get("session_id") == job_id and e.get("category") == category for e in mine):
                return None
        elif self.memory_manager.find_duplicates(text, mine) or _is_text_duplicate(text, mine, threshold=0.8):
            return None
        entry = self.memory_manager.add_entry(text, source=MEMORY_SOURCE, category=category, owner=owner or None)
        entry["session_id"] = job_id
        entries.append(entry)
        self.memory_manager.save(entries)
        if self.memory_vector is not None and getattr(self.memory_vector, "healthy", False):
            try:
                self.memory_vector.add(entry["id"], text)
            except Exception:
                logger.debug("Creator: memory vector add failed", exc_info=True)
        try:
            from src.event_bus import fire_event
            fire_event("memory_added", owner or None)
        except Exception:
            pass
        return entry["id"]

    async def extract_knowledge(self, job_id, owner, report, endpoint_url, model, headers,
                                facts: bool = True):
        """Facts (if `facts`) and lessons from the report; returns how many of
        each were saved."""
        from services.memory.memory_extractor import _parse_extraction_json
        from src.llm_core import llm_call_async
        url, mdl, hdrs = self._task_endpoint(endpoint_url, model, headers, owner)
        if not url or not mdl:
            return 0, 0
        raw = await llm_call_async(url, mdl, [
            {"role": "system", "content": knowledge_prompt(facts)},
            {"role": "user", "content": "Job report:\n\n" + report_text(report)
             + "\n\nReturn the JSON array now (or [] if nothing is worth keeping)."},
        ], temperature=0.1, max_tokens=2048, headers=hdrs)
        n_facts = n_lessons = 0
        for item in (_parse_extraction_json(raw) or []):
            text = (item.get("text", "") if isinstance(item, dict) else str(item)).strip()
            category = item.get("category", "fact") if isinstance(item, dict) else "fact"
            if len(text) < 8 or len(text) > 300:
                continue
            if category == LESSON_CATEGORY:
                if n_lessons >= MAX_LESSONS:
                    continue
                if not text.startswith(LESSON_PREFIX):
                    text = LESSON_PREFIX + text
                if self.save_memory(text, owner, job_id, category=LESSON_CATEGORY):
                    n_lessons += 1
                continue
            if not facts or n_facts >= MAX_FACTS:
                continue
            if category not in ("fact", "project", "preference"):
                category = "fact"
            if self.save_memory(text, owner, job_id, category=category):
                n_facts += 1
        return n_facts, n_lessons

    async def extract_facts(self, job_id, owner, report, endpoint_url, model, headers) -> int:
        """Facts and lessons from a finished job; returns the number of facts."""
        facts, _ = await self.extract_knowledge(job_id, owner, report, endpoint_url, model, headers)
        return facts

    async def extract_skill(self, job_id, owner, report, endpoint_url, model, headers,
                            tool_calls: int = 0, rounds: int = 0) -> Optional[str]:
        """The skill's name, or None. Why not is in learn_skill's note."""
        return (await self.learn_skill(job_id, owner, report, endpoint_url, model, headers))["skill"]

    async def learn_skill(self, job_id, owner, report, endpoint_url, model, headers) -> Dict[str, Any]:
        """Learn a skill from a finished job's whole report. Returns
        {"skill": name or None, "status": "published"/"draft", "note": why},
        so a job that leaves no skill says why (it declined, too unsure, a
        duplicate, the model failed). Chat's extractor isn't used: it cuts
        each message at 500 characters, which left it only the task."""
        from services.memory.skill_extractor import _extract_json_object, _has_duplicate_title
        from src.llm_core import llm_call_async
        url, mdl, hdrs = self._task_endpoint(endpoint_url, model, headers, owner)
        if not url or not mdl:
            return {"skill": None, "note": "no model to ask"}
        try:
            raw = await llm_call_async(url, mdl, [
                {"role": "system", "content": SKILL_SYSTEM_PROMPT},
                {"role": "user", "content": "Job report:\n\n" + report_text(report)
                 + "\n\nReturn the JSON object now."},
            ], temperature=0.1, max_tokens=4096, headers=hdrs)
        except Exception as e:
            return {"skill": None, "note": f"the model call failed: {str(e)[:200]}"}
        try:
            from src.text_helpers import strip_think
            raw = strip_think(raw or "", prose=True, prompt_echo=True)
        except Exception:
            pass
        data = _extract_json_object(raw or "")
        if not isinstance(data, dict):
            return {"skill": None, "note": "the model's answer had no JSON object: " + (raw or "")[:160]}
        if data.get("skip"):
            return {"skill": None, "note": f"declined: {str(data['skip'])[:200]}"}
        name = str(data.get("name") or "").strip()
        procedure = [str(x) for x in (data.get("procedure") or []) if str(x).strip()]
        if not name or len(procedure) < 2:
            return {"skill": None, "note": "the model's skill had no name or too few steps"}
        try:
            confidence = float(data.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        if confidence < SKILL_MIN_CONFIDENCE:
            return {"skill": None, "note": f"too unsure ({confidence:.2f} < {SKILL_MIN_CONFIDENCE})",
                    "name": name}
        from services.memory.skills import slugify
        existing = self.skills_manager.load(owner=owner or None)
        if _has_duplicate_title(existing, name) or any(sk.get("name") == slugify(name) for sk in existing):
            return {"skill": None, "note": f"a skill called {name!r} already exists"}
        status = "published" if _prefs(owner).get("auto_approve_skills", True) else "draft"
        entry = self.skills_manager.add_skill(
            name=name,
            description=str(data.get("description") or "").strip(),
            when_to_use=str(data.get("when_to_use") or "").strip(),
            procedure=procedure,
            pitfalls=[str(x) for x in (data.get("pitfalls") or []) if str(x).strip()],
            verification=[str(x) for x in (data.get("verification") or []) if str(x).strip()],
            tags=[str(x) for x in (data.get("tags") or [])][:8],
            source=SKILL_SOURCE, confidence=confidence, session_id=job_id,
            owner=owner or None, category=SKILL_CATEGORY, status=status,
        )
        if entry.get("_deduped"):
            return {"skill": None, "note": f"nearly the same as the existing skill {entry.get('_duplicate_of')!r}"}
        try:
            from src.event_bus import fire_event
            fire_event("skill_added", owner or None)
        except Exception:
            pass
        return {"skill": entry.get("name"), "status": status, "note": f"saved as {status}"}


# ---------------------------------------------------------------------------
# creator_jobs: read-only access to Creator jobs from normal chat
# ---------------------------------------------------------------------------

CREATOR_JOBS_TOOL = "creator_jobs"
_LIST_DEFAULT = 10
_LIST_MAX = 50
_READ_MAX_CHARS = 20000


def _parse_args(content) -> dict:
    if isinstance(content, dict):
        return content
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    if re.fullmatch(r"cr-[0-9a-f]{12}", raw):
        return {"action": "read", "id": raw}
    return {"action": "list"}


def do_creator_jobs(content, owner: Optional[str] = None) -> dict:
    """{"action": "list", "limit": 10} or {"action": "read", "id": "cr-…"}.
    Only the caller's own jobs."""
    from src.creator_mode import get_active_manager, is_valid_job_id
    manager = get_active_manager()
    if manager is None:
        return {"error": "Creator mode isn't available.", "exit_code": 1}
    args = _parse_args(content)
    action = str(args.get("action") or "list").lower()
    if action == "list":
        try:
            limit = max(1, min(int(args.get("limit") or _LIST_DEFAULT), _LIST_MAX))
        except (TypeError, ValueError):
            limit = _LIST_DEFAULT
        jobs = manager.list_jobs(owner or "", limit=limit)
        if not jobs:
            return {"output": "You have no Creator jobs yet.", "exit_code": 0, "jobs": []}
        lines = [f"- {j['job_id']} · {(j.get('started_at') or '')[:16].replace('T', ' ')} · {j['status']} · "
                 + (j.get("task") or "").split("\n")[0][:150] for j in jobs]
        return {"output": "Your Creator jobs, newest first (read one with action=read and its id):\n"
                          + "\n".join(lines), "exit_code": 0, "jobs": jobs}
    if action == "read":
        job_id = str(args.get("id") or args.get("job_id") or "").strip()
        if not is_valid_job_id(job_id):
            return {"error": 'Give the job id, e.g. {"action": "read", "id": "cr-0123456789ab"}.', "exit_code": 1}
        job = manager.get_job(job_id)
        if job is None or job.get("owner", "") != (owner or ""):
            return {"error": f"No Creator job {job_id} of yours.", "exit_code": 1}
        if job.get("report"):
            text = job["report"]
        else:
            notes = (job.get("state") or {}).get("notes") or []
            text = (f"# Creator job {job_id} ({job['status']}, no report yet)\n\nTask: {job['task']}\n\n"
                    + "\n".join(f"- {n.get('text')}" for n in notes[-20:]))
        if len(text) > _READ_MAX_CHARS:
            text = text[:_READ_MAX_CHARS] + "\n[…report cut]"
        return {"output": text, "exit_code": 0}
    return {"error": 'Unknown action. Use {"action": "list"} or {"action": "read", "id": "cr-…"}.',
            "exit_code": 1}
