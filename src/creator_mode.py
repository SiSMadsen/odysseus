# src/creator_mode.py
"""
Creator mode engine.

A Creator job takes one task and works through it with the regular agent loop
(src/agent_loop.py) as a background job. Every job is a row in the
`creator_jobs` table: task, status, start/end time, the report, an event log,
and a JSON `state` (pause request, progress notes, failure tracker, command
log, structured report). Routes live in routes/creator_routes.py.

A run is a series of agent-loop *segments*:
- A segment ends when the model finishes, when it hits SEGMENT_ROUNDS
  (checkpoint), or when it needs the user (approval card, ask_user question,
  or a final `STATUS: BLOCKED` line).
- Needing the user pauses the job: status "paused" until /resume. On resume
  (or at a checkpoint) the next segment starts from a fresh context rebuilt
  from the progress notes, command log and failures, so a long run can't lose
  its place. Approved actions are run by Creator itself, then the run goes on.
- The time limit covers the whole run, paused time included, and stop works
  in any state.

Safety net (src/creator_safety.py): time limit, stop that also kills the
job's shell, redacted audit log, protected paths, one job at a time (a paused
job counts as running). Secrets: src/creator_secrets.py.
"""
import asyncio
import json
import logging
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

from core.database import CreatorJob, SessionLocal, utcnow_naive
from src.creator_safety import (
    AuditLog,
    Redactor,
    kill_job_shell,
    make_protected_action_check,
)
from src.creator_secrets import SecretStore, secret_store_tripwire_paths

logger = logging.getLogger(__name__)

# The manager the app runs, so the get_secret tool can find the running job.
_active_manager: Optional["CreatorManager"] = None


def get_active_manager() -> Optional["CreatorManager"]:
    return _active_manager


# Whole-run caps. The time limit and the stop route are what normally end a
# long run.
CREATOR_MAX_ROUNDS = 500
CREATOR_MAX_TOOL_CALLS = 2000
# Rounds per segment. At the end of one, the next starts from a context
# rebuilt from notes + command log (a checkpoint).
SEGMENT_ROUNDS = 30
# Same command failing the same way this many times → refused from then on.
FAILURE_LIMIT = 3
# Creator writes its own checkpoint note every this many tool calls, so notes
# exist even if the model writes none.
AUTO_NOTE_EVERY = 10

_SECONDS_PER_MINUTE = 60  # patched by tests
DEFAULT_MAX_MINUTES = 60
MIN_MAX_MINUTES = 1
MAX_MAX_MINUTES = 1440

# Keep the stored event log bounded: per-event output is truncated and only
# the most recent events are kept. The audit log on disk keeps more.
_EVENT_OUTPUT_CHARS = 2000
_AUDIT_OUTPUT_CHARS = 100_000
_MAX_EVENTS = 2000
_MAX_NOTES = 300
_MAX_COMMANDS = 2000
# Flush the event log / state to the database at most this often (seconds).
_FLUSH_INTERVAL_S = 3.0

_JOB_ID_RE = re.compile(r"^cr-[a-f0-9]{12}$")
_NOTE_RE = re.compile(r"^[ \t>*-]*PROGRESS:\s*(.+?)\s*$", re.M | re.I)
_STATUS_RE = re.compile(r"^[ \t>*]*STATUS:\s*(DONE|BLOCKED)\b[ \t:—-]*(.*?)\s*$", re.M | re.I)

# Statuses in which a job still holds the one-job slot.
ACTIVE_STATUSES = ("running", "paused")

APPROVAL_DECISIONS = ("approve_once", "approve_job", "deny")

CREATOR_SYSTEM_PROMPT = """\
You are running in Creator mode: you have been given a task to complete on \
your own on this server, using the tools available to you. Nobody is \
watching live. Keep working until the task is done.

How to work:
- When something fails, don't give up. Read the error, work out why, and try \
a different approach. Keep going until it works or you have run out of \
reasonable approaches.
- Keep a list of what you tried. After each meaningful step write one short \
line starting with `PROGRESS:` saying what you did and what you learned, for \
example `PROGRESS: apt install foo failed (no such package); trying pip next`. \
These notes are saved and shown back to you when the run continues, so keep \
them short and factual.
- Never repeat a command that already failed the same way. A command that \
fails the same way 3 times is refused from then on.
- Only stop to ask the user when you are blocked on something only they can \
give you: a missing password, token or credential (or a secret that is \
switched off), access you don't have, or a decision between options that \
changes the outcome. Then call the `ask_user` tool with one clear question \
and at least two options (the user can also answer in their own words). \
Don't ask permission for routine steps.
- Use `get_secret` to fetch stored passwords and tokens by name. Never print \
them or write them into files, notes or the report.
- Some actions need the user's OK first (protected paths, or actions after \
reading untrusted content). The run pauses for that by itself; carry on \
when it continues.

When you are finished, write a final report with exactly these headings:
## What was done
## What worked
## What didn't work
## What's left
and end with the line `STATUS: DONE`. If you are genuinely blocked and could \
not ask with ask_user, end with `STATUS: BLOCKED: <what you need from the \
user>` instead.
"""


class CreatorBusyError(RuntimeError):
    """Another Creator job is already running (or paused)."""


class CreatorResumeError(ValueError):
    """A resume request that doesn't fit the job's pause."""


