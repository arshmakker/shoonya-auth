#!/usr/bin/env bash
# deploy_vps.sh — power on the DO droplet, sync this repo onto it, and start
# the trading session (start_vps.sh).
#
# The sync step was added 2026-09-02. Before it, this script only ever powered
# on and launched, so the droplet ran whatever checkout happened to be sitting
# there — the name promised a deploy and delivered a launcher. Nothing else
# pulled shoonya-auth either (deploy_regimetrader.sh covers only the
# regimetrader repo), so every change to this repo needed a remembered manual
# `git pull`. Forgetting it failed silently, in the worst way available: the
# code kept working, just the old version of it.
#
# Order matters — the pull has to precede start_vps.sh, because start_vps.sh is
# itself one of the files being pulled.
#
# The second tmux session that ran `claude --remote-control` was removed
# 2026-09-01: it monitored nothing (an idle REPL, no /loop) while holding
# ~328MB RSS on a 1GB box. Operator alerting is main.py's own ntfy channel
# plus tools/heartbeat_check.py on cron — neither needs a resident session.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_HOST="droplet"                 # see ~/.ssh/config
REMOTE_DIR="~/git/trading/shoonya-auth"
BRANCH="main"                      # the droplet checkout tracks main

echo "🔌 Ensuring droplet is powered on..."
"$SCRIPT_DIR/vps_power.sh" on

echo ""
echo "📦 Syncing $REMOTE_DIR to origin/$BRANCH..."
# Stash/pop mirrors deploy_regimetrader.sh: that repo keeps a deliberately
# uncommitted PAPER_TRADE_MODE override on the droplet. This one currently has
# no such override and a clean tree, but a pull that silently discards a local
# edit is a bad failure to leave armed in a script run half-asleep at 9am.
ssh "$SSH_HOST" "
    set -euo pipefail
    cd $REMOTE_DIR
    STASHED=0
    if [ -n \"\$(git status --short)\" ]; then
        echo '   Local changes found — stashing across the pull:'
        git status --short | sed 's/^/     /'
        git stash push -m 'deploy_vps.sh auto-stash' >/dev/null
        STASHED=1
    fi
    git pull origin $BRANCH
    if [ \"\$STASHED\" = 1 ]; then
        # A conflicting pop leaves the tree in UU with conflict markers. That
        # matters more here than in a normal repo: the very next step executes
        # start_vps.sh, and markers in a shell script are a bash syntax error.
        # set -e would abort before that — but it would abort leaving the tree
        # broken, so the retry AND the 'start manually' hint below both fail
        # confusingly. Reset to the pulled code instead. The stash is preserved
        # on a conflicted pop, so the local edit is recoverable, not discarded.
        if ! git stash pop; then
            echo ''
            echo '   ⚠️  Local changes conflict with the pulled commit.'
            git reset -q --hard HEAD
            echo '   Tree reset to the pulled code (clean and runnable).'
            echo '   Your edit is SAFE in the stash — recover it with:'
            echo '       ssh droplet \"cd $REMOTE_DIR && git stash show -p stash@{0}\"'
            exit 1
        fi
    fi
    echo \"   now at: \$(git log --oneline -1)\"
"

echo ""
echo "🧪 Running test suite on the pulled code before starting..."
# Gate the start, don't just report. A broken broker_proxy.py that still boots
# is worse than not starting: regimetrader connects, trades against it, and the
# damage is real. Refusing to start costs a morning; the alternative costs money.
if ! ssh "$SSH_HOST" "cd $REMOTE_DIR && ./venv/bin/python -m pytest tests -q"; then
    echo ""
    echo "❌ Tests failed on $SSH_HOST after the pull. NOT starting the session."
    echo "   Fix and re-run, or start manually: ssh $SSH_HOST 'cd $REMOTE_DIR && ./start_vps.sh'"
    exit 1
fi
echo "   ✅ Tests pass."

echo ""
echo "🚀 Starting trading session on $SSH_HOST..."
ssh "$SSH_HOST" "cd $REMOTE_DIR && ./start_vps.sh"

echo ""
echo "✅ Done."
echo "   Trading session:  ssh $SSH_HOST -t 'tmux attach -t trading'"
echo "   List all live tmux sessions: ssh $SSH_HOST -t 'tmux ls | awk '\''{print \"\\033[32m\" \$0 \"\\033[0m\"}'\'''"
