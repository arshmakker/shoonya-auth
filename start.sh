#!/usr/bin/env bash
# start.sh — daily trading session launcher
# Starts broker_proxy (with auto-login), then spawns regimetrader and
# flowTrader in separate tmux windows once the proxy is healthy.
set -euo pipefail

SESSION="trading"
DIR="$(cd "$(dirname "$0")" && pwd)"
REGIME_DIR="$HOME/git/regimetrader"
FLOW_DIR="$HOME/git/flowTrader"
PROXY_URL="http://127.0.0.1:7890"
CRED_FILE="$HOME/.shoonya/cred.yml"

# Pre-flight: check credentials
if [ ! -f "$CRED_FILE" ]; then
    echo "❌ ERROR: $CRED_FILE not found"
    echo "   Run: ~/git/shoonya-auth/login.py to set up credentials"
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

echo "🚀 Starting trading session..."

# Window 0 — broker_proxy (handles auto-login if token is stale)
tmux new-session -d -s "$SESSION" -n "proxy" -x 220 -y 50

# Enable pane border labels (shows badge at top of each pane)
tmux set-option -t "$SESSION" pane-border-status top
tmux set-option -t "$SESSION" pane-border-format " #{pane_title} "

tmux send-keys -t "$SESSION:proxy" "cd $DIR && python broker_proxy.py" Enter
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
        tmux attach-session -t "$SESSION"
        exit 1
    fi
done

# Split proxy window into 3 panes: proxy (top), regime (bottom-left), flow (bottom-right)
# First split horizontally: proxy on top, bottom pane for regime+flow
tmux split-window -t "$SESSION:proxy" -h -p 50

# Split bottom pane vertically for regime and flow
tmux split-window -t "$SESSION:proxy.1" -v -p 50

# Pane 0 (top): proxy is already running
# Pane 1 (bottom-left): regimetrader
tmux send-keys -t "$SESSION:proxy.1" \
    "cd $REGIME_DIR && BROKER_PROXY_URL=$PROXY_URL python main.py" Enter
tmux select-pane -t "$SESSION:proxy.1" -T "📈 regimetrader"

# Pane 2 (bottom-right): flowTrader
tmux send-keys -t "$SESSION:proxy.2" \
    "cd $FLOW_DIR && BROKER_PROXY_URL=$PROXY_URL python main.py" Enter
tmux select-pane -t "$SESSION:proxy.2" -T "🌊 flowTrader"

echo ""
echo "📺 Layout:"
echo "   Pane 0 (top)       → proxy (broker_proxy.py)"
echo "   Pane 1 (bottom-L)  → regimetrader"
echo "   Pane 2 (bottom-R)  → flowTrader"
echo "   Ctrl-b arrow keys  → navigate panes"
echo "   Ctrl-b z           → zoom pane (Ctrl-b z again to unzoom)"
echo "   Ctrl-b d           → detach (keeps running)"
echo ""

# Only attach if we're in an interactive terminal
if [ -t 0 ]; then
    tmux attach-session -t "$SESSION"
else
    echo "ℹ️  Running in non-interactive mode. Attach with: tmux attach -t $SESSION"
    echo "   All processes are running in the background."
fi
