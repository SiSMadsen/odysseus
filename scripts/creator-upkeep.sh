#!/usr/bin/env bash
# Creator upkeep: apply what's in this folder and check that it all works.
# (docs/creator-plan.md, Phase 9a; host_helper/README.md.)
#
#   scripts/creator-upkeep.sh               apply changes, rebuild, check everything
#   scripts/creator-upkeep.sh --no-rebuild  the same, without rebuilding the container
#   scripts/creator-upkeep.sh --check       change nothing: only report
#
# Run it as yourself, from anywhere; it asks for sudo once. It never fetches
# code (no git pull): it applies what is in this folder.
#
# What it does, in order:
#   1. the folder: right place, branch, uncommitted changes, the helpers parse
#      with the host's own python3 (standard library only);
#   2. the host helper: installs creator_helper.py, its unit and the polkit
#      rule when they differ from this folder, restarts it only if something
#      changed, then checks the user, folders, ACLs and that it's running;
#   3. the root helper: the same for root_helper.py and its unit (a restart
#      switches root off), then its folders, key, watchdog and switch;
#   4. .env has both overlays; the container is rebuilt (unless --no-rebuild
#      or --check) and comes up;
#   5. inside the container: the 5a check (the tool user), and both helpers
#      answer.
# Every line says PASS, FIXED (changed now), WARN or FAIL; the summary at the
# end counts them. Exit code 0 only without FAIL.

set -u

MODE=apply
REBUILD=1
for arg in "$@"; do
  case "$arg" in
    --check) MODE=check; REBUILD=0 ;;
    --no-rebuild) REBUILD=0 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HH="$REPO/host_helper"
APP_UID=1000

if [ -t 1 ]; then
  C_PASS=$'\e[32m'; C_FIX=$'\e[36m'; C_WARN=$'\e[33m'; C_FAIL=$'\e[31m'; C_OFF=$'\e[0m'; C_B=$'\e[1m'
else
  C_PASS=; C_FIX=; C_WARN=; C_FAIL=; C_OFF=; C_B=
fi
N_PASS=0; N_FIX=0; N_WARN=0; N_FAIL=0
FAILS=()
WARNS=()

