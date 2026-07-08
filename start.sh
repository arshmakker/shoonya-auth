#!/usr/bin/env bash
# start.sh — daily trading session launcher
# Starts broker_proxy (with auto-login), then spawns regimetrader and
# flowTrader in separate tmux windows once the proxy is healthy.
set -euo pipefail

SESSION="trading"
DIR="$(cd "$(dirname "$0")" && pwd)"
REGIME_DIR="$HOME/git/trading/regimetrader"
FLOW_DIR="$HOME/git/trading/flowTrader"
ADVISOR_DIR="$HOME/git/trading/portfolio-advisor"
BSENSE_DIR="$HOME/git/trading/bsensearb"
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

# Force a fresh OAuth login on every start so the session binds to the CURRENT
# public IP. On a dynamic IP a cached token stays "valid" for reads, but its
# session is bound to the old IP and live orders get rejected with
# "ALGO_CHK: Invalid IP address". Blanking Access_token makes broker_proxy
# re-authenticate from scratch. (2026-07-08)
echo "🔑 Forcing fresh OAuth login (clearing cached Access_token)..."
sed -i '' 's/^Access_token:.*/Access_token: ""/' "$CRED_FILE"

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

# Split proxy window into 4 panes:
#   Pane 0 (top-left):     broker_proxy
#   Pane 1 (bottom-left):  regimetrader
#   Pane 2 (bottom-right): flowTrader
#   Pane 3 (top-right):    portfolio-advisor

# Split right half off (pane 1 on the right)
tmux split-window -t "$SESSION:proxy" -h -p 50

# Split right pane vertically: pane 1 (top-right), pane 2 (bottom-right)
tmux split-window -t "$SESSION:proxy.1" -v -p 50

# Split left pane (proxy) vertically: pane 0 (top-left), pane 3 (bottom-left)
tmux split-window -t "$SESSION:proxy.0" -v -p 40

# Pane 1 (top-right): regimetrader
tmux send-keys -t "$SESSION:proxy.1" \
    "cd $REGIME_DIR && BROKER_PROXY_URL=$PROXY_URL python main.py" Enter
tmux select-pane -t "$SESSION:proxy.1" -T "📈 regimetrader"

# Pane 2 (bottom-right): flowTrader
tmux send-keys -t "$SESSION:proxy.2" \
    "cd $FLOW_DIR && BROKER_PROXY_URL=$PROXY_URL python main.py" Enter
tmux select-pane -t "$SESSION:proxy.2" -T "🌊 flowTrader"

# Pane 3 (bottom-left): portfolio-advisor
tmux send-keys -t "$SESSION:proxy.3" \
    "cd $ADVISOR_DIR && BROKER_PROXY_URL=$PROXY_URL python main.py" Enter
tmux select-pane -t "$SESSION:proxy.3" -T "🧠 portfolio-advisor"

# Pane 4: bsensearb — split bottom-left pane vertically
tmux split-window -t "$SESSION:proxy.3" -v -p 40
tmux send-keys -t "$SESSION:proxy.4" \
    "cd $BSENSE_DIR && BROKER_PROXY_URL=$PROXY_URL python main.py" Enter
tmux select-pane -t "$SESSION:proxy.4" -T "⚡ bsensearb"

echo ""
echo "📺 Layout:"
echo "   Pane 0 (top-L)     → broker_proxy"
echo "   Pane 1 (top-R)     → regimetrader"
echo "   Pane 2 (bottom-R)  → flowTrader"
echo "   Pane 3 (bottom-L)  → portfolio-advisor (read-only, recommendations only)"
echo "   Pane 4 (bottom-L)  → bsensearb (NSE-BSE arbitrage scanner, paper mode)"
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
