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
SUMMARY_TASK_CHARS = 160
SUMMARY_DONE_CHARS = 280
# What the fact extractor and skill extractor are shown of a report.
EXTRACT_REPORT_CHARS = 6000

FACTS_SYSTEM_PROMPT = (
    "You read the report of a finished admin job on the user's own server and "
    "extract DURABLE facts about that machine and its setup, useful for later "
    "jobs and chats.\n\n"
    "Good: where things are (web root, config files, log paths), which "
    "services/software and versions are installed, what was changed and is "
    "still in effect, how the user wants things done there.\n"
    "Bad: one-off command output, failed attempts, temporary states, anything "
    "about the AI itself, passwords/tokens/keys or anything like a secret "
    "(never include those, even if shown).\n\n"
    f"Rules: at most {MAX_FACTS} facts; each one short sentence (under 20 words) "
    "that makes sense on its own; only what the report shows to be true; if "
    "nothing durable, return [].\n"
    "Return a JSON array of objects with 'text' and 'category' "
    "('fact', 'project' or 'preference'). Only JSON, no fences."
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
            lines.append(f"- [{c.get('tool')}] {str(c.get('command') or '')[:200]} -> {mark}")
        parts.append("Commands:\n" + "\n".join(lines))
    return "\n\n".join(parts)[:EXTRACT_REPORT_CHARS]


class _JobTranscript:
    """Just enough of a chat session for chat's skill extractor."""

    def __init__(self, job_id: str, owner: Optional[str], task: str, text: str):
        self.session_id = job_id
        self.owner = owner
        self._messages = [{"role": "user", "content": task},
                          {"role": "assistant", "content": text}]

    def get_context_messages(self):
        return list(self._messages)


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
        return [m for m in preface
                if m.get("role") != "system"
                and str((m.get("metadata") or {}).get("source", "")).startswith("saved memory")]

    # -- out of a job --------------------------------------------------------

    async def after_job(self, job_id: str, owner: Optional[str], report: Dict[str, Any],
                        endpoint_url: str, model: str, headers: Optional[dict],
                        tool_calls: int = 0, rounds: int = 0) -> Dict[str, Any]:
        """Everything a job leaves behind. Never raises; returns what it did
        (for the job's audit log)."""
        prefs = _prefs(owner)
        out: Dict[str, Any] = {"summary": None, "facts": 0, "skill": None}
        if self.memory_manager is None or not prefs.get("memory_enabled", True):
            out["skipped"] = "memory off"
        else:
            try:
                out["summary"] = self.save_memory(job_summary(job_id, report), owner, job_id)
            except Exception:
                logger.warning("Creator: could not save the job's memory", exc_info=True)
            if report.get("status") == "done" and prefs.get("auto_memory", True):
                try:
                    out["facts"] = await self.extract_facts(job_id, owner, report, endpoint_url, model, headers)
                except Exception:
                    logger.warning("Creator: fact extraction failed", exc_info=True)
        if (self.skills_manager is not None and report.get("status") == "done"
                and prefs.get("auto_skills", True)
                and (tool_calls >= SKILL_MIN_COMMANDS or rounds >= 2)):
            try:
                out["skill"] = await self.extract_skill(job_id, owner, report, endpoint_url, model, headers,
                                                        tool_calls, rounds)
            except Exception:
                logger.warning("Creator: skill extraction failed", exc_info=True)
        return out

    def _task_endpoint(self, endpoint_url, model, headers, owner):
        try:
            from src.task_endpoint import resolve_task_endpoint
            return resolve_task_endpoint(endpoint_url, model, headers, owner=owner or None)
        except Exception:
            return endpoint_url, model, headers

    def save_memory(self, text: str, owner: Optional[str], job_id: str,
                    category: str = MEMORY_CATEGORY) -> Optional[str]:
        """Adds one memory unless it's a duplicate. Returns its id or None."""
        from services.memory.memory_extractor import _is_text_duplicate
        entries = self.memory_manager.load_all_for_update()
        mine = [e for e in entries if e.get("owner") == (owner or None) or e.get("owner") is None] \
            if owner else entries
        if self.memory_manager.find_duplicates(text, mine) or _is_text_duplicate(text, mine, threshold=0.8):
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

    async def extract_facts(self, job_id, owner, report, endpoint_url, model, headers) -> int:
        from services.memory.memory_extractor import _parse_extraction_json
        from src.llm_core import llm_call_async
        url, mdl, hdrs = self._task_endpoint(endpoint_url, model, headers, owner)
        if not url or not mdl:
            return 0
        raw = await llm_call_async(url, mdl, [
            {"role": "system", "content": FACTS_SYSTEM_PROMPT},
            {"role": "user", "content": "Job report:\n\n" + report_text(report)
             + "\n\nReturn the JSON array of durable facts now (or [] if none)."},
        ], temperature=0.1, max_tokens=2048, headers=hdrs)
        added = 0
        for fact in (_parse_extraction_json(raw) or [])[:MAX_FACTS]:
            text = fact.get("text", "") if isinstance(fact, dict) else str(fact)
            category = fact.get("category", "fact") if isinstance(fact, dict) else "fact"
            if category not in ("fact", "project", "preference"):
                category = "fact"
            text = text.strip()
            if len(text) < 8 or len(text) > 300:
                continue
            if self.save_memory(text, owner, job_id, category=category):
                added += 1
        return added

    async def extract_skill(self, job_id, owner, report, endpoint_url, model, headers,
                            tool_calls: int, rounds: int) -> Optional[str]:
        from services.memory.skill_extractor import maybe_extract_skill
        url, mdl, hdrs = self._task_endpoint(endpoint_url, model, headers, owner)
        transcript = _JobTranscript(job_id, owner, report.get("asked") or "", report_text(report))
        entry = await maybe_extract_skill(
            transcript, self.skills_manager, url, mdl, hdrs,
            max(rounds, 2), tool_calls, owner=owner or None, source=SKILL_SOURCE,
        )
        return (entry or {}).get("name") or (entry or {}).get("id")


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
