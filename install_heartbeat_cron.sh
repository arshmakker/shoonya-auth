#!/usr/bin/env bash
# install_heartbeat_cron.sh — schedule regimetrader's heartbeat watchdog on the
# droplet.
#
# Why this exists: tools/heartbeat_check.py detects silent death of the trading
# loop (stale data/pnl_snapshot.json during market hours) and pushes an operator
# alert over the same ntfy channel main.py uses. It was written for launchd on
# the Mac and never got scheduled after the move to the droplet, so the two
# failure modes in-process alerting structurally cannot cover — the process
# being dead, and the morning session never being started at all — went
# unwatched. main.py's own alerts (halt, kill switch, margin, mid-session auth)
# are unaffected and already working; this only covers "nothing is running".
#
# Safe to re-run: the crontab entry is keyed on MARKER and replaced in place.
set -euo pipefail

SSH_HOST="${SSH_HOST:-droplet}"                 # see ~/.ssh/config
REMOTE_DIR="/root/git/trading/regimetrader"
MARKER="# regimetrader-heartbeat"
LOGFILE="/var/log/regimetrader-heartbeat.log"

# Droplet runs UTC; IST has no DST, so UTC+5:30 is a fixed offset.
#
# The window is deliberately NARROWER than is_market_hours() (09:15-15:40 IST),
# because that function's bounds don't match when the snapshot is actually being
# written. Both edges would otherwise produce a false page every single day:
#
#   Open  — main.py starts ~09:05 IST but spends the first ~10 min in
#           SymbolManager setup. The first "Collection cycle complete" landed at
#           09:16-09:17 IST on 08-27/28/31. A check at 09:15 would read
#           yesterday's snapshot, see it >180s old, and fire. Start at 09:20 IST
#           (03:50 UTC) instead — after the first cycle on all observed days.
#   Close — the main loop stops at settings.SESSION_END (15:30) but
#           is_market_hours() stays true until 15:40. The snapshot goes stale at
#           15:33 and every run from then to 15:40 would fire. Stop at 15:25 IST
#           (09:55 UTC), the last */5 slot before SESSION_END.
#
# Net coverage 09:20-15:25 IST. The ~5 min surrendered at each end is worth far
# more than a daily false alarm, which would train you to ignore the channel.
# Two lines because cron can't express "from 03:50" in one field set; both carry
# MARKER so the installer replaces them as a unit.
HEARTBEAT_CMD="$REMOTE_DIR/venv/bin/python $REMOTE_DIR/tools/heartbeat_check.py >> $LOGFILE 2>&1"
CRON_LINE="50,55 3 * * 1-5 $HEARTBEAT_CMD $MARKER
*/5 4-9 * * 1-5 $HEARTBEAT_CMD $MARKER"

echo "🔍 Verifying the watchdog resolves a real push channel on $SSH_HOST..."
# Guards the 2026-09-01 bug: if this prints LogAlertChannel the alert goes to a
# logfile and pushes nothing, so installing the cron would be worse than
# useless — it would look monitored while being silent.
CHANNEL="$(ssh "$SSH_HOST" "cd $REMOTE_DIR && ./venv/bin/python -c \"
import sys; sys.path.insert(0, '.')
from tools.heartbeat_check import _load_ntfy_url_from_cred
from trading_system.config import settings
from trading_system.ops.alerts import build_channel
ch = build_channel(enabled=settings.ALERTS_ENABLED,
                   channel_type=settings.ALERTS_CHANNEL,
                   ntfy_topic_url=_load_ntfy_url_from_cred())
print(type(ch).__name__)
\"")"

echo "   Channel: $CHANNEL"
if [ "$CHANNEL" != "NtfyAlertChannel" ]; then
    echo "❌ Expected NtfyAlertChannel, got '$CHANNEL'."
    echo "   Check ALERTS_ENABLED / ALERTS_CHANNEL in trading_system/config/settings.py"
    echo "   and ALERTS_NTFY_TOPIC_URL in ~/.shoonya/cred.yml. Not installing."
    exit 1
fi

echo ""
echo "🩺 Dry run (market is presumably closed — expect exit 0 and no push)..."
ssh "$SSH_HOST" "cd $REMOTE_DIR && ./venv/bin/python tools/heartbeat_check.py; echo \"   exit=\$?\""

echo ""
echo "📅 Installing crontab entry..."
ssh "$SSH_HOST" "
    set -euo pipefail
    ( crontab -l 2>/dev/null | grep -vF '$MARKER' || true; echo '$CRON_LINE' ) | crontab -
    echo '   Installed. Current crontab:'
    crontab -l | sed 's/^/   /'
"

echo ""
echo "✅ Done. Watchdog runs every 5 min, 09:20-15:25 IST, Mon-Fri."
echo "   Log:    ssh $SSH_HOST 'tail -f $LOGFILE'"
echo "   Remove: ssh $SSH_HOST \"crontab -l | grep -vF '$MARKER' | crontab -\""
echo ""
echo "   Not yet verified end-to-end: that an alert actually lands on your"
echo "   phone. Confirm with regimetrader's own smoke test when you're awake:"
echo "     ssh $SSH_HOST 'cd $REMOTE_DIR && ./venv/bin/python tools/smoke_test_alerts.py'"