class CreatorModelError(RuntimeError):
    """The model request failed (the agent loop sent a failed agent_terminal)."""


class CreatorNotPausedError(CreatorResumeError):
    """Resume sent to a job that isn't waiting (or is already resuming)."""


def is_valid_job_id(job_id: str) -> bool:
    return isinstance(job_id, str) and bool(_JOB_ID_RE.fullmatch(job_id))


def new_job_id() -> str:
    return f"cr-{uuid.uuid4().hex[:12]}"


def clamp_minutes(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_MINUTES
    return max(MIN_MAX_MINUTES, min(MAX_MAX_MINUTES, n))


def default_max_minutes() -> int:
    try:
        from src.settings import get_setting
        return clamp_minutes(get_setting("creator_max_minutes", DEFAULT_MAX_MINUTES))
    except Exception:
        return DEFAULT_MAX_MINUTES


def _now_iso() -> str:
    return utcnow_naive().isoformat() + "Z"


def _truncate(value, limit: int = _EVENT_OUTPUT_CHARS):
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[:limit] + f"... [{len(value) - limit} more chars]"


def _tail(value: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) <= limit:
        return value or ""
    return "..." + value[-limit:]


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


def _tool_output_text(data: dict) -> str:
    output = data.get("output")
    if output is None:
        output = data.get("stdout") or data.get("result") or data.get("error")
    if output is None:
        return ""
    return output if isinstance(output, str) else json.dumps(output, default=str)


def _sse_error_text(chunk: str) -> str:
    """The message from an `event: error` SSE chunk, e.g.
    'Anthropic returned HTTP 400: `temperature` is deprecated for this model.'"""
    for line in chunk.splitlines():
        if line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except ValueError:
                return _truncate(line[6:], 1000)
            if isinstance(data, dict):
                text = data.get("text") or data.get("message") or data.get("error")
                if text:
                    return _truncate(str(text), 1000)
                if data.get("status"):
                    return f"HTTP {data['status']}"
            return _truncate(str(data), 1000)
    return "no details from the provider"


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
        return {
            "type": "tool_output",
            "tool": data.get("tool"),
            "command": _truncate(data.get("command")),
            "output": _truncate(_tool_output_text(data)),
            "exit_code": data.get("exit_code"),
        }
    if etype in ("rounds_exhausted", "budget_exceeded"):
        return {k: v for k, v in data.items() if k in ("type", "rounds", "limit", "used")}
    return None


# ---------------------------------------------------------------------------
# Failure tracking
# ---------------------------------------------------------------------------

# Tools whose input is "<target>\n<content to write>". The report logs the
# target and size, not the whole body (found in the first real run: a
# write_file line held the entire file, flattened onto one line).
_CONTENT_WRITING_TOOLS = frozenset({
    "write_file", "edit_file", "apply_patch",
    "create_document", "update_document", "edit_document", "suggest_document",
})


def _command_for_log(tool: str, content: Any) -> str:
    """The command as the report and command log show it: exact for shells
    and most tools; target + size for tools that write a body of content."""
    if tool in _CONTENT_WRITING_TOOLS and isinstance(content, str) and "\n" in content.strip():
        first, rest = content.lstrip().split("\n", 1)  # size = the body as written
        return f"{first.strip()} (+{len(rest)} chars of content)"
    return _norm_command(content)


def _norm_command(content: Any) -> str:
    text = content if isinstance(content, str) else json.dumps(content, sort_keys=True, default=str)
    return re.sub(r"\s+", " ", text or "").strip()


def _failure_text(result: dict) -> str:
    text = result.get("error") or result.get("stderr") or result.get("output") or ""
    if not isinstance(text, str):
        text = json.dumps(text, default=str)
    # "The same way": same exit code and the same message, ignoring numbers
    # (pids, timings, line numbers) and whitespace.
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).strip()[:300]


