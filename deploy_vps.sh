#!/usr/bin/env bash
# deploy_vps.sh — power on the DO droplet, SSH in, start the trading
# session (start_vps.sh), then start a second tmux session running
# Claude Code with Remote Control enabled so it can be driven from the app.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_HOST="droplet"                 # see ~/.ssh/config
REMOTE_DIR="~/git/trading/shoonya-auth"
CLAUDE_SESSION="claude_remote"
CLAUDE_RC_NAME="vps"               # name shown in Remote Control

echo "🔌 Ensuring droplet is powered on..."
"$SCRIPT_DIR/vps_power.sh" on

echo ""
echo "🚀 Starting trading session on $SSH_HOST..."
ssh "$SSH_HOST" "cd $REMOTE_DIR && ./start_vps.sh"

echo ""
echo "🤖 Starting Claude Code (Remote Control) on $SSH_HOST..."
ssh "$SSH_HOST" "
    tmux has-session -t $CLAUDE_SESSION 2>/dev/null && tmux kill-session -t $CLAUDE_SESSION
    tmux new-session -d -s $CLAUDE_SESSION -c $REMOTE_DIR 'claude --remote-control $CLAUDE_RC_NAME'
"

echo ""
echo "✅ Done."
echo "   Trading session:  ssh $SSH_HOST -t 'tmux attach -t trading'"
echo "   Claude session:   ssh $SSH_HOST -t 'tmux attach -t $CLAUDE_SESSION'"
echo "   List all live tmux sessions: ssh $SSH_HOST -t 'tmux ls | awk '\''{print \"\\033[32m\" \$0 \"\\033[0m\"}'\'''"
echo "   Claude Remote Control name: $CLAUDE_RC_NAME"
