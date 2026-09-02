#!/usr/bin/env bash
# eod_housekeeping.sh — the droplet's end-of-day chores, run from this Mac.
#
# Runs after the proxy's 23:58 IST shutdown (see _DEFAULT_SHUTDOWN_TIME /
# SHOONYA_SHUTDOWN_TIME in broker_proxy.py), when nothing on the droplet is
# holding files open or trading:
#
#   1. BACKUP   — vps_backup.sh: rsync logs/data/market_data_* droplet → Mac
#   2. COMPRESS — compress_old_ticks.sh --apply: gzip tick CSVs >7 days old
#   3. SYNC     — git pull origin main on the droplet
#   4. VERIFY   — pytest on the droplet, so a bad pull is known tonight
#
# Why this exists: steps 1 and 2 had no trigger at all. `vps_power.sh off` was
# the only caller of vps_backup.sh, and that nightly power-off was abandoned
# once it turned out DigitalOcean bills a powered-off droplet at the full rate.
# Dropping it silently orphaned the backup — nothing had pulled a day's data
# down since, and nothing was compressing ticks on the 24G disk.
#
# Why the sync is HERE and not in deploy_vps.sh: pushing code onto the live box
# should not be welded to starting the trading day. At 00:20 there is a whole
# night to notice a failed pull or a failing test; at 09:05 there is not. This
# is also why step 4 exists — a sync you don't verify is just a slower surprise.
#
# NOT set -e. Every step runs even if an earlier one fails, because they are
# independent: a broken backup should not also cost you the compression and the
# sync. Failures are accumulated and reported once at the end, and pushed to
# ntfy so a silent nightly failure can't hide.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_HOST="droplet"                    # see ~/.ssh/config
REMOTE_DIR="~/git/trading/shoonya-auth"
BRANCH="main"                         # the droplet checkout tracks main
# Overridable so the failure path can be exercised without firing a real alert
# at the operator's phone (point it at /dev/null for a dry test).
CRED_FILE="${SHOONYA_CRED_FILE:-$HOME/.shoonya/cred.yml}"

# The trading day that just ended. This runs at 00:20, i.e. already the next
# calendar day, so the default is yesterday. That stays correct if the Mac was
# asleep and launchd catches up later the same morning. Override for a manual
# run against a specific day:  TRADING_DAY=20260902 ./eod_housekeeping.sh
TRADING_DAY="${TRADING_DAY:-$(date -v-1d +%Y%m%d)}"

FAILURES=()
step_failed() { FAILURES+=("$1"); echo "   ❌ $1"; }

# Is this running when it is meant to? The job fires at 00:20; anything in the
# small hours counts. Used ONLY to decide whether a still-live session is a real
# fault worth alerting on, or the expected state of an off-schedule manual run.
HOUR_NOW=$(date +%H)
if [ "$((10#$HOUR_NOW))" -lt 6 ]; then IN_EOD_WINDOW=1; else IN_EOD_WINDOW=0; fi

echo "🌙 EOD housekeeping for trading day $TRADING_DAY — $(date '+%Y-%m-%d %H:%M:%S %Z')"
[ "$IN_EOD_WINDOW" = 1 ] || echo "   (off-schedule run — the job is scheduled for 00:20)"
echo ""

# ── Pre-flight ───────────────────────────────────────────────────────────────
# Bail early rather than letting four steps each fail their own slow timeout.
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$SSH_HOST" true 2>/dev/null; then
    echo "❌ $SSH_HOST unreachable — nothing to do."
    FAILURES=("droplet unreachable")
