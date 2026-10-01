# src/creator_mode.py
"""
Creator mode engine.

A Creator job takes one task and works through it with the regular agent loop
(src/agent_loop.py) running as a background job, with the round and tool-call
caps set very high. Every job is a row in the `creator_jobs` table: task,
status, start/end time, the final report and an event log of what the agent
did. Routes live in routes/creator_routes.py.

Phase 1 skeleton only: no Creator-specific instructions, failure tracking,
time limit or host access yet (see docs/creator-plan.md).
"""
import asyncio
import json
import logging
import re
import uuid
from typing import Callable, Dict, List, Optional, Set

from core.database import CreatorJob, SessionLocal, utcnow_naive

logger = logging.getLogger(__name__)

# "Very high" caps — Creator runs are expected to take many steps. The stop
# route is the way to end a run early.
CREATOR_MAX_ROUNDS = 500
CREATOR_MAX_TOOL_CALLS = 2000

# Keep the stored event log bounded: per-event output is truncated and only
# the most recent events are kept.
_EVENT_OUTPUT_CHARS = 2000
_MAX_EVENTS = 2000
# Flush the event log to the database at most this often (seconds).
_FLUSH_INTERVAL_S = 3.0

_JOB_ID_RE = re.compile(r"^cr-[a-f0-9]{12}$")

CREATOR_SYSTEM_PROMPT = (
    "You are running in Creator mode: you have been given a task to complete "
    "on your own, using the tools available to you. Work through the task "
    "step by step until it is done. When you finish, write a final report: "
    "what was asked, what you did, what worked, what didn't, and what is left."
)


def is_valid_job_id(job_id: str) -> bool:
    return isinstance(job_id, str) and bool(_JOB_ID_RE.fullmatch(job_id))


def new_job_id() -> str:
    return f"cr-{uuid.uuid4().hex[:12]}"


def _truncate(value, limit: int = _EVENT_OUTPUT_CHARS):
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[:limit] + f"... [{len(value) - limit} more chars]"


def privilege_disabled_tools(privs: Optional[dict]) -> Set[str]:
    """Tools to switch off for a Creator run, from the owner's privileges.
    Mirrors the per-user privilege gates in routes/chat_routes.py."""
    disabled: Set[str] = set()
    if not privs:
        return disabled
    if not privs.get("can_use_bash", True):
        disabled.update({"bash", "python", "read_file", "write_file"})
    if not privs.get("can_use_browser", True):
        try:
            from routes.chat_routes import _BROWSER_MCP_TOOLS
            disabled.update(_BROWSER_MCP_TOOLS)
        except Exception:
            logger.debug("Could not load browser tool list", exc_info=True)
    if not privs.get("can_use_documents", True):
        disabled.update({"create_document", "edit_document", "update_document", "suggest_document"})
    if not privs.get("can_generate_images", True):
        disabled.add("generate_image")
    if not privs.get("can_manage_memory", True):
        disabled.update({"manage_memory", "manage_skills"})
    return disabled


def _event_from_sse(data: dict) -> Optional[dict]:
    """Reduce one agent-loop SSE payload to a compact event-log entry, or None
    for events that aren't worth keeping (text deltas, metrics, ...)."""
    etype = data.get("type")
    if etype == "agent_step":
        return {"type": "round", "round": data.get("round")}
    if etype == "tool_start":
        return {
            "type": "tool_start",
            "tool": data.get("tool"),
            "command": _truncate(data.get("command")),
        }
    if etype == "tool_output":
        output = data.get("output")
        if output is None:
            output = data.get("stdout") or data.get("result")
        return {
            "type": "tool_output",
            "tool": data.get("tool"),
            "command": _truncate(data.get("command")),
            "output": _truncate(output if isinstance(output, str) else json.dumps(output, default=str)),
            "exit_code": data.get("exit_code"),
        }
    if etype in ("rounds_exhausted", "budget_exceeded"):
        return {k: v for k, v in data.items() if k in ("type", "rounds", "limit", "used")}
    return None


