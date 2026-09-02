#!/usr/bin/env bash
# deploy_vps.sh — power on the DO droplet, SSH in, and start the trading
# session (start_vps.sh).
#
# This script deliberately does NOT sync code. A stash/pull/pop + test-gate step
# was added and then removed on 2026-09-02, by operator decision: pushing code
# onto the live box is a separate act from starting the day, and folding it into
# the morning command means every `git push` to main becomes a deploy on the
# next start. Syncing stays a deliberate, separately-timed step.
#
# So this starts WHATEVER CHECKOUT IS ON THE DROPLET. To ship a change, pull it
# across first and confirm the commit, then run this:
#     ssh droplet "cd ~/git/trading/shoonya-auth && git pull origin main && git log --oneline -1"
#
# The second tmux session that ran `claude --remote-control` was removed
# 2026-09-01: it monitored nothing (an idle REPL, no /loop) while holding
# ~328MB RSS on a 1GB box. Operator alerting is main.py's own ntfy channel
# plus tools/heartbeat_check.py on cron — neither needs a resident session.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_HOST="droplet"                 # see ~/.ssh/config
REMOTE_DIR="~/git/trading/shoonya-auth"

echo "🔌 Ensuring droplet is powered on..."
"$SCRIPT_DIR/vps_power.sh" on

echo ""
echo "🚀 Starting trading session on $SSH_HOST..."
ssh "$SSH_HOST" "cd $REMOTE_DIR && ./start_vps.sh"

echo ""
echo "✅ Done."
echo "   Trading session:  ssh $SSH_HOST -t 'tmux attach -t trading'"
echo "   List all live tmux sessions: ssh $SSH_HOST -t 'tmux ls | awk '\''{print \"\\033[32m\" \$0 \"\\033[0m\"}'\'''"