else
    # Refuse to run while the session is still up. The proxy exits at 23:58; if
    # it is still alive, either the shutdown failed or this was started early,
    # and compressing/pulling underneath a live process is not worth the risk.
    if ssh "$SSH_HOST" 'pgrep -f "broker_proxy.py|venv/bin/python main.py" >/dev/null' 2>/dev/null; then
        echo "⚠️  Trading processes are STILL RUNNING on $SSH_HOST."
        echo "   Skipping every step — this job must not touch a live session."
        if [ "$IN_EOD_WINDOW" = 1 ]; then
            # Scheduled time, session still up: the 23:58 shutdown genuinely
            # failed. Worth waking someone for.
            FAILURES=("session still running at EOD — the 23:58 proxy shutdown did not happen")
        else
            # Run by hand outside the window, e.g. mid-session. A live session
            # is the CORRECT state here, so this is not a failure and must not
            # page anyone. Alerting on an expected condition is how a channel
            # gets muted, and a muted channel is worse than no channel.
            echo ""
            echo "ℹ️  This was an off-schedule run at $(date '+%H:%M') — the job is"
            echo "   scheduled for 00:20, after the proxy's 23:58 shutdown."
            echo "   A live session at this hour is expected, not a fault."
            echo "   Nothing was done and no alert was sent."
            exit 2
        fi
    else

        # ── 1. Backup ────────────────────────────────────────────────────────
        echo "1️⃣  Backup — pulling droplet data to this Mac"
        if BACKUP_STAMP="$TRADING_DAY" "$SCRIPT_DIR/vps_backup.sh"; then
            echo "   ✅ backup done"
        else
            step_failed "vps_backup.sh failed"
        fi
        echo ""

        # ── 2. Compress ──────────────────────────────────────────────────────
        # After the backup, never before: the day's data is safely on the Mac
        # before anything on the droplet is rewritten. Only touches files >7
        # days old, so today's capture is never in scope.
        echo "2️⃣  Compress — gzipping tick CSVs older than 7 days on the droplet"
        if "$SCRIPT_DIR/compress_old_ticks.sh" --apply; then
            echo "   ✅ compression done"
        else
            step_failed "compress_old_ticks.sh failed"
        fi
        echo ""

        # ── 3. Sync ──────────────────────────────────────────────────────────
        echo "3️⃣  Sync — pulling origin/$BRANCH onto the droplet"
        if ssh "$SSH_HOST" "
            set -euo pipefail
            cd $REMOTE_DIR
            STASHED=0
            if [ -n \"\$(git status --short)\" ]; then
                echo '   local changes found — stashing across the pull:'
                git status --short | sed 's/^/     /'
                git stash push -m 'eod_housekeeping.sh auto-stash' >/dev/null
                STASHED=1
            fi
            git pull origin $BRANCH
            if [ \"\$STASHED\" = 1 ]; then
                # A conflicting pop leaves the tree in UU with conflict markers.
                # start_vps.sh is one of the tracked files, and markers in a
                # shell script are a syntax error — so reset to the pulled code
                # rather than leaving tomorrow's start to trip over it. The
                # stash survives a conflicted pop, so the edit is recoverable.
                if ! git stash pop; then
                    echo ''
                    echo '   ⚠️  local changes conflict with the pulled commit.'
                    git reset -q --hard HEAD
                    echo '   tree reset to the pulled code; your edit is SAFE in the stash:'
                    echo '       ssh $SSH_HOST \"cd $REMOTE_DIR && git stash show -p stash@{0}\"'
                    exit 1
                fi
            fi
            echo \"   now at: \$(git log --oneline -1)\"
        "; then
            echo "   ✅ sync done"
        else
            step_failed "droplet sync failed (see stash message above)"
        fi
        echo ""

        # ── 4. Verify ────────────────────────────────────────────────────────
        # The point of syncing at night: find out now, not at 09:05 tomorrow.
        echo "4️⃣  Verify — running the test suite on the droplet"
        if ssh "$SSH_HOST" "cd $REMOTE_DIR && ./venv/bin/python -m pytest tests -q" 2>&1 | tail -3; then
            echo "   ✅ tests pass — the droplet is ready for tomorrow's start"
        else
            step_failed "pytest FAILED on the droplet — fix before ./deploy_vps.sh"
        fi
        echo ""
    fi
fi

# ── Report ───────────────────────────────────────────────────────────────────
echo "────────────────────────────────────────────────────────"
if [ ${#FAILURES[@]} -eq 0 ]; then
    echo "✅ EOD housekeeping complete for $TRADING_DAY — all four steps OK."
    exit 0
fi

echo "❌ EOD housekeeping finished with ${#FAILURES[@]} failure(s) for $TRADING_DAY:"
printf '   • %s\n' "${FAILURES[@]}"

# Push to the same ntfy channel main.py and heartbeat_check.py already use, so
# this lands where operator alerts are already being read. Only on failure — a
# nightly "all fine" push is how a channel gets muted, and a muted channel is
# worse than no channel. Never echo the URL: it is the topic secret.
NTFY_URL="$(sed -n 's/^ALERTS_NTFY_TOPIC_URL:[[:space:]]*//p' "$CRED_FILE" 2>/dev/null | tr -d '"'"'" | tr -d '\r')"
if [ -n "$NTFY_URL" ]; then
    BODY="EOD housekeeping failed for $TRADING_DAY: $(printf '%s; ' "${FAILURES[@]}")"
    if curl -fsS -m 20 -H "Title: EOD housekeeping FAILED" -d "$BODY" "$NTFY_URL" >/dev/null 2>&1; then
        echo "   📣 pushed alert to ntfy"
    else
        echo "   ⚠️  could not push the ntfy alert (check ALERTS_NTFY_TOPIC_URL)"
    fi
else
    echo "   ⚠️  no ALERTS_NTFY_TOPIC_URL in $CRED_FILE — no alert sent"
fi
exit 1