def _is_failure(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("blocked") or result.get("approval_required"):
        return False
    code = result.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return True
    return code is None and bool(result.get("error"))


# ---------------------------------------------------------------------------
# Structured report
# ---------------------------------------------------------------------------

_SECTION_HEADINGS = (
    ("done", re.compile(r"^what\s+(?:was\s+)?done\b", re.I)),
    ("worked", re.compile(r"^what\s+worked\b", re.I)),
    ("didnt_work", re.compile(r"^what\s+(?:didn'?t|did\s+not)\s+work\b", re.I)),
    ("left", re.compile(r"^what(?:'s|\s+is)\s+left\b", re.I)),
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")


def parse_report_sections(text: str) -> Dict[str, str]:
    """Pull the four report sections out of the model's final text. Returns
    {section: body} for the ones present, plus "other" for text outside them
    (minus the STATUS line)."""
    sections: Dict[str, List[str]] = {}
    current = "other"
    for line in (text or "").splitlines():
        # STATUS lines are control lines; PROGRESS lines are already in the
        # report's notes section (they used to show up twice).
        if _STATUS_RE.match(line) or _NOTE_RE.match(line):
            continue
        m = _HEADING_RE.match(line)
        if m:
            title = m.group(1).strip().strip("*").strip()
            key = next((k for k, rx in _SECTION_HEADINGS if rx.match(title)), None)
            if key:
                current = key
                sections.setdefault(key, [])
                continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if "\n".join(v).strip()}


_STATUS_LABELS = {
    "done": "Finished",
    "stopped": "Stopped by you",
    "timeout": "Stopped: time limit reached",
    "limit": "Stopped: round or tool-call cap reached",
    "error": "Stopped: error",
}


def render_report(data: dict) -> str:
    """Markdown for the structured report."""
    def bullet_list(items, empty):
        return "\n".join(f"- {i}" for i in items) if items else empty

    commands = data.get("commands") or []
    cmd_lines = []
    for c in commands:
        code = c.get("exit_code")
        mark = "ok" if c.get("ok") else f"failed, exit {code}" if code is not None else "failed"
        approved = " (approved by you)" if c.get("approved") else ""
        cmd_lines.append(f"{c.get('n')}. [{c.get('tool')}] `{c.get('command')}` — {mark}{approved}")

    parts = [
        "# Creator report",
        f"**Status:** {_STATUS_LABELS.get(data.get('status'), data.get('status'))}"
        + (f" — {data['error']}" if data.get("error") else ""),
        f"**Started:** {data.get('started_at')}  **Finished:** {data.get('finished_at')}",
        "## What was asked",
        data.get("asked") or "",
        "## What was done",
        data.get("done") or "_The agent did not write this section; see the commands and notes below._",
        "## What worked",
        data.get("worked") or "_Not reported by the agent._",
        "## What didn't work",
        (data.get("didnt_work") or "_Not reported by the agent._")
        + ("\n\n**Failures recorded by Creator:**\n" + bullet_list(data.get("failures"), "")
           if data.get("failures") else ""),
        "## What's left",
        data.get("left") or "_Not reported by the agent._",
        "## Exact commands run",
        "\n".join(cmd_lines) if cmd_lines else "_No commands were run._",
        "## Progress notes",
        bullet_list([f"{n.get('at', '')[:19]} {n.get('text')}" for n in (data.get("notes") or [])],
                    "_No notes._"),
    ]
    if data.get("other"):
        parts += ["## Other notes from the agent", data["other"]]
    return "\n\n".join(parts).strip() + "\n"


class CreatorManager:
    """Runs Creator jobs as asyncio background tasks and records them in the
    database. `session_factory`, `agent_loop`, `audit_directory`,
    `secret_store` and `tool_executor` are injectable for tests."""

    def __init__(
        self,
        session_factory: Callable = SessionLocal,
        agent_loop: Optional[Callable] = None,
        audit_directory=None,
        secret_store: Optional[SecretStore] = None,
        tool_executor: Optional[Callable] = None,
    ):
        global _active_manager
        self._session_factory = session_factory
        self._agent_loop = agent_loop
        self._audit_directory = audit_directory
        self._tool_executor = tool_executor
        self.secrets = secret_store or SecretStore(session_factory)
        self._tasks: Dict[str, asyncio.Task] = {}
        # Live state of active jobs, for status/stream/resume without a DB
        # round-trip.
        self._live: Dict[str, dict] = {}
        self._stopping: Set[str] = set()
        self._mark_orphans()
        _active_manager = self

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _mark_orphans(self) -> None:
        """Jobs left running or paused by a previous process can never
        finish: their in-memory run is gone."""
        try:
            db = self._session_factory()
            try:
                rows = db.query(CreatorJob).filter(CreatorJob.status.in_(ACTIVE_STATUSES)).all()
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
            try:
                state = json.loads(row.state) if getattr(row, "state", None) else {}
            except (TypeError, ValueError):
                state = {}
            job = {
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
                "state": state,
            }
        finally:
            db.close()
        live = self._live.get(job_id)
        if live is not None and job["status"] in ACTIVE_STATUSES:
            # Fresher than the periodic DB flush.
            job["events"] = list(live["events"][-_MAX_EVENTS:])
            job["state"] = self._state_snapshot(live)
            job["status"] = "paused" if live.get("pause") else "running"
            job["max_minutes"] = live["max_minutes"]
        return job

    def is_running(self, job_id: str) -> bool:
        """True while the job's task is alive — running or paused."""
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    def is_paused(self, job_id: str) -> bool:
        live = self._live.get(job_id)
        return bool(live and live.get("pause")) and self.is_running(job_id)

    def running_job_id(self) -> Optional[str]:
        for job_id, task in self._tasks.items():
            if not task.done():
                return job_id
        return None

    def live_events_after(self, job_id: str, seq: int) -> Optional[List[dict]]:
        """Events newer than `seq` for an active job, or None once the job has
        ended (read the finished job with get_job then)."""
        live = self._live.get(job_id)
        if live is None:
            return None
        return [e for e in live["events"] if e.get("seq", 0) > seq]

    def audit_log_path(self, job_id: str):
        return AuditLog(job_id, Redactor(), self._audit_directory).path

    @staticmethod
    def _public_pause(pause: Optional[dict]) -> Optional[dict]:
        if not pause:
            return None
        return {k: pause.get(k) for k in
                ("kind", "question", "options", "action", "protected", "choices", "since")}

    def _state_snapshot(self, live: dict) -> dict:
        return {
            "pause": self._public_pause(live.get("pause")),
            "notes": live["notes"][-_MAX_NOTES:],
            "commands": live["commands"][-_MAX_COMMANDS:],
            "failures": live["failure_list"],
            "rounds": live["rounds"],
            "tool_calls": live["tool_calls"],
            "segments": live["segments"],
            "gate_bypassed": live["gate_bypassed"],
            "deadline_at": live["deadline_at"],
        }

    # ------------------------------------------------------------------
    # Start / stop / resume
    # ------------------------------------------------------------------

    def start_job(
        self,
        task: str,
        endpoint_url: str,
        model: str,
        headers: Optional[dict] = None,
        owner: str = "",
        disabled_tools: Optional[Set[str]] = None,
        max_minutes: Optional[int] = None,
        protected_paths: Optional[List[str]] = None,
        approve_untrusted: bool = False,
    ) -> str:
        """`approve_untrusted` is "approve_job" given up front: the untrusted-
        content gate is lifted for the whole run, so it doesn't pause at its
        first command. Protected paths and the secret switch still apply."""
        # One job at a time; a paused job still holds its task, so it counts.
        # No await between this check and registering the task below.
        if self.running_job_id() is not None:
            raise CreatorBusyError("Another Creator job is already running or paused.")

        minutes = clamp_minutes(max_minutes) if max_minutes else default_max_minutes()
        redactor = Redactor.for_run(headers)
        # Every stored secret of this owner is scrubbed from what the run
        # stores and from what the agent reads back — switched off or not.
        try:
            redactor.add(*self.secrets.all_values(owner))
        except Exception:
            logger.warning("Creator: could not load secrets for redaction", exc_info=True)
        # Always protect the files that hold every secret (tripwire).
        protected_paths = list(protected_paths or []) + [
            p for p in secret_store_tripwire_paths() if p not in (protected_paths or [])
        ]
        job_id = new_job_id()
        started = utcnow_naive()
        db = self._session_factory()
        try:
            db.add(CreatorJob(
                id=job_id,
                owner=owner or None,
                task=redactor.text(task),
                status="running",
                started_at=started,
                model=model,
                events="[]",
            ))
            db.commit()
        finally:
            db.close()

        audit = AuditLog(job_id, redactor, self._audit_directory)
        audit.write({
            "at": _now_iso(), "type": "job_start", "job_id": job_id, "owner": owner,
            "task": task, "model": model, "max_minutes": minutes,
            "protected_paths": list(protected_paths),
            "approve_untrusted": bool(approve_untrusted),
        })
        from datetime import timedelta
        self._live[job_id] = {
            "events": [], "seq": 0, "max_minutes": minutes,
            "deadline_at": (started + timedelta(minutes=minutes)).isoformat() + "Z",
            "owner": owner or "", "redactor": redactor, "audit": audit,
            "notes": [], "commands": [], "failure_counts": {}, "failure_list": [],
            "refused": {}, "rounds": 0, "tool_calls": 0, "segments": 0,
            "tainted": False, "gate_bypassed": bool(approve_untrusted),
            "pause": None, "resume_event": None, "resume_payload": None,
            "protected_check": make_protected_action_check(protected_paths),
        }

        bg = asyncio.create_task(self._run(
            job_id, task, endpoint_url, model, headers or {}, owner,
            set(disabled_tools or ()), minutes,
        ))
        self._tasks[job_id] = bg
        bg.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))
        logger.info("Creator: started job %s (owner=%r, model=%s, limit=%dm)", job_id, owner, model, minutes)
        return job_id

    def request_secret(self, job_id: Optional[str], owner: Optional[str], name: str) -> dict:
        """get_secret's entry point. The run asking must be an active Creator
        job of the same owner; then SecretStore checks the switch. An allowed
        value is added to the run's redactor before it is returned, so it is
        scrubbed from everything stored from here on."""
        live = self._live.get(job_id or "") if is_valid_job_id(job_id or "") else None
        running = (
            live is not None
            and self.is_running(job_id)
            and live.get("owner", "") == (owner or "")
        )
        decision = self.secrets.request_secret(owner or "", name, job_id=job_id, job_running=running)
        if live is not None and running:
            if decision["allowed"]:
                live["redactor"].add(decision["value"])
            live["audit"].write({
                "at": _now_iso(), "type": "secret_request", "name": name,
                "allowed": decision["allowed"], "reason": decision.get("reason"),
            })
        return decision

    def stop_job(self, job_id: str) -> bool:
        """Stop a running or paused job."""
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        if job_id in self._stopping:
            # Already stopping; a second cancel could interrupt the final save.
            return True
        self._stopping.add(job_id)
        task.cancel()
        # The run kills its shell on the way out too; doing it here as well
        # means a command that is ignoring cancellation still dies now.
        asyncio.get_running_loop().create_task(kill_job_shell(job_id))
        return True

    def resume_job(self, job_id: str, decision: Optional[str] = None,
                   answer: Optional[str] = None) -> dict:
        """Answer a paused job and let it continue.

        Approval pauses take `decision`: "approve_once" (run this one
        action), "approve_job" (run it, and stop asking at the untrusted-
        content gate for the rest of this job — not offered for protected
        paths) or "deny". Question / blocked pauses take `answer` (free text;
        empty means "no answer, carry on as best you can")."""
        live = self._live.get(job_id)
        if not self.is_running(job_id) or live is None or not live.get("pause"):
            raise CreatorNotPausedError("This job is not paused.")
        pause = live["pause"]
        if pause["kind"] == "approval":
            if decision not in pause["choices"]:
                raise CreatorResumeError(
                    f"Choose one of: {', '.join(pause['choices'])}.")
            payload = {"decision": decision, "answer": (answer or "").strip()}
        else:
            if decision not in (None, ""):
                raise CreatorResumeError("This pause is a question; send an answer, not a decision.")
            payload = {"answer": (answer or "").strip()}
        if live.get("resume_payload") is not None:
            raise CreatorNotPausedError("This job is already resuming.")
        live["resume_payload"] = payload
        live["resume_event"].set()
        return {"resumed": True, "kind": pause["kind"]}

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
        max_minutes: int,
    ) -> None:
        live = self._live[job_id]
        redactor: Redactor = live["redactor"]
        audit: AuditLog = live["audit"]
        events: List[dict] = live["events"]
        loop = asyncio.get_running_loop()
        last_flush = [loop.time()]
        outcome = {"status": "done", "error": None, "final_text": ""}

        def add_event(event: dict) -> None:
            event = redactor.obj(event)
            event["at"] = _now_iso()
            # Monotonic sequence number, so pollers/streams can ask for
            # "everything after N" even once old events are trimmed.
            live["seq"] += 1
            event["seq"] = live["seq"]
            events.append(event)
            if len(events) > _MAX_EVENTS:
                del events[: len(events) - _MAX_EVENTS]

        def flush(**extra) -> None:
            self._update(job_id, events=json.dumps(events, default=str),
                         state=json.dumps(self._state_snapshot(live), default=str), **extra)
            last_flush[0] = loop.time()

        def add_note(text: str, source: str) -> None:
            text = redactor.text(text.strip())[:500]
            if not text:
                return
            note = {"at": _now_iso(), "text": text, "source": source}
            live["notes"].append(note)
            if len(live["notes"]) > _MAX_NOTES:
                del live["notes"][: len(live["notes"]) - _MAX_NOTES]
            add_event({"type": "note", "text": text, "source": source})
            audit.write({"at": note["at"], "type": "note", "text": text, "source": source})

        def record_command(tool: str, content: Any, result: dict, approved: bool = False) -> None:
            """Exact command log, failure tracking and taint, for every tool
            call the run makes (inside the loop or approved by the user)."""
            if not isinstance(result, dict) or result.get("approval_required"):
                return
            if tool == "ask_user":  # a question to the user, not a command
                return
            live["tool_calls"] += 1
            cmd = _norm_command(content)
            ok = not _is_failure(result) and not result.get("blocked")
            entry = {
                "n": live["tool_calls"], "at": _now_iso(), "tool": tool,
                "command": redactor.text(_truncate(_command_for_log(tool, content), 2000)),
                "exit_code": result.get("exit_code"), "ok": ok,
                "blocked": bool(result.get("blocked")),
            }
            if approved:
                entry["approved"] = True
            live["commands"].append(entry)
            if len(live["commands"]) > _MAX_COMMANDS:
                del live["commands"][: len(live["commands"]) - _MAX_COMMANDS]

            try:
                from src.tool_capabilities import tool_result_should_arm_gate
                if tool_result_should_arm_gate(tool, result, content):
                    live["tainted"] = True
            except Exception:
                live["tainted"] = True  # fail closed: treat as untrusted

            if live["tool_calls"] % AUTO_NOTE_EVERY == 0:
                failed = sum(1 for c in live["commands"] if not c["ok"])
                add_note(f"Checkpoint: {live['tool_calls']} tool calls so far, {failed} failed. "
                         f"Last: [{tool}] {_truncate(cmd, 120)}", "auto")

        def failure_hook(tool: str, content: Any, result: dict) -> dict:
            """tool_result_hook for the agent loop: tracks identical failures
            and, on the third, tells the model this command is now refused."""
            record_command(tool, content, result)
            if not _is_failure(result):
                return result
            key = f"{tool}\x00{_norm_command(content)}"
            sig = f"{key}\x00{result.get('exit_code')}\x00{_failure_text(result)}"
            count = live["failure_counts"].get(sig, 0) + 1
            live["failure_counts"][sig] = count
            if count == FAILURE_LIMIT:
                short = _truncate(_norm_command(content), 200)
                why = _truncate(_failure_text(result), 200)
                live["refused"][key] = why
                live["failure_list"].append(redactor.text(f"[{tool}] `{short}` failed {FAILURE_LIMIT} times the same way: {why}"))
                add_event({"type": "failure_limit", "tool": tool, "command": short, "error": why})
                audit.write({"at": _now_iso(), "type": "failure_limit", "tool": tool,
                             "command": _norm_command(content), "error": why})
                notice = (
                    f"\n\n[Creator] This exact command has now failed {FAILURE_LIMIT} times "
                    "the same way. It will be refused from now on. Use a different approach."
                )
                result = dict(result)
                if isinstance(result.get("error"), str):
                    result["error"] += notice
                else:
                    result["output"] = (str(result.get("output") or "") + notice).strip()
            return result

        def refusal_check(tool: str, content: Any) -> Optional[str]:
            why = live["refused"].get(f"{tool}\x00{_norm_command(content)}")
            if why is None:
                return None
            return (
                f"Refused by Creator mode: this exact command already failed {FAILURE_LIMIT} "
                f"times the same way ({why}). Try a different approach — a different "
                "command, tool, or fix for the underlying problem."
            )

        agent_loop = self._agent_loop
        if agent_loop is None:
            from src.agent_loop import stream_agent_loop as agent_loop

        async def run_segment(messages: List[dict]) -> dict:
            """One agent-loop call. Returns how it ended:
            {"end": "done"|"blocked"|"question"|"approval"|"rounds"|"budget",
             "text": final text, "pause": {...} for pauses}."""
            live["segments"] += 1
            text_parts: List[str] = []
            # Kept on `live` so a stop/timeout mid-segment can still report
            # what the agent had written.
            live["segment_text"] = text_parts
            harvested = [0]
            ending: Dict[str, Any] = {"end": "done"}

            def harvest_notes(final: bool = False) -> None:
                text = "".join(text_parts)
                upto = len(text) if final else text.rfind("\n") + 1
                if upto <= harvested[0]:
                    return
                for m in _NOTE_RE.finditer(text[harvested[0]:upto]):
                    add_note(m.group(1), "agent")
                harvested[0] = upto

            remaining_rounds = max(1, CREATOR_MAX_ROUNDS - live["rounds"])
            remaining_calls = max(1, CREATOR_MAX_TOOL_CALLS - live["tool_calls"])
            async for chunk in agent_loop(
                endpoint_url=endpoint_url,
                model=model,
                messages=messages,
                headers=headers,
                # Like interactive chat: no explicit temperature, so the
                # provider default applies. The loop's 0.3 default makes
                # newer Claude models (e.g. claude-sonnet-5-5) return HTTP 400
                # "temperature is deprecated" — found in the first real run.
                temperature=None,
                max_rounds=min(SEGMENT_ROUNDS, remaining_rounds),
                max_tool_calls=remaining_calls,
                session_id=job_id,
                owner=owner or None,
                disabled_tools=disabled_tools,
                workload="background",
                protected_action_check=live["protected_check"],
                # Always offer get_secret; tool retrieval wouldn't pick it.
                forced_tools={"get_secret"},
                # Known secret values are blanked from tool results before the
                # agent reads them (get_secret's own result excepted).
                output_redactor=redactor.known_obj,
                tool_refusal_check=refusal_check,
                tool_result_hook=failure_hook,
                external_untrusted_context_seen=live["tainted"],
                untrusted_gate_bypassed=live["gate_bypassed"],
            ):
                if isinstance(chunk, str) and chunk.startswith("event: error"):
                    # A model request that fails before producing anything
                    # comes through as a raw SSE error event (no
                    # agent_terminal), and the loop stops after it.
                    raise CreatorModelError(f"The model request failed: {_sse_error_text(chunk)}")
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
                        text_parts.append(data["delta"])
                    continue

                etype = data.get("type")
                if etype == "agent_terminal":
                    # The model request itself failed (provider error, bad
                    # key, unreachable endpoint...). The loop stops here; it is
                    # not a finished task.
                    meta = data.get("data") if isinstance(data.get("data"), dict) else {}
                    if meta.get("failed"):
                        failure = meta.get("failure")
                        msg = (failure.get("message") or failure.get("status")
                               if isinstance(failure, dict) else failure)
                        raise CreatorModelError(
                            f"The model request failed: {msg or 'no details from the provider'}")
                if etype is None and data.get("error"):
                    # Raw provider error chunk; keep it in the log for diagnosis.
                    add_event({"type": "model_error", "error": _truncate(str(data.get("error")), 1000)})
                    continue
                if etype == "agent_step":
                    live["rounds"] += 1
                elif etype == "rounds_exhausted":
                    ending = {"end": "rounds"}
                elif etype == "budget_exceeded":
                    ending = {"end": "budget"}

                event = _event_from_sse(data)
                if event is not None:
                    add_event(event)
                    if event["type"] in ("tool_start", "tool_output"):
                        entry = {"at": _now_iso(), "type": event["type"], "tool": data.get("tool"),
                                 "command": data.get("full_command") or data.get("command")}
                        if event["type"] == "tool_output":
                            entry["exit_code"] = data.get("exit_code")
                            entry["output"] = _truncate(_tool_output_text(data), _AUDIT_OUTPUT_CHARS)
                        audit.write(entry)
                    if event["type"] == "tool_start":
                        harvest_notes()

                ask = data.get("ask_user")
                if etype == "tool_output" and isinstance(ask, dict):
                    if ask.get("kind") == "tool_approval":
                        # The loop holds the action back and ends its turn.
                        # Creator handles the approval itself, so retire the
                        # chat-style approval record.
                        self._retire_approval(ask.get("approval_id"), owner, job_id)
                        action = ask.get("action") or {}
                        tool = action.get("tool") or data.get("tool") or ""
                        content = action.get("content") or ""
                        protected = bool(live["protected_check"] and live["protected_check"](tool, content))
                        ending = {"end": "approval", "pause": {
                            "kind": "approval",
                            "question": ask.get("description") or "This action needs your OK.",
                            "action": {"tool": tool, "command": content},
                            "protected": protected,
                            # A protected path is approved one action at a time.
                            "choices": ["approve_once", "deny"] if protected else list(APPROVAL_DECISIONS),
                        }}
                    else:
                        options = [
                            (o.get("label") if isinstance(o, dict) else str(o))
                            for o in (ask.get("options") or [])
                        ]
                        ending = {"end": "question", "pause": {
                            "kind": "question",
                            "question": ask.get("question") or "The agent has a question.",
                            "options": [o for o in options if o],
                        }}

                if loop.time() - last_flush[0] >= _FLUSH_INTERVAL_S:
                    flush()

            harvest_notes(final=True)
            text = "".join(text_parts).strip()
            ending["text"] = text
            if ending["end"] == "done":
                status_lines = list(_STATUS_RE.finditer(text))
                if status_lines and status_lines[-1].group(1).upper() == "BLOCKED":
                    need = status_lines[-1].group(2) or "The agent says it is blocked."
                    ending = {"end": "blocked", "text": text, "pause": {
                        "kind": "blocked", "question": need, "options": [],
                    }}
            return ending

        async def pause(request: dict) -> dict:
            request = redactor.obj(dict(request))
            request["since"] = _now_iso()
            live["pause"] = request
            live["resume_payload"] = None
            live["resume_event"] = asyncio.Event()
            add_event({"type": "paused", "kind": request["kind"], "question": request.get("question"),
                       "action": request.get("action")})
            audit.write({"at": _now_iso(), "type": "paused", **request})
            flush(status="paused")
            try:
                await live["resume_event"].wait()
            finally:
                live["resume_event"] = None
            payload = live["resume_payload"] or {}
            live["pause"] = None
            live["resume_payload"] = None
            add_event({"type": "resumed", "decision": payload.get("decision"),
                       "answer": _truncate(payload.get("answer") or "", 500)})
            audit.write({"at": _now_iso(), "type": "resumed", **payload})
            flush(status="running")
            return payload

        async def run_approved(tool: str, content: str) -> dict:
            """Run an action the user approved. Its exact (tool, content) is
            let through the protected-path check once; the untrusted-content
            gate is lifted for this call only."""
            from src.tool_capabilities import ToolRunSecurityContext
            base = live["protected_check"]

            def allow_this(t, c):
                if t == tool and c == content:
                    return None
                return base(t, c) if base else None

            executor = self._tool_executor
            if executor is None:
                from src.agent_tools import ToolBlock, execute_tool_block as executor
            else:
                from collections import namedtuple
                ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])
            add_event({"type": "tool_start", "tool": tool, "command": _truncate(content), "approved": True})
            audit.write({"at": _now_iso(), "type": "tool_start", "tool": tool, "command": content,
                         "approved": True})
            try:
                _desc, result = await executor(
                    ToolBlock(tool, content),
                    session_id=job_id,
                    disabled_tools=disabled_tools,
                    owner=owner or None,
                    security_context=ToolRunSecurityContext(
                        approval_gate_bypassed=True,
                        protected_action_check=allow_this,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                result = {"error": f"Approved action failed to run: {e}", "exit_code": 1}
            result = redactor.known_obj(result if isinstance(result, dict) else {"output": str(result)})
            record_command(tool, content, result, approved=True)
            out = _tool_output_text(result)
            add_event({"type": "tool_output", "tool": tool, "command": _truncate(content),
                       "output": _truncate(out), "exit_code": result.get("exit_code"), "approved": True})
            audit.write({"at": _now_iso(), "type": "tool_output", "tool": tool, "command": content,
                         "exit_code": result.get("exit_code"), "approved": True,
                         "output": _truncate(out, _AUDIT_OUTPUT_CHARS)})
            return result

        def continuation(reason: str, previous_text: str) -> List[dict]:
            """A fresh context for the next segment: the task, the last thing
            the agent said, and its notes, command log and failures."""
            notes = live["notes"][-25:]
            cmds = live["commands"][-25:]
            lines = [f"[Creator mode — continuing the same task] {reason}", ""]
            lines.append("Your progress notes so far:")
            lines += [f"- {n['text']}" for n in notes] or ["- (none yet)"]
            lines += ["", f"Tool calls so far ({live['tool_calls']} total, most recent last):"]
            lines += [
                f"- [{c['tool']}] {c['command'][:300]} -> "
                + ("ok" if c["ok"] else f"failed (exit {c['exit_code']})")
                for c in cmds
            ] or ["- (none yet)"]
            if live["failure_list"]:
                lines += ["", "Refused — failed 3 times the same way, do not repeat as-is:"]
                lines += [f"- {f}" for f in live["failure_list"]]
            lines += ["", "Continue from where you are. Keep writing PROGRESS: lines, ask the "
                          "user with ask_user only when blocked, and finish with the four-heading "
                          "report and a STATUS line."]
            msgs = [
                {"role": "system", "content": CREATOR_SYSTEM_PROMPT},
                {"role": "user", "content": task},
            ]
            if previous_text:
                msgs.append({"role": "assistant", "content": redactor.known_text(_tail(previous_text, 4000))})
            msgs.append({"role": "user", "content": redactor.known_text("\n".join(lines))})
            return msgs

        async def drive() -> None:
            messages = [
                {"role": "system", "content": CREATOR_SYSTEM_PROMPT},
                {"role": "user", "content": task},
            ]
            while True:
                seg = await run_segment(messages)
                outcome["final_text"] = seg.get("text") or outcome["final_text"]
                end = seg["end"]
                if end == "done":
                    return
                if end == "budget" or live["rounds"] >= CREATOR_MAX_ROUNDS \
                        or live["tool_calls"] >= CREATOR_MAX_TOOL_CALLS:
                    outcome["status"] = "limit"
                    outcome["error"] = (f"Cap reached: {live['rounds']} rounds, "
                                        f"{live['tool_calls']} tool calls.")
                    return
                if end == "rounds":
                    add_note(f"Checkpoint after segment {live['segments']}: context rebuilt from notes.", "auto")
                    messages = continuation("Checkpoint: your context was rebuilt from your notes.",
                                            seg.get("text") or "")
                    continue

                # A pause: wait for the user.
                request = seg["pause"]
                payload = await pause(request)
                answer = payload.get("answer") or ""
                if request["kind"] == "approval":
                    action = request["action"]
                    decision = payload["decision"]
                    if decision == "deny":
                        reason = (f"You asked to run [{action['tool']}] `{_truncate(action['command'], 300)}`. "
                                  "The user DENIED it; it was not run. Find another way, or ask the "
                                  "user if there is none.")
                    else:
                        result = await run_approved(action["tool"], action["command"])
                        if decision == "approve_job":
                            live["gate_bypassed"] = True
                        scope = ("for the rest of this job (untrusted-content approvals only)"
                                 if decision == "approve_job" else "for this one action")
                        reason = (f"The user APPROVED [{action['tool']}] `{_truncate(action['command'], 300)}` "
                                  f"{scope}. It has been run. Result (exit {result.get('exit_code')}):\n"
                                  f"{_tail(_tool_output_text(result), 3000)}")
                    if answer:
                        reason += f"\nThe user also said: {answer}"
                else:
                    reason = (f"You were blocked and asked: {request.get('question')}\n"
                              + (f"The user answered: {answer}" if answer else
                                 "The user resumed without an answer; carry on as best you can."))
                messages = continuation(reason, seg.get("text") or "")

        try:
            await asyncio.wait_for(drive(), timeout=max_minutes * _SECONDS_PER_MINUTE)
        except asyncio.TimeoutError:
            # Paused time counts: the limit is wall-clock for the whole run.
            outcome["status"] = "timeout"
            outcome["error"] = f"Time limit of {max_minutes} minute(s) reached; the job was stopped."
            add_event({"type": "timeout", "max_minutes": max_minutes})
        except asyncio.CancelledError:
            outcome["status"] = "stopped"
            outcome["error"] = "Stopped by user."
            add_event({"type": "stopped"})
        except Exception as e:
            logger.error("Creator job %s failed: %s", job_id, e, exc_info=True)
            outcome["status"] = "error"
            outcome["error"] = str(e)[:2000]
        finally:
            # Whatever happened, make sure nothing this job started in its
            # shell keeps running.
            try:
                await kill_job_shell(job_id)
            except asyncio.CancelledError:
                pass

        if outcome["status"] != "done":
            partial = "".join(live.get("segment_text") or []).strip()
            if partial:
                outcome["final_text"] = partial
        live["pause"] = None
        finished = utcnow_naive()
        error = redactor.text(outcome["error"]) if outcome["error"] else None
        sections = parse_report_sections(outcome["final_text"])
        report_data = redactor.obj({
            "asked": task,
            "status": outcome["status"],
            "error": outcome["error"],
            "started_at": None,  # filled below from the DB row
            "finished_at": finished.isoformat() + "Z",
            "done": sections.get("done", ""),
            "worked": sections.get("worked", ""),
            "didnt_work": sections.get("didnt_work", ""),
            "left": sections.get("left") or (
                f"The run did not finish ({outcome['status']}): {outcome['error']}"
                if outcome["status"] != "done" else ""),
            # Text outside the four headings (all of it, if the agent used none).
            "other": sections.get("other", ""),
            "failures": live["failure_list"],
            "commands": live["commands"],
            "notes": live["notes"],
        })
        job_row = self.get_job(job_id) or {}
        report_data["started_at"] = job_row.get("started_at")
        report = render_report(report_data)
        audit.write({"at": _now_iso(), "type": "job_end", "status": outcome["status"],
                     "error": outcome["error"], "report": report})
        state = self._state_snapshot(live)
        state["report"] = report_data
        try:
            self._update(
                job_id,
                status=outcome["status"],
                error=error,
                report=report,
                events=json.dumps(events, default=str),
                state=json.dumps(state, default=str),
                finished_at=finished,
            )
        except Exception:
            logger.error("Creator job %s: could not save final state", job_id, exc_info=True)
        finally:
            self._live.pop(job_id, None)
            self._stopping.discard(job_id)
        logger.info("Creator: job %s finished with status %s", job_id, outcome["status"])

    @staticmethod
    def _retire_approval(approval_id, owner: str, job_id: str) -> None:
        if not approval_id:
            return
        try:
            from src.tool_approvals import tool_approval_store
            tool_approval_store.consume(approval_id, decision="deny", owner=owner, session_id=job_id)
        except Exception:
            logger.debug("Could not retire Creator approval", exc_info=True)
