r"""Creator window (Phase 7, docs/creator-plan.md).

`static/js/creator/view.js` turns a job's event log into the window's
timeline; it has no DOM, so node runs it directly. The wiring checks are
static: the sidebar/rail buttons exist, open the lazily-loaded panel, are
hidden without `can_use_creator`, and the panel puts untrusted text in with
textContent.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_STATIC = _REPO / "static"
_VIEW = (_STATIC / "js" / "creator" / "view.js").as_posix()
_PANEL = _STATIC / "js" / "creator" / "panel.js"
_HAS_NODE = shutil.which("node") is not None

needs_node = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def _run(js: str):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def _timeline(events):
    return _run(textwrap.dedent(f"""
        const v = await import('{_VIEW}');
        console.log(JSON.stringify(v.buildTimeline({json.dumps(events)})));
    """))


@needs_node
def test_tool_output_joins_its_start_and_rounds_are_dropped():
    items = _timeline([
        {"seq": 1, "type": "round", "round": 1},
        {"seq": 2, "type": "tool_start", "tool": "bash", "command": "ls /"},
        {"seq": 3, "type": "tool_start", "tool": "read_file", "command": "/etc/hosts"},
        {"seq": 4, "type": "tool_output", "tool": "bash", "command": "ls /", "output": "bin\netc", "exit_code": 0},
        {"seq": 5, "type": "tool_output", "tool": "read_file", "command": "/etc/hosts", "output": "", "exit_code": 2},
    ])
    assert [i["kind"] for i in items] == ["command", "command"]
    bash, read = items
    assert bash == {"kind": "command", "tool": "bash", "command": "ls /", "output": "bin\netc",
                    "exitCode": 0, "approved": False, "done": True}
    assert read["exitCode"] == 2 and read["done"] is True


@needs_node
def test_unfinished_and_orphan_commands():
    items = _timeline([
        {"type": "tool_start", "tool": "bash", "command": "sleep 100"},
        {"type": "tool_output", "tool": "bash", "command": "echo hi", "output": "hi", "exit_code": 0,
         "approved": True},
    ])
    assert len(items) == 2
    assert items[0]["done"] is False and items[0]["exitCode"] is None
    # An output with no matching start (an approved action's start was
    # trimmed, say) still shows up as its own command.
    assert items[1]["command"] == "echo hi" and items[1]["approved"] is True


@needs_node
def test_notes_pauses_resumes_and_endings():
    items = _timeline([
        {"type": "note", "text": "checked nginx config", "source": "model"},
        {"type": "note", "text": "Checkpoint after segment 1", "source": "auto"},
        {"type": "paused", "kind": "approval", "question": "Run this?",
         "action": {"tool": "bash", "command": "rm -rf build"}},
        {"type": "resumed", "decision": "approve_job", "answer": ""},
        {"type": "paused", "kind": "question", "question": "Which port?"},
        {"type": "resumed", "decision": None, "answer": "8080"},
        {"type": "failure_limit", "tool": "bash", "command": "make", "error": "x"},
        {"type": "model_error", "error": "overloaded"},
        {"type": "timeout", "max_minutes": 60},
        {"type": "stopped"},
        {"type": "something_new"},
    ])
    kinds = [i["kind"] for i in items]
    assert kinds == ["note", "note", "pause", "resumed", "pause", "resumed",
                     "system", "system", "system", "system"]
    assert items[0]["auto"] is False and items[1]["auto"] is True
    assert items[2]["label"] == "Needs your approval" and items[2]["action"]["command"] == "rm -rf build"
    assert items[3]["text"] == "Approved for the rest of this job"
    assert items[5] == {"kind": "resumed", "text": "You answered", "answer": "8080"}
    assert items[6]["level"] == "warn" and "make" in items[6]["text"]
    assert items[7]["level"] == "error"
    assert "60 min" in items[8]["text"]


@needs_node
def test_helpers():
    out = _run(textwrap.dedent(f"""
        const v = await import('{_VIEW}');
        const now = Date.parse('2026-10-01T12:00:00Z');
        console.log(JSON.stringify({{
          action: v.describeAction({{tool: 'bash', command: 'ls'}}),
          noAction: v.describeAction(null),
          labels: [v.statusLabel('paused'), v.statusLabel('weird')],
          active: [v.isActive('running'), v.isActive('paused'), v.isActive('done')],
          rel: [v.relativeTime('2026-10-01T11:59:30Z', now), v.relativeTime('2026-10-01T11:00:00Z', now),
                v.relativeTime('2026-09-01T00:00:00Z', now), v.relativeTime(null, now)],
          dur: [v.formatDuration('2026-10-01T11:00:00Z', '2026-10-01T12:05:00Z'),
                v.formatDuration('2026-10-01T11:59:48Z', null, now), v.formatDuration(null, null)],
        }}));
    """))
    assert out["action"] == "bash: ls" and out["noAction"] == ""
    assert out["labels"] == ["Waiting for you", "weird"]
    assert out["active"] == [True, True, False]
    assert out["rel"] == ["just now", "1 h ago", "2026-09-01", ""]
    assert out["dur"] == ["1 h 05 min", "12 s", ""]


@needs_node
def test_panel_module_parses():
    proc = subprocess.run(["node", "--check", str(_PANEL)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


def test_window_is_wired_into_sidebar_rail_and_privileges():
    index = (_STATIC / "index.html").read_text()
    app = (_STATIC / "app.js").read_text()
    init = (_STATIC / "js" / "init.js").read_text()
    modal = (_STATIC / "js" / "modalManager.js").read_text()
    assert 'id="tool-creator-btn"' in index and 'id="rail-creator"' in index
    assert "import('./js/creator/panel.js')" in app
    assert "'rail-creator':   'tool-creator-btn'" in app
    assert "'creator-overlay':      { rail: 'rail-creator',   sidebar: 'tool-creator-btn' }" in modal
    assert "hideOn('#tool-creator-btn, #rail-creator, [data-settings-tab=\"secrets\"]', privs.can_use_creator)" in init


def test_panel_only_uses_innerhtml_for_static_markup_and_the_report():
    src = _PANEL.read_text()
    lines = [ln.strip() for ln in src.splitlines() if ".innerHTML" in ln and not ln.strip().startswith("//")]
    assert len(lines) == 3, lines
    assert any("ICON" in ln for ln in lines)
    assert any("<svg" in ln for ln in lines)
    assert any("markdownModule.mdToHtml(report.report)" in ln for ln in lines)