pass() { N_PASS=$((N_PASS + 1)); printf '  %sPASS%s  %s\n' "$C_PASS" "$C_OFF" "$1"; }
fixed() { N_FIX=$((N_FIX + 1)); printf '  %sFIXED%s %s\n' "$C_FIX" "$C_OFF" "$1"; }
warn() { N_WARN=$((N_WARN + 1)); WARNS+=("$1"); printf '  %sWARN%s  %s\n' "$C_WARN" "$C_OFF" "$1"; }
fail() { N_FAIL=$((N_FAIL + 1)); FAILS+=("$1"); printf '  %sFAIL%s  %s\n' "$C_FAIL" "$C_OFF" "$1"; }
section() { printf '\n%s%s%s\n' "$C_B" "$1" "$C_OFF"; }
# Run a check command quietly; its output is shown only when it fails.
quiet() { local out; out="$("$@" 2>&1)"; local rc=$?; [ $rc -ne 0 ] && [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/        /' | tail -n 15; return $rc; }

# install_if_changed SRC DEST MODE LABEL -> sets CHANGED=1 when it installs
install_if_changed() {
  local src="$1" dest="$2" mode="$3" label="$4"
  if sudo test -f "$dest" && sudo cmp -s "$src" "$dest"; then
    pass "$label is up to date"
    return 0
  fi
  if [ "$MODE" = check ]; then
    if sudo test -f "$dest"; then fail "$label differs from this folder ($dest)"; else fail "$label is not installed ($dest)"; fi
    return 0
  fi
  if sudo install -D -o root -g root -m "$mode" "$src" "$dest"; then
    fixed "$label installed ($dest)"
    CHANGED=1
  else
    fail "$label could not be installed to $dest"
  fi
}

service_running() {
  local unit="$1"
  if systemctl is-active --quiet "$unit"; then pass "$unit is running"; else
    fail "$unit is not running (journalctl -u $unit -n 50)"; fi
  if systemctl is-enabled --quiet "$unit" 2>/dev/null; then pass "$unit starts at boot"; else
    warn "$unit is not enabled (sudo systemctl enable $unit)"; fi
}

# ---------------------------------------------------------------------------
section "1. This folder"
# ---------------------------------------------------------------------------

if [ -f "$REPO/docker-compose.yml" ] && [ -f "$HH/root_helper.py" ]; then
  pass "Odysseus folder: $REPO"
else
  fail "$REPO doesn't look like the Odysseus folder"; exit 1
fi
branch="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
pass "branch: $branch ($(git -C "$REPO" log -1 --format='%h %s' 2>/dev/null | cut -c1-70))"
if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
  warn "uncommitted changes in the folder: they are what gets applied"
fi
for f in creator_helper.py root_helper.py; do
  if quiet /usr/bin/python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$HH/$f"; then
    pass "$f parses with the host's python3"
  else
    fail "$f doesn't parse with /usr/bin/python3: not installing anything"; exit 1
  fi
done

echo "  (sudo is needed for the helpers and Docker)"
if ! sudo -v; then fail "sudo is needed"; exit 1; fi

# ---------------------------------------------------------------------------
section "2. Host helper (runs commands as creator)"
# ---------------------------------------------------------------------------

if id creator >/dev/null 2>&1; then pass "user creator exists"; else
  fail "user creator is missing (host_helper/README.md, install step 1)"; fi
if sudo -l -U creator 2>/dev/null | grep -q "may run"; then
  fail "creator has sudo rights: it must have none"
else
  pass "creator has no sudo rights"
fi
CHANGED=0
install_if_changed "$HH/creator_helper.py" /opt/creator-helper/creator_helper.py 0644 "creator_helper.py"
install_if_changed "$HH/creator-helper.service" /etc/systemd/system/creator-helper.service 0644 "creator-helper.service"
install_if_changed "$HH/50-creator-apache.rules" /etc/polkit-1/rules.d/50-creator-apache.rules 0644 "polkit rule (Apache)"
HOST_CHANGED=$CHANGED
if quiet systemd-analyze verify /etc/systemd/system/creator-helper.service; then pass "creator-helper.service verifies"; else
  fail "creator-helper.service doesn't verify"; fi
if sudo test -d /srv/creator-helper && [ "$(sudo stat -c %U /srv/creator-helper)" = creator ]; then
  pass "/srv/creator-helper belongs to creator"
else
  fail "/srv/creator-helper is missing or not creator's (README install step 3)"
fi
if [ "$HOST_CHANGED" = 1 ]; then
  sudo systemctl daemon-reload
  if sudo systemctl restart creator-helper; then fixed "creator-helper restarted (a host command running now was stopped)"; else
    fail "creator-helper failed to restart (journalctl -u creator-helper -n 50)"; fi
  sleep 1
fi
service_running creator-helper
if sudo test -S /srv/creator-helper/helper.sock; then pass "its socket exists"; else fail "no socket at /srv/creator-helper/helper.sock"; fi
if [ -d /var/www/html ]; then
  if getfacl -p /var/www/html 2>/dev/null | grep -q '^user:creator:rwx'; then pass "creator may edit /var/www/html"; else
    warn "creator has no ACL on /var/www/html (README step 4a): web page tasks will get Permission denied"; fi
fi
if [ -d /var/log/apache2 ]; then
  if sudo getfacl -p /var/log/apache2 2>/dev/null | grep -q '^user:creator:r'; then
    if sudo sh -c 'for f in /var/log/apache2/*.log; do getfacl -p "$f" 2>/dev/null | grep -q "^user:creator:r" || exit 1; done'; then
      pass "creator may read Apache's logs"
    else
      warn "some Apache logs lack creator's ACL: sudo sh -c 'setfacl -m u:creator:r /var/log/apache2/*.log'"
    fi
  else
    warn "creator can't read /var/log/apache2 (README step 4b)"
  fi
fi

# ---------------------------------------------------------------------------
section "3. Root helper (the switch, the watchdog, root commands)"
# ---------------------------------------------------------------------------

CHANGED=0
install_if_changed "$HH/root_helper.py" /opt/creator-root/root_helper.py 0644 "root_helper.py"
# The installed unit may name another Odysseus folder: compare the rest.
unit_src="$HH/creator-root-helper.service"
unit_dst=/etc/systemd/system/creator-root-helper.service
want_dir="$(grep -o -- '--odysseus-dir [^ ]*' "$unit_src" | awk '{print $2}')"
if sudo test -f "$unit_dst"; then
  have_dir="$(sudo grep -o -- '--odysseus-dir [^ ]*' "$unit_dst" | awk '{print $2}')"
else
  have_dir=""
fi
install_if_changed "$unit_src" "$unit_dst" 0644 "creator-root-helper.service"
ROOT_CHANGED=$CHANGED
if [ "$want_dir" != "$REPO" ]; then
  warn "the unit's --odysseus-dir is $want_dir, but this folder is $REPO: edit $unit_dst so the watchdog guards the right folder"
elif [ -n "$have_dir" ] && [ "$have_dir" != "$REPO" ] && [ "$ROOT_CHANGED" = 0 ]; then
  warn "the installed unit's --odysseus-dir is $have_dir, this folder is $REPO"
else
  pass "the watchdog guards this folder ($REPO)"
fi
if quiet systemd-analyze verify "$unit_dst"; then pass "creator-root-helper.service verifies"; else
  fail "creator-root-helper.service doesn't verify"; fi
if sudo test -d /srv/creator-root \
   && [ "$(sudo stat -c '%U %a' /srv/creator-root)" = "root 700" ] \
   && sudo getfacl -pn /srv/creator-root 2>/dev/null | grep -q "^user:$APP_UID:--x"; then
  pass "/srv/creator-root: root, 0700, uid $APP_UID may pass through"
else
  if [ "$MODE" = apply ] && sudo test -d /srv/creator-root; then
    sudo chown root:root /srv/creator-root && sudo chmod 0700 /srv/creator-root \
      && sudo setfacl -m "u:$APP_UID:x" /srv/creator-root \
      && fixed "/srv/creator-root set to root, 0700, uid $APP_UID may pass through" \
      || fail "couldn't fix /srv/creator-root"
    ROOT_CHANGED=1
  else
    fail "/srv/creator-root must be root's, 0700, with an ACL u:$APP_UID:x (README, the root helper, step 2)"
  fi
fi
if sudo test -f /etc/creator-root/totp.key && [ "$(sudo stat -c '%U %a' /etc/creator-root/totp.key)" = "root 600" ]; then
  pass "the authenticator key is root's, 0600"
else
  fail "no usable authenticator key: sudo python3 /opt/creator-root/root_helper.py setup-totp"
fi
if [ "$(cat /proc/sys/fs/protected_hardlinks 2>/dev/null)" = 1 ]; then pass "fs.protected_hardlinks is on"; else
  warn "fs.protected_hardlinks is off: automatic chmod/chown will ask for approval"; fi
if [ "$ROOT_CHANGED" = 1 ]; then
  sudo systemctl daemon-reload
  if sudo systemctl restart creator-root-helper; then fixed "creator-root-helper restarted (root is off now)"; else
    fail "creator-root-helper failed to restart (journalctl -u creator-root-helper -n 50)"; fi
  sleep 1
fi
service_running creator-root-helper
ROOT_PY=/opt/creator-root/root_helper.py
if out="$(sudo /usr/bin/python3 "$ROOT_PY" status 2>&1)"; then
  pass "the switch answers: $(printf '%s' "$out" | head -n 1)"
  printf '%s\n' "$out" | grep -q '^Note:' && warn "$(printf '%s' "$out" | grep '^Note:' | head -n 1)"
else
  fail "the root helper doesn't answer on its control socket: $out"
fi
verdict() { sudo /usr/bin/python3 "$ROOT_PY" check "$1" 2>&1; }
v="$(verdict 'apt-get update')"
case "$v" in automatic:*) pass "watchdog: 'apt-get update' is automatic" ;;
  *) fail "watchdog: 'apt-get update' should be automatic, got: $v" ;; esac
