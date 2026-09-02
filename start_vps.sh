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

# WS feed: hybrid mode — fresh cached ticks served to consumers, REST fallback.
# Flip back to SHOONYA_FEED_MODE=shadow if validation ever needs re-running.
# SHOONYA_TICK_PERSIST_DIR turns on in-process persistence of every subscribed
# instrument (option legs, MCX, index) to per-day CSVs. In-process because the
# proxy already owns the tick store: a separate collector would cost another
# Python interpreter on a 1GB box plus an HTTP round-trip per instrument, to
# read memory this process already holds. The subscription set is ~200
# instruments (ws_subscribe_chain alone spans 33 strikes x 2 x 3 expiries), so
# rows are written on quote CHANGE with a 60s heartbeat rather than every pass —
# writing all of them every 5s would be ~920k rows/day, mostly identical repeats
# of far-OTM strikes that never requote. See tick_persist.py.
# SHOONYA_SHUTDOWN_TIME=23:58 keeps the proxy up for the MCX evening session
# (MCX closes 23:30, or 23:55 on US daylight-saving days). It belongs HERE and
# not in broker_proxy.py's default, because it is a property of this deployment
# — the droplet captures MCX, the Mac's start.sh does not. broker_proxy.py
# defaults to 15:40, a buffer past the NSE close; see the comment on
# _DEFAULT_SHUTDOWN_TIME there. A malformed value is fatal at startup by design,
# so the proxy will refuse to boot rather than quietly end the day at 15:40 and
# lose the whole commodity evening.
tmux send-keys -t "$SESSION:proxy" "cd $DIR && SHOONYA_FEED_MODE=hybrid SHOONYA_SHUTDOWN_TIME=23:58 SHOONYA_TICK_PERSIST_DIR='$REGIME_DIR' ./venv/bin/python broker_proxy.py" Enter
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

# Wide-net WS subscription: NIFTY index + full weekly strike chain around
# live spot (CE+PE). Backgrounded so session start never blocks on it; log
# lands in ws_chain_subscribe.log for post-boot inspection.
nohup bash -c "sleep 25 && cd '$DIR' && ./venv/bin/python tools/ws_subscribe_chain.py --positions-file '$REGIME_DIR/data/open_positions.json'" > "$DIR/ws_chain_subscribe.log" 2>&1 &

# MCX liquid-5 commodity futures (GOLD, SILVER, CRUDEOIL, COPPER,
# NATURALGAS — front-2 expiries each): touchline-only, observational, no
# trading rule attached. Backgrounded same as the NIFTY chain above.
nohup bash -c "sleep 25 && cd '$DIR' && ./venv/bin/python tools/mcx_ws_subscribe.py" > "$DIR/mcx_ws_subscribe.log" 2>&1 &

# mcx_collector.py (REST poller -> raw_data/futures/) was retired: the same 28
# contracts are streamed by mcx_ws_subscribe.py above and written by the proxy's
# own tick_persist, which costs no broker API calls at all. The pkill stays for
# one deploy cycle to stop an instance left running by a previous start.
pkill -f "tools/mcx_collector.py" 2>/dev/null || true

# BankNifty spot + near-month future + options onto the WS feed: reads
# whatever regimetrader's own SymbolManager already selected today (from
# market_data_YYYYMMDD/raw_data/{futures,options/BANKNIFTY}), so it doesn't
# duplicate BankNifty's expiry/strike selection logic here. CSV persistence
# for these instruments already happens via regimetrader's own DataCollector
# REST polling — this only adds WS cache coverage on top. Longer delay than
# the NIFTY/MCX subscribes since it depends on regimetrader (started below)
# having completed its first SymbolManager pass; if that hasn't happened
# yet, it logs and exits without retrying — same fire-once convention as
# the other backgrounded subscribes above.
nohup bash -c "sleep 45 && cd '$DIR' && ./venv/bin/python tools/banknifty_ws_subscribe.py" > "$DIR/banknifty_ws_subscribe.log" 2>&1 &

# Split proxy window: pane 0 (left) broker_proxy, pane 1 (right) regimetrader
#
# NOTE: `split-window -p <percent>` resolves the percentage against an
# attached client's size, and this session is created detached (-d) and
# never gets a client attached (headless/non-interactive launch, e.g. over
# SSH without a pty). With no client to resolve against, tmux 3.4 fails
# with "size missing" and the split — and everything after it — never
# happens. `-l <columns>` (absolute size) has no such dependency, so
# compute the half-width from the window's own known size instead.
WIN_WIDTH=$(tmux display-message -p -t "$SESSION:proxy" '#{window_width}')
tmux split-window -t "$SESSION:proxy" -h -l "$(( WIN_WIDTH / 2 ))"

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
