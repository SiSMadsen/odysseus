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
                    "exitCode": 0, "approved": False, "done": True, "host": False}
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
def test_live_helpers():
    out = _run(textwrap.dedent(f"""
        const v = await import('{_VIEW}');
        const now = Date.parse('2026-10-01T12:00:00Z');
        console.log(JSON.stringify({{
          seq: [v.lastSeq([{{seq: 4}}, {{seq: 9}}, {{seq: 7}}]), v.lastSeq([{{}}, {{}}]), v.lastSeq([])],
          status: [v.statusAfterEvent('running', {{type: 'paused'}}),
                   v.statusAfterEvent('paused', {{type: 'resumed'}}),
                   v.statusAfterEvent('running', {{type: 'note'}}),
                   v.statusAfterEvent('done', {{type: 'paused'}})],
          left: [v.timeLeft('2026-10-01T12:42:00Z', now), v.timeLeft('2026-10-01T12:00:35Z', now),
                 v.timeLeft('2026-10-01T13:05:00Z', now), v.timeLeft('2026-10-01T11:59:00Z', now),
                 v.timeLeft(null, now)],
          delay: [0, 1, 2, 10].map(v.reconnectDelay),
        }}));
    """))
    # Events without a seq count by position, as the server's ?since= does.
    assert out["seq"] == [9, 2, 0]
    assert out["status"] == ["paused", "running", "running", "done"]
    assert out["left"] == ["42 min left", "35 s left", "1 h 05 min left", "time's up", ""]
    assert out["delay"] == [1000, 2000, 4000, 30000]


@needs_node
def test_reply_controls_follow_the_pause():
    out = _run(textwrap.dedent(f"""
        const v = await import('{_VIEW}');
        console.log(JSON.stringify({{
          approval: v.replyControls({{kind: 'approval', choices: ['approve_once', 'approve_job', 'deny'], protected: false}}),
          protectedPath: v.replyControls({{kind: 'approval', choices: ['approve_once', 'deny'], protected: true}}),
          noChoices: v.replyControls({{kind: 'approval'}}),
          question: v.replyControls({{kind: 'question', options: ['80', '', ' 8080 ', null]}}),
          blocked: v.replyControls({{kind: 'blocked', options: []}}),
          none: [v.replyControls(null), v.replyControls(undefined)],
          send: [v.sendLabel(''), v.sendLabel('   '), v.sendLabel('yes')],
        }}));
    """))
    a = out["approval"]
    assert a["mode"] == "approval" and a["notice"] == ""
    assert [(b["label"], b["decision"], b["tone"]) for b in a["buttons"]] == [
        ("Approve once", "approve_once", "approve"),
        ("Approve for this job", "approve_job", "approve"),
        ("Deny", "deny", "deny"),
    ]
    # Protected paths: the server offers no approve_job, and the bar says why.
    p = out["protectedPath"]
    assert [b["decision"] for b in p["buttons"]] == ["approve_once", "deny"]
    assert "protected path" in p["notice"]
    # Missing choices fall back to the narrowest set, never approve_job.
    assert [b["decision"] for b in out["noChoices"]["buttons"]] == ["approve_once", "deny"]
    q = out["question"]
    assert q["mode"] == "answer" and [b["answer"] for b in q["buttons"]] == ["80", "8080"]
    assert out["blocked"]["buttons"] == [] and "needs" in out["blocked"]["placeholder"]
    assert out["none"] == [None, None]
    assert out["send"] == ["Carry on without an answer", "Carry on without an answer", "Send"]


@needs_node
def test_host_commands_read_plainly_and_say_where_they_run():
    out = _run(textwrap.dedent(f"""
        const v = await import('{_VIEW}');
        const cmd = JSON.stringify({{command: 'systemctl reload apache2', timeout_s: 30}});
        const items = v.buildTimeline([
          {{type: 'tool_start', tool: 'host_exec', command: cmd}},
          {{type: 'tool_output', tool: 'host_exec', command: cmd, output: '', exit_code: 0}},
          {{type: 'tool_start', tool: 'bash', command: '{{"command": "x"}}'}},
        ]);
        const host = v.replyControls({{kind: 'approval', scope: 'host', protected: false,
                                      choices: ['approve_once', 'approve_job', 'deny']}});
        const untrusted = v.replyControls({{kind: 'approval', scope: 'untrusted',
                                           choices: ['approve_once', 'approve_job', 'deny']}});
        console.log(JSON.stringify({{
          items: items.map(i => [i.host, v.displayCommand(i.tool, i.command)]),
          action: v.describeAction({{tool: 'host_exec', command: cmd}}),
          bare: v.displayCommand('host_exec', 'uptime'),
          host: [host.buttons.map(b => b.label), host.notice],
          untrusted: untrusted.buttons.map(b => b.label),
        }}));
    """))
    assert out["items"] == [[True, "systemctl reload apache2"], [False, '{"command": "x"}']]
    assert out["action"] == "host_exec: systemctl reload apache2"
    assert out["bare"] == "uptime"
    labels, notice = out["host"]
    assert labels == ["Allow once", "Allow all host commands for this job", "Deny"]
    assert "host machine" in notice
    assert out["untrusted"] == ["Approve once", "Approve for this job", "Deny"]


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
