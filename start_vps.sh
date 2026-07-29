#!/usr/bin/env bash
# start_vps.sh — VPS trading session launcher (broker_proxy + regimetrader only)
# Trimmed copy of start.sh for the remote-vps-deploy migration: no flowTrader,
# no portfolio-advisor — those stay on the Mac. See
# /Users/arshdeep/.claude/plans/magical-humming-dragonfly.md for the full plan.
set -euo pipefail

SESSION="trading"
DIR="$(cd "$(dirname "$0")" && pwd)"
REGIME_DIR="$HOME/git/trading/regimetrader"
PROXY_URL="http://127.0.0.1:7890"
CRED_FILE="$HOME/.shoonya/cred.yml"

# Pre-flight: check credentials
if [ ! -f "$CRED_FILE" ]; then
    echo "❌ ERROR: $CRED_FILE not found"
    echo "   Run: ~/git/trading/shoonya-auth/login.py to set up credentials"
    exit 1
fi

# Check if credentials are filled in (not just the template)
if grep -q "YOUR_USER_ID\|YOUR_CLIENT_ID\|YOUR_64_CHAR" "$CRED_FILE"; then
    echo "❌ ERROR: $CRED_FILE has placeholder values"
    echo "   Edit ~/.shoonya/cred.yml and fill in:"
    echo "   - UID: your Shoonya user ID"
    echo "   - client_id: your Shoonya API client ID"
    echo "   - Secret_Code: your 64-char secret from Shoonya portal"
    exit 1
fi

# Kill any stale session from a previous run
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Force a fresh OAuth login on every start so the session binds to the CURRENT
# public IP (see start.sh for the ALGO_CHK rationale — same applies here even
# though the VPS IP is normally static, since it's a Reserved IP re-assigned
# manually during the live/paper toggle procedure).
echo "🔑 Forcing fresh OAuth login (clearing cached Access_token)..."
sed -i 's/^Access_token:.*/Access_token: ""/' "$CRED_FILE"

echo "🚀 Starting VPS trading session (broker_proxy + regimetrader)..."

# Window 0 — broker_proxy (handles auto-login if token is stale)
tmux new-session -d -s "$SESSION" -n "proxy" -x 220 -y 50

# Enable pane border labels (shows badge at top of each pane)
tmux set-option -t "$SESSION" pane-border-status top
tmux set-option -t "$SESSION" pane-border-format " #{pane_title} "

tmux send-keys -t "$SESSION:proxy" "cd $DIR && ./venv/bin/python broker_proxy.py" Enter
tmux select-pane -t "$SESSION:proxy.0" -T "🔌 broker_proxy"

# Wait up to 90s for proxy to be healthy
echo "⏳ Waiting for broker proxy to be ready..."
for i in $(seq 1 90); do
    if curl -sf "$PROXY_URL/health" \
        | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('ok') else 1)" \
        2>/dev/null; then
        echo "✅ Proxy ready (${i}s)"
        break
    fi
    sleep 1
    if [ "$i" -eq 90 ]; then
        echo "❌ Proxy did not become healthy in 90s"
        echo "   Attach to check: tmux attach -t $SESSION"
        # Only attach if we're in an interactive terminal — a non-interactive
        # invocation (cron, systemd, ssh without -t) has no tty for tmux to
        # attach to, and would otherwise error out or hang here instead of
        # reaching the exit 1 below.
        if [ -t 0 ]; then
            tmux attach-session -t "$SESSION"
        fi
        exit 1
    fi
done

# Split proxy window: pane 0 (left) broker_proxy, pane 1 (right) regimetrader
tmux split-window -t "$SESSION:proxy" -h -p 50

tmux send-keys -t "$SESSION:proxy.1" \
    "cd $REGIME_DIR && BROKER_PROXY_URL=$PROXY_URL ./venv/bin/python main.py" Enter
tmux select-pane -t "$SESSION:proxy.1" -T "📈 regimetrader"

echo ""
echo "📺 Layout:"
echo "   Pane 0 (left)  → broker_proxy"
echo "   Pane 1 (right) → regimetrader"
echo "   Ctrl-b arrow keys  → navigate panes"
echo "   Ctrl-b d           → detach (keeps running)"
echo ""

# Only attach if we're in an interactive terminal
if [ -t 0 ]; then
    tmux attach-session -t "$SESSION"
else
    echo "ℹ️  Running in non-interactive mode. Attach with: tmux attach -t $SESSION"
    echo "   All processes are running in the background."
fi
