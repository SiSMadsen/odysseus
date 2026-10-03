r"""The Creator window itself (static/js/creator/panel.js), driven in a fake
browser page under node (tests/helpers/creator_window_dom.mjs): no jsdom,
the same approach as the Settings shell tests.

Covers what the throwaway jsdom harness covered during Phase 7 and later:
starting a job, opening on the running job, the live view (new events,
duplicates, the final reload with the report, reconnecting from the last
event), the reply bar for each kind of pause (approvals, protected paths,
root commands, questions, blocked), 409/400 from resume, the attention dot and
notification while minimized, Stop, the root switch strip, Follow up, the
unsent task surviving a close, the report download, and task text going in
as text, never HTML. The window's pure helpers are tested in
test_creator_window_js.py.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HARNESS = (_REPO / "tests" / "helpers" / "creator_window_dom.mjs").as_posix()

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")

# Shared by every scenario: a job record like /api/creator/status returns, and
# done(), which prints the result and ends the process (the window's timers
# would otherwise keep it alive).
_COMMON = r"""
const JOB = 'cr-000000000001';
function job(over = {}) {
  return {
    job_id: JOB, task: 'Fix the status page', status: 'running', events: [], pause: null,
    deadline_at: new Date(Date.now() + 3600e3).toISOString(), has_report: false, model: 'm',
    started_at: new Date().toISOString(), finished_at: null, max_minutes: 60, follow_up_of: null,
    ...over,
  };
}
function listed(j) { return { job_id: j.job_id, task: j.task, status: j.status, started_at: j.started_at }; }
function done(out) { console.log(JSON.stringify(out)); process.exit(0); }
const sleep = ms => new Promise(r => setTimeout(r, ms));
"""


def _run(body: str) -> dict:
    js = f"const h = await import('{_HARNESS}');\n{_COMMON}\n{textwrap.dedent(body)}"
    proc = subprocess.run(["node", "--input-type=module"], input=js, capture_output=True, text=True,
                          cwd=str(_REPO), timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _with_running_job(extra_routes: str = "", job_over: str = "{}", admin: str = "false") -> str:
    """JS that opens the window on a running job (with its stream open)."""
    return f"""
        const j = job({job_over});
        const env = h.setup({{ admin: {admin}, routes: {{
          'GET /api/model-endpoints': [],
          'GET /api/creator/jobs?limit=100': {{ jobs: [listed(j)] }},
          [`GET /api/creator/status/${{JOB}}?since=0`]: () => j,
          {extra_routes}
        }} }});
        const panel = await env.loadPanel();
        panel.openPanel();
        await env.settle();
        const stream = () => env.streams[env.streams.length - 1];
    """


# ---------------------------------------------------------------------------
# Starting a job
# ---------------------------------------------------------------------------

def test_start_sends_the_options_then_opens_the_job():
    out = _run("""
        const started = job({ task: 'Install nginx' });
        const env = h.setup({ routes: {
          'GET /api/model-endpoints': [{ id: 'ep1', name: 'Claude', is_enabled: true, model_type: 'llm',
                                         models: ['b-model', 'a-model'] }],
          'GET /api/creator/jobs?limit=100': { jobs: [] },
          'POST /api/creator/start': { job_id: JOB },
          [`GET /api/creator/status/${JOB}?since=0`]: started,
        } });
        const panel = await env.loadPanel();
        panel.openPanel();
        await env.settle();
        const empty = (env.clickButton('Start'), await env.settle(), env.text('#creator-composer-msg'));
        env.type('creator-task', '  Install nginx  ');
        env.byId('creator-max-minutes').value = '30';
        env.byId('creator-approve-untrusted').checked = true;
        const ep = env.byId('creator-endpoint');
        ep.value = 'ep1';
        ep.dispatchEvent(h.makeEvent('change'));
        const models = env.byId('creator-model').options.map(o => o.value);
        env.byId('creator-model').value = 'a-model';
        env.byId('creator-model').dispatchEvent(h.makeEvent('change'));
        env.clickButton('Start');
        await env.settle(10);
        done({
          empty, models,
          start: env.lastCall('POST', '/api/creator/start').body,
          composerHidden: env.byId('creator-composer').hidden,
          untrustedReset: env.byId('creator-approve-untrusted').checked,
          title: env.text('.creator-job-title'),
          stream: env.streams.map(s => s.url),
          saved: JSON.parse(env.storage.get('odysseus-creator-options')),
        });
    """)
    assert out["empty"] == "Write a task first."
    assert out["models"] == ["", "a-model", "b-model"]
    assert out["start"] == {"task": "Install nginx", "approve_untrusted": True, "approve_host": False,
                            "max_minutes": 30, "endpoint_id": "ep1", "model": "a-model"}
    assert out["composerHidden"] is True and out["untrustedReset"] is False
    assert out["title"] == "Install nginx"
    assert out["stream"] == ["/api/creator/stream/cr-000000000001?since=0"]
    assert out["saved"] == {"endpoint_id": "ep1", "model": "a-model", "max_minutes": 30}


def test_start_explains_a_busy_server_and_a_bad_time_limit():
    out = _run("""
        const env = h.setup({ routes: {
          'GET /api/model-endpoints': [],
          'GET /api/creator/jobs?limit=100': { jobs: [] },
          'POST /api/creator/start': { status: 409, body: { detail: 'busy' } },
        } });
        const panel = await env.loadPanel();
        panel.openPanel();
        await env.settle();
        env.type('creator-task', 'x');
        env.byId('creator-max-minutes').value = '0';
        env.clickButton('Start');
        await env.settle();
        const bad = env.text('#creator-composer-msg');
        env.byId('creator-max-minutes').value = '';
        env.key('creator-task', 'Enter', { ctrlKey: true });   // Ctrl+Enter starts too
        await env.settle();
        done({ bad, busy: env.text('#creator-composer-msg'),
               starts: env.calls.filter(c => c.url === '/api/creator/start').length });
    """)
    assert out["bad"] == "Time limit must be 1–1440 minutes."
    assert out["busy"] == "A Creator job is already running. Stop it or wait for it to finish."
    assert out["starts"] == 1


def test_an_unsent_task_survives_closing_the_window():
    out = _run("""
        const env = h.setup({ routes: { 'GET /api/model-endpoints': [], 'GET /api/creator/jobs?limit=100': { jobs: [] } } });
        const panel = await env.loadPanel();
        panel.openPanel();
        await env.settle();
        env.type('creator-task', 'half-written task');
        env.key(null, 'Escape');                 // Esc closes it
        const closed = !env.byId('creator-overlay');
        panel.openPanel();
        await env.settle();
        done({ closed, task: env.byId('creator-task').value });
    """)
    assert out == {"closed": True, "task": "half-written task"}


# ---------------------------------------------------------------------------
# The live view
# ---------------------------------------------------------------------------

def test_opening_the_window_goes_to_the_running_job():
    out = _run(_with_running_job() + """
        done({ title: env.text('.creator-job-title'), selected: env.$('.creator-job-item.selected') !== null,
               stream: stream().url, stop: env.buttons('#creator-job-head') });
    """)
    assert out["title"] == "Fix the status page" and out["selected"] is True
    assert out["stream"].endswith("?since=0")
    assert "Stop" in out["stop"]


def test_live_events_arrive_once_and_the_end_brings_the_report():
    out = _run(_with_running_job(
        extra_routes="""'GET /api/creator/report/cr-000000000001': { report: '# Creator report', audit_log: '/app/data/creator/audit/x.jsonl' },""",
        job_over="{ events: [{ seq: 1, type: 'note', text: 'first', source: 'agent' }] }",
    ) + """
        const firstUrl = stream().url;
        stream().opened();
        stream().emit({ seq: 2, type: 'note', text: 'second', source: 'agent' });
        stream().emit({ seq: 2, type: 'note', text: 'second', source: 'agent' });   // a duplicate
        stream().emit({ seq: 1, type: 'note', text: 'first', source: 'agent' });    // already had it
        stream().emit({ seq: 3, type: 'tool_start', tool: 'run_as_root', command: '{"command": "apt-get update"}' });
        await env.settle();
        const live = env.$$('.creator-note-text').map(n => n.textContent);
        const cmd = [env.text('.creator-cmd-text'), env.$$('.creator-chip').map(c => c.textContent)];
        j.status = 'done'; j.has_report = true; j.finished_at = new Date().toISOString();
        stream().emit({ final: true, status: 'done' });
        await env.settle(10);
        done({
          firstUrl, live, cmd,
          closed: env.streams[0].closed,
          report: env.text('.creator-report-body'),
          reportHtml: env.$('.creator-report-body').innerHTML,
          foot: env.text('.creator-report-foot'),
          head: env.buttons('#creator-job-head'),
          pill: env.text('.creator-status-pill'),
        });
    """)
    assert out["firstUrl"].endswith("?since=1")
    assert out["live"] == ["first", "second"]
    assert out["cmd"] == ["apt-get update", ["root", "…"]]
    assert out["closed"] is True
    assert out["reportHtml"] == "<md># Creator report</md>"   # through the chat's renderer
    assert out["foot"] == "Full audit log on the server: /app/data/creator/audit/x.jsonl"
    assert "Follow up" in out["head"] and "Stop" not in out["head"]
    assert out["pill"] == "Done"


def test_a_dropped_stream_reconnects_from_the_last_event():
    out = _run(_with_running_job() + """
        stream().emit({ seq: 7, type: 'note', text: 'x', source: 'agent' });
        await env.settle();
        stream().fail();
        const state = env.text('#creator-live-state');
        await sleep(1100);   // the first retry waits 1 s
        await env.settle();
        done({ state, urls: env.streams.map(s => s.url), firstClosed: env.streams[0].closed });
    """)
    assert out["state"] == "Live view lost, reconnecting in 1 s…"
    assert out["urls"][-1].endswith("?since=7") and len(out["urls"]) == 2
    assert out["firstClosed"] is True


# ---------------------------------------------------------------------------
# The reply bar
# ---------------------------------------------------------------------------

def _paused(pause_js: str, resume_reply: str = "{ resumed: true }") -> str:
    """Opens on a running job, then a pause arrives over the stream."""
    return _with_running_job(extra_routes=f"""
          [`GET /api/creator/status/${{JOB}}?since=1`]: {{ ...job(), status: 'paused', pause: {pause_js} }},
          [`POST /api/creator/resume/${{JOB}}`]: {resume_reply},
    """) + f"""
        const p = {pause_js};
        stream().emit({{ seq: 1, type: 'paused', kind: p.kind, question: p.question, action: p.action }});
        await env.settle(10);
        const bar = () => ({{
          shown: h.visible(env.byId('creator-reply')),
          buttons: env.buttons('#creator-reply'),
          notice: env.byId('creator-reply-notice').hidden ? '' : env.text('#creator-reply-notice'),
          composer: h.visible(env.byId('creator-composer')),
        }});
    """


def test_an_approval_offers_the_servers_choices_and_sends_the_note():
    out = _run(_paused("""{ kind: 'approval', question: 'OK to run?', scope: 'untrusted', protected: false,
        since: 't1', action: { tool: 'bash', command: 'ls' }, choices: ['approve_once', 'approve_job', 'deny'] }""") + """
        const before = bar();
        env.type('creator-reply-text', '  go ahead  ');
        env.clickButton('Approve for this job', '#creator-reply');
        await env.settle();
        done({ before, sent: env.lastCall('POST', '/api/creator/resume/').body, after: bar().shown,
               pill: env.text('.creator-status-pill') });
    """)
    b = out["before"]
    assert b["shown"] is True and b["composer"] is False
    assert b["buttons"] == ["Approve once", "Approve for this job", "Deny"]
    assert "outside content" in b["notice"]
    assert out["sent"] == {"decision": "approve_job", "answer": "go ahead"}
    assert out["after"] is False and out["pill"] == "Waiting for you"


@pytest.mark.parametrize("pause, buttons, notice", [
    ("""{ kind: 'approval', question: 'q', scope: 'protected', protected: true, since: 't',
          action: { tool: 'read_file', command: '/etc/hosts' }, choices: ['approve_once', 'deny'] }""",
     ["Approve once", "Deny"], "protected path"),
    ("""{ kind: 'approval', question: 'q', scope: 'root', protected: false, since: 't',
          action: { tool: 'run_as_root', command: '{"command": "head /etc/x"}' }, choices: ['approve_once', 'deny'] }""",
     ["Run as root once", "Deny"], "as root"),
    ("""{ kind: 'approval', question: 'q', scope: 'host', protected: false, since: 't',
          action: { tool: 'host_exec', command: 'ls' }, choices: ['approve_once', 'approve_job', 'deny'] }""",
     ["Allow once", "Allow all host commands for this job", "Deny"], "as the user creator"),
    ("""{ kind: 'approval', question: 'q', since: 't', action: { tool: 'bash', command: 'ls' } }""",
     ["Approve once", "Deny"], ""),   # no choices from the server: never "for this job"
])
def test_each_kind_of_approval_gets_its_own_buttons(pause, buttons, notice):
    out = _run(_paused(pause) + "done(bar());")
    assert out["buttons"] == buttons
    assert notice in out["notice"]
    if not notice:
        assert out["notice"] == ""


def test_questions_by_option_by_text_and_blocked_without_an_answer():
    out = _run(_paused("""{ kind: 'question', question: 'Which port?', options: ['80', '8080'], since: 'q1' }""") + """
        const q = bar();
        env.clickButton('8080', '#creator-reply');
        await env.settle();
        const byChip = env.lastCall('POST', '/api/creator/resume/').body;
        done({ q, byChip });
    """)
    assert out["q"]["buttons"] == ["80", "8080", "Carry on without an answer"]
    assert out["byChip"] == {"answer": "8080"}

    out = _run(_paused("""{ kind: 'blocked', question: 'Need the password', options: [], since: 'b1' }""") + """
        const empty = env.buttons('#creator-reply');
        env.type('creator-reply-text', 'hunter2');
        const typed = env.buttons('#creator-reply');
        env.type('creator-reply-text', '');
        env.clickButton('Carry on without an answer', '#creator-reply');
        await env.settle();
        done({ empty, typed, sent: env.lastCall('POST', '/api/creator/resume/').body });
    """)
    assert out["empty"] == ["Carry on without an answer"] and out["typed"] == ["Send"]
    assert out["sent"] == {"answer": ""}


def test_resume_409_reloads_the_job_and_400_shows_the_reason():
    pause = """{ kind: 'approval', question: 'q', scope: 'untrusted', since: 't', action: { tool: 'bash', command: 'ls' },
                 choices: ['approve_once', 'deny'] }"""
    out = _run(_paused(pause, resume_reply="{ status: 409, body: { detail: 'not paused' } }") + """
        const loads = () => env.calls.filter(c => c.url === `/api/creator/status/${JOB}?since=0`).length;
        const before = loads();
        env.clickButton('Approve once', '#creator-reply');
        await env.settle(10);
        done({ reloaded: loads() - before });
    """)
    assert out["reloaded"] == 1
    out = _run(_paused(pause, resume_reply="{ status: 400, body: { detail: 'Choose one of: approve_once, deny.' } }") + """
        env.clickButton('Approve once', '#creator-reply');
        await env.settle();
        done({ msg: env.text('#creator-reply-msg'), error: env.$('#creator-reply-msg').classList.contains('error'),
               enabled: env.$$('#creator-reply button').every(b => !b.disabled) });
    """)
    assert out == {"msg": "Choose one of: approve_once, deny.", "error": True, "enabled": True}


def test_a_pause_while_minimized_puts_a_dot_on_the_buttons_and_notifies():
    out = _run(_with_running_job(extra_routes="""
          [`GET /api/creator/status/${JOB}?since=1`]: { ...job(), status: 'paused',
            pause: { kind: 'question', question: 'Which port?', options: [], since: 'q' } },
    """).replace("h.setup({", "h.setup({ notification: 'granted',") + """
        env.$('.modal-minimize-btn').click();
        const minimized = env.byId('creator-overlay').style.display;
        stream().emit({ seq: 1, type: 'paused', kind: 'question', question: 'Which port?' });
        await env.settle(10);
        const dots = ['tool-creator-btn', 'rail-creator'].map(id => env.byId(id).classList.contains('creator-needs-you'));
        panel.openPanel();   // restore
        const after = ['tool-creator-btn', 'rail-creator'].map(id => env.byId(id).classList.contains('creator-needs-you'));
        done({ minimized, dots, after, notes: env.notifications });
    """)
    assert out["minimized"] == "none"
    assert out["dots"] == [True, True] and out["after"] == [False, False]
    assert out["notes"] == [{"title": "Creator is waiting for you", "body": "Which port?", "tag": "creator-pause"}]


def test_no_notification_without_permission_and_none_while_shown():
    out = _run(_with_running_job(extra_routes="""
          [`GET /api/creator/status/${JOB}?since=1`]: { ...job(), status: 'paused', pause: null },
    """) + """
        stream().emit({ seq: 1, type: 'paused', kind: 'question', question: 'q' });
        await env.settle(10);
        done({ dot: env.byId('tool-creator-btn').classList.contains('creator-needs-you'),
               notes: env.notifications.length });
    """)
    assert out == {"dot": False, "notes": 0}


# ---------------------------------------------------------------------------
# Stop, Follow up, the report
# ---------------------------------------------------------------------------

def test_stop_is_one_click():
    out = _run(_with_running_job(extra_routes="""
          [`POST /api/creator/stop/${JOB}`]: { stopped: true },
    """) + """
        env.clickButton('Stop', '#creator-job-head');
        await env.settle();
        done({ stop: env.lastCall('POST', '/api/creator/stop/') !== null,
               label: env.text('#creator-stop-btn'), disabled: env.byId('creator-stop-btn').disabled });
    """)
    assert out == {"stop": True, "label": "Stopping…", "disabled": True}


def test_follow_up_starts_a_job_that_names_the_earlier_one_and_downloads_work():
    out = _run("""
        const finished = job({ status: 'done', has_report: true, finished_at: '2026-10-03T10:00:00Z',
                               task: 'Build a status page' });
        const env = h.setup({ routes: {
          'GET /api/model-endpoints': [],
          'GET /api/creator/jobs?limit=100': { jobs: [listed(finished)] },
          [`GET /api/creator/status/${JOB}?since=0`]: finished,
          [`GET /api/creator/report/${JOB}`]: { report: '# Report', audit_log: null },
          'POST /api/creator/start': { job_id: 'cr-000000000002' },
        } });
        const panel = await env.loadPanel();
        panel.openPanel();
        await env.settle();
        await panel.selectJob(JOB);
        await env.settle();
        env.clickButton('Download .md');
        const link = env.doc.clickedLinks[0];
        env.clickButton('Follow up', '#creator-job-head');
        await env.settle();
        const title = env.text('.creator-job-title');
        const following = env.text('#creator-follow-up');
        env.type('creator-task', 'Undo it');
        env.clickButton('Start');
        await env.settle();
        done({ link, title, following, start: env.lastCall('POST', '/api/creator/start').body,
               stream: env.streams.length });
    """)
    assert out["link"]["download"].startswith("creator-cr-000000000001-") and out["link"]["download"].endswith(".md")
    assert out["link"]["href"].startswith("blob:")
    assert out["title"] == "Follow-up job"
    assert out["following"].startswith("Following up onBuild a status page")
    assert out["start"]["follow_up_of"] == "cr-000000000001" and out["start"]["task"] == "Undo it"
    assert out["stream"] == 0   # a finished job has no live view


def test_task_and_command_text_go_in_as_text_never_html():
    out = _run(_with_running_job(job_over="""{ task: '<img src=x onerror=alert(1)>',
        events: [{ seq: 1, type: 'tool_start', tool: 'bash', command: '<b>ls</b>' }] }""") + """
        const nodes = env.$('#creator-timeline').descendants();
        done({ html: nodes.map(n => n.innerHTML).filter(Boolean),
               user: env.text('.creator-msg-user'), cmd: env.text('.creator-cmd-text') });
    """)
    assert out["html"] == []
    assert out["user"] == "<img src=x onerror=alert(1)>" and out["cmd"] == "<b>ls</b>"


# ---------------------------------------------------------------------------
# The root switch strip (admins)
# ---------------------------------------------------------------------------

def test_root_strip_switches_on_with_a_code_and_revokes():
    out = _run("""
        let on = false;
        const status = () => on
          ? { installed: true, available: true, on: true, remaining_s: 1800, expires_at: new Date(Date.now() + 1800e3).toISOString(),
              totp_ready: true, max_minutes: 90 }
          : { installed: true, available: true, on: false, totp_ready: true, max_minutes: 90, locked_s: 0 };
        const env = h.setup({ admin: true, routes: {
          'GET /api/model-endpoints': [],
          'GET /api/creator/jobs?limit=100': { jobs: [] },
          'GET /api/creator/root/status': () => status(),
          'POST /api/creator/root/enable': (body) => {
            if (body.code !== '123456') return { status: 400, body: { detail: "That code isn't right." } };
            on = true; return status();
          },
          'POST /api/creator/root/revoke': () => { on = false; return status(); },
        } });
        const panel = await env.loadPanel();
        panel.openPanel();
        await env.settle();
        const off = env.buttons('#creator-root');
        env.clickButton('Root: off', '#creator-root');
        const form = env.buttons('#creator-root-panel');
        env.byId('creator-root-code').value = '000000';
        env.clickButton('Turn root on', '#creator-root-panel');
        await env.settle();
        const wrong = env.text('#creator-root-panel .creator-root-note.error');
        env.clickButton('15 min', '#creator-root-panel');
        env.byId('creator-root-code').value = '123 456';
        env.clickButton('Turn root on', '#creator-root-panel');
        await env.settle();
        const sent = env.lastCall('POST', '/api/creator/root/enable').body;
        const onText = env.text('#creator-root');
        env.clickButton('Revoke', '#creator-root');
        await env.settle();
        done({ off, form, wrong, sent, onText, after: env.buttons('#creator-root'),
               panelHidden: env.byId('creator-root-panel').hidden });
    """)
    assert out["off"] == ["Root: off"]
    assert out["form"] == ["15 min", "30 min", "60 min", "Custom", "Turn root on"]
    assert out["wrong"] == "That code isn't right."
    assert out["sent"] == {"code": "123456", "minutes": 15}
    assert out["onText"].startswith("Root on · 30:00") or out["onText"].startswith("Root on · 29:5")
    assert out["onText"].endswith("Revoke")
    assert out["after"] == ["Root: off"] and out["panelHidden"] is True


def test_no_root_strip_for_non_admins_or_without_the_helper():
    out = _run("""
        const env = h.setup({ admin: false, routes: { 'GET /api/model-endpoints': [], 'GET /api/creator/jobs?limit=100': { jobs: [] } } });
        const panel = await env.loadPanel();
        panel.openPanel();
        await env.settle();
        done({ calls: env.calls.filter(c => c.url.includes('/root/')).length, strip: env.buttons('#creator-root') });
    """)
    assert out == {"calls": 0, "strip": []}
    out = _run("""
        const env = h.setup({ admin: true, routes: { 'GET /api/model-endpoints': [], 'GET /api/creator/jobs?limit=100': { jobs: [] },
                                                     'GET /api/creator/root/status': { installed: false } } });
        const panel = await env.loadPanel();
        panel.openPanel();
        await env.settle();
        done({ strip: env.buttons('#creator-root') });
    """)
    assert out == {"strip": []}


def test_a_root_card_shows_the_staged_files():
    pause = """{ kind: 'approval', question: 'q', scope: 'root', protected: false, since: 'r1',
        action: { tool: 'run_as_root', command: '{"command": "install /srv/creator-helper/work/x /usr/local/bin/x"}' },
        choices: ['approve_once', 'deny'],
        files: [{ path: '/srv/creator-helper/work/x', size: 12, text: 'echo staged', status: 'new' },
                { path: '/srv/creator-helper/work/y', size: 3, text: 'abc', status: 'unchanged' }] }"""
    out = _run(_paused(pause) + """
        const boxes = env.$$('#creator-reply-files details');
        done({ shown: h.visible(env.byId('creator-reply-files')),
               files: boxes.map(d => [d.querySelector('summary').textContent, d.open,
                                      d.querySelector('pre') ? d.querySelector('pre').textContent : null]),
               buttons: env.buttons('#creator-reply-choices') });
    """)
    assert out["shown"] is True
    assert out["files"] == [["/srv/creator-helper/work/x · 12 bytesnew", True, "echo staged"],
                            ["/srv/creator-helper/work/y · 3 bytesunchanged since you approved it", False, None]]
    assert out["buttons"] == ["Run as root once", "Deny"]


def test_other_cards_show_no_files():
    out = _run(_paused("""{ kind: 'approval', question: 'q', scope: 'untrusted', since: 'u1',
        action: { tool: 'bash', command: 'ls' }, choices: ['approve_once', 'deny'] }""") + """
        done({ shown: h.visible(env.byId('creator-reply-files')), n: env.$$('#creator-reply-files details').length });
    """)
    assert out == {"shown": False, "n": 0}