printf '%s\n' "$v" | grep -q '^Note:' && warn "watchdog $(printf '%s' "$v" | grep '^Note:' | head -n 1)"
v="$(verdict 'systemctl stop creator-root-helper')"
case "$v" in refused:*) pass "watchdog: touching the helpers is refused (a check never switches root off)" ;;
  *) fail "watchdog: 'systemctl stop creator-root-helper' should be refused, got: $v" ;; esac
v="$(verdict 'head -n 5 /etc/hosts')"
case "$v" in approval:*) pass "watchdog: anything else needs approval" ;;
  *) fail "watchdog: 'head -n 5 /etc/hosts' should need approval, got: $v" ;; esac

# ---------------------------------------------------------------------------
section "4. Docker"
# ---------------------------------------------------------------------------

compose_line="$(grep -E '^COMPOSE_FILE=' "$REPO/.env" 2>/dev/null | tail -n 1)"
for overlay in docker/creator-helper.yml docker/creator-root-helper.yml; do
  if printf '%s' "$compose_line" | grep -q "$overlay"; then pass ".env enables $overlay"; else
    fail ".env's COMPOSE_FILE lacks $overlay"; fi
done
cd "$REPO" || exit 1
if [ "$REBUILD" = 1 ]; then
  echo "  rebuilding (sudo docker compose up -d --build); this takes a while…"
  if sudo docker compose up -d --build >/tmp/creator-upkeep-build.log 2>&1; then
    fixed "container rebuilt and started (log: /tmp/creator-upkeep-build.log)"
  else
    fail "the rebuild failed: see /tmp/creator-upkeep-build.log"
    tail -n 15 /tmp/creator-upkeep-build.log | sed 's/^/        /'
  fi