class CreatorManager:
    """Runs Creator jobs as asyncio background tasks and records them in the
    database. `session_factory` and `agent_loop` are injectable for tests."""

    def __init__(self, session_factory: Callable = SessionLocal, agent_loop: Optional[Callable] = None):
        self._session_factory = session_factory
        self._agent_loop = agent_loop
        self._tasks: Dict[str, asyncio.Task] = {}
        self._mark_orphans()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _mark_orphans(self) -> None:
        """Jobs left 'running' by a previous process can never finish."""
        try:
            db = self._session_factory()
            try:
                rows = db.query(CreatorJob).filter(CreatorJob.status == "running").all()
                for row in rows:
                    row.status = "interrupted"
                    row.finished_at = row.finished_at or utcnow_naive()
                    row.error = row.error or "Server restarted while the job was running."
                if rows:
                    db.commit()
                    logger.info("Creator: marked %d orphaned job(s) as interrupted", len(rows))
            finally:
                db.close()
        except Exception:
            logger.warning("Creator: could not check for orphaned jobs", exc_info=True)

    def _update(self, job_id: str, **fields) -> None:
        db = self._session_factory()
        try:
            row = db.query(CreatorJob).filter(CreatorJob.id == job_id).first()
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            db.commit()
        finally:
            db.close()

    def get_job(self, job_id: str) -> Optional[dict]:
        if not is_valid_job_id(job_id):
            return None
        db = self._session_factory()
        try:
            row = db.query(CreatorJob).filter(CreatorJob.id == job_id).first()
            if row is None:
                return None
            try:
                events = json.loads(row.events) if row.events else []
            except (TypeError, ValueError):
                events = []
            return {
                "id": row.id,
                "owner": row.owner or "",
                "task": row.task,
                "status": row.status,
                "started_at": row.started_at.isoformat() + "Z" if row.started_at else None,
                "finished_at": row.finished_at.isoformat() + "Z" if row.finished_at else None,
                "report": row.report,
                "error": row.error,
                "model": row.model,
                "events": events,
            }
        finally:
            db.close()

    def is_running(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start_job(
        self,
        task: str,
        endpoint_url: str,
        model: str,
        headers: Optional[dict] = None,
        owner: str = "",
        disabled_tools: Optional[Set[str]] = None,
    ) -> str:
        job_id = new_job_id()
        db = self._session_factory()
        try:
            db.add(CreatorJob(
                id=job_id,
                owner=owner or None,
                task=task,
                status="running",
                started_at=utcnow_naive(),
                model=model,
                events="[]",
            ))
            db.commit()
        finally:
            db.close()

        bg = asyncio.create_task(self._run(
            job_id, task, endpoint_url, model, headers or {}, owner, set(disabled_tools or ())
        ))
        self._tasks[job_id] = bg
        bg.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))
        logger.info("Creator: started job %s (owner=%r, model=%s)", job_id, owner, model)
        return job_id

    def stop_job(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    # ------------------------------------------------------------------
    # The run itself
    # ------------------------------------------------------------------

    async def _run(
        self,
        job_id: str,
        task: str,
        endpoint_url: str,
        model: str,
        headers: dict,
        owner: str,
        disabled_tools: Set[str],
    ) -> None:
        events: List[dict] = []
        full_text = ""
        status = "done"
        error: Optional[str] = None
        loop = asyncio.get_running_loop()
        last_flush = loop.time()

        def _flush():
            self._update(job_id, events=json.dumps(events[-_MAX_EVENTS:], default=str))

        agent_loop = self._agent_loop
        if agent_loop is None:
            from src.agent_loop import stream_agent_loop as agent_loop

        messages = [
            {"role": "system", "content": CREATOR_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        try:
            async for chunk in agent_loop(
                endpoint_url=endpoint_url,
                model=model,
                messages=messages,
                headers=headers,
                max_rounds=CREATOR_MAX_ROUNDS,
                max_tool_calls=CREATOR_MAX_TOOL_CALLS,
                session_id=job_id,
                owner=owner or None,
                disabled_tools=disabled_tools,
                workload="background",
            ):
                if not isinstance(chunk, str) or not chunk.startswith("data: "):
                    continue
                body = chunk[6:].strip()
                if not body or body == "[DONE]":
                    continue
                try:
                    data = json.loads(body)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue

                if "delta" in data:
                    if not data.get("thinking") and isinstance(data["delta"], str):
                        full_text += data["delta"]
                    continue

                event = _event_from_sse(data)
                if event is not None:
                    event["at"] = utcnow_naive().isoformat() + "Z"
                    events.append(event)

                approval = data.get("ask_user")
                if data.get("type") == "tool_output" and isinstance(approval, dict) \
                        and approval.get("kind") == "tool_approval":
                    # Nobody is watching a background run to approve the
                    # action. Retire the approval (the action never ran) and
                    # end the job as blocked. Phase 2 replaces this with a
                    # real pause-and-notify.
                    self._retire_approval(approval.get("approval_id"), owner, job_id)
                    status = "blocked"
                    error = (
                        f"{data.get('tool') or 'A tool'} needed your approval, which a "
                        "background run can't give. The action was not run."
                    )
                    events.append({"type": "blocked", "tool": data.get("tool"),
                                   "at": utcnow_naive().isoformat() + "Z"})
                    break

                if loop.time() - last_flush >= _FLUSH_INTERVAL_S:
                    _flush()
                    last_flush = loop.time()
        except asyncio.CancelledError:
            status = "stopped"
            error = "Stopped by user."
            events.append({"type": "stopped", "at": utcnow_naive().isoformat() + "Z"})
        except Exception as e:
            logger.error("Creator job %s failed: %s", job_id, e, exc_info=True)
            status = "error"
            error = str(e)[:2000]

        try:
            self._update(
                job_id,
                status=status,
                error=error,
                report=full_text.strip() or None,
                events=json.dumps(events[-_MAX_EVENTS:], default=str),
                finished_at=utcnow_naive(),
            )
        except Exception:
            logger.error("Creator job %s: could not save final state", job_id, exc_info=True)
        logger.info("Creator: job %s finished with status %s", job_id, status)

    @staticmethod
    def _retire_approval(approval_id, owner: str, job_id: str) -> None:
        if not approval_id:
            return
        try:
            from src.tool_approvals import tool_approval_store
            tool_approval_store.consume(approval_id, decision="deny", owner=owner, session_id=job_id)
        except Exception:
            logger.debug("Could not retire Creator approval", exc_info=True)
