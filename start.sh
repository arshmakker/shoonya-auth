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
tmux send-keys -t "$SESSION:proxy" "cd $DIR && python broker_proxy.py" Enter

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

# Window 1 — regimetrader
tmux new-window -t "$SESSION" -n "regime"
tmux send-keys -t "$SESSION:regime" \
    "cd $REGIME_DIR && BROKER_PROXY_URL=$PROXY_URL python main.py" Enter

# Window 2 — flowTrader
tmux new-window -t "$SESSION" -n "flow"
tmux send-keys -t "$SESSION:flow" \
    "cd $FLOW_DIR && BROKER_PROXY_URL=$PROXY_URL python main.py" Enter

# Land on proxy window
tmux select-window -t "$SESSION:proxy"
echo ""
echo "📺 tmux windows:"
echo "   Ctrl-b 0  →  proxy (broker session)"
echo "   Ctrl-b 1  →  regime (regimetrader)"
echo "   Ctrl-b 2  →  flow (flowTrader)"
echo "   Ctrl-b d  →  detach (keeps running)"
echo ""

# Only attach if we're in an interactive terminal
if [ -t 0 ]; then
    tmux attach-session -t "$SESSION"
else
    echo "ℹ️  Running in non-interactive mode. Attach with: tmux attach -t $SESSION"
    echo "   All processes are running in the background."
fi