fi
in_app() { sudo docker compose exec -T -u odysseus odysseus "$@"; }
ready=0
for _ in $(seq 1 60); do
  if in_app python -c "print('ok')" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
if [ "$ready" = 1 ]; then pass "the odysseus container is up"; else fail "the odysseus container isn't answering (sudo docker compose ps)"; fi

# ---------------------------------------------------------------------------
section "5. Inside the container"
# ---------------------------------------------------------------------------

if [ "$ready" = 1 ]; then
  if out="$(in_app python -m src.tool_user --check 2>&1)"; then
    pass "5a check: the agent's tools run as their own user ($(printf '%s\n' "$out" | grep -c PASS) checks)"
  else
    fail "5a check failed:"
    printf '%s\n' "$out" | grep -v '^PASS' | sed 's/^/        /' | tail -n 15
  fi
  if out="$(in_app python -c "
import asyncio, sys
from src import creator_host_helper as h
r = asyncio.run(h.hello())
rep = r.get('reply') or {}
print(r.get('error') or 'runs as %s, can do: %s' % (rep.get('user'), ', '.join(rep.get('capabilities') or [])))
sys.exit(0 if r.get('ok') and 'run' in (rep.get('capabilities') or []) else 1)
" 2>&1)"; then pass "host helper from the container: $out"; else fail "host helper from the container: $out"; fi
  if out="$(in_app python -c "
import asyncio, sys
from src import creator_root_helper as c
r = asyncio.run(c.ask({'type': 'hello'}))
print('version %s, runs commands: %s, root is %s' % (r.get('version'), r.get('runs_commands'), 'on' if r.get('on') else 'off'))
sys.exit(0 if r.get('ok') and r.get('runs_commands') else 1)
" 2>&1)"; then pass "root helper from the container: $out"; else fail "root helper from the container: $out"; fi
fi

# ---------------------------------------------------------------------------
section "Summary"
# ---------------------------------------------------------------------------

printf '  %s%d PASS%s, %s%d FIXED%s, %s%d WARN%s, %s%d FAIL%s\n' \
  "$C_PASS" "$N_PASS" "$C_OFF" "$C_FIX" "$N_FIX" "$C_OFF" "$C_WARN" "$N_WARN" "$C_OFF" "$C_FAIL" "$N_FAIL" "$C_OFF"
if [ "$N_FAIL" -gt 0 ]; then
  echo "  Failed:"; for f in "${FAILS[@]}"; do echo "    - $f"; done
fi
if [ "$N_WARN" -gt 0 ]; then
  echo "  Warnings:"; for w in "${WARNS[@]}"; do echo "    - $w"; done
fi
if [ "$N_FIX" -gt 0 ] && [ "$REBUILD" = 1 ]; then
  echo "  Reload the Odysseus page twice (the service worker serves the old page first)."
fi
[ "$N_FAIL" -eq 0 ]
