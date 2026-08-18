#!/usr/bin/env bash
# vps_backup.sh — pull logs/data from the droplet to this Mac before shutdown.
#
# Archives regimetrader + shoonya-auth logs/data into a timestamped
# droplet_backup_YYYYMMDD/ dir (matching the manual precedent in
# droplet_backup_20260814/MANIFEST.md), and merges the day's
# market_data_*/ tick dir into the live regimetrader tree (regenerable,
# non-conflicting — safe to merge directly).
#
# Deliberately does NOT touch the live regimetrader/data/ tree — the
# droplet and Mac each maintain independent paper_trades.csv / state,
# and blindly merging destroys real trade history. That data only lands
# in the timestamped archive here; see tools/sync_from_vps.sh in
# regimetrader for the paper_trades_vps.csv-style side-file convention.
set -euo pipefail

SSH_HOST="droplet"
REMOTE_REGIME="~/git/trading/regimetrader"
REMOTE_AUTH="~/git/trading/shoonya-auth"
LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d)"
BACKUP_DIR="$LOCAL_ROOT/droplet_backup_$STAMP"

echo "📦 Pulling droplet logs/data → $BACKUP_DIR"
mkdir -p "$BACKUP_DIR/regimetrader" "$BACKUP_DIR/shoonya-auth"

echo "  regimetrader/{logs,data,market_data_*,option_snapshots,docs,ledger_summary.csv}"
rsync -avz --exclude='venv/' --exclude='symbols/' \
  "$SSH_HOST:$REMOTE_REGIME/logs" \
  "$SSH_HOST:$REMOTE_REGIME/data" \
  "$SSH_HOST:$REMOTE_REGIME/option_snapshots" \
  "$SSH_HOST:$REMOTE_REGIME/docs" \
  "$SSH_HOST:$REMOTE_REGIME/ledger_summary.csv" \
  "$BACKUP_DIR/regimetrader/" 2>&1 || echo "  ⚠️  some regimetrader paths missing, continuing"

rsync -avz --include='market_data_*/***' --exclude='*' \
  "$SSH_HOST:$REMOTE_REGIME/" "$BACKUP_DIR/regimetrader/" \
  2>&1 || echo "  ⚠️  market_data_*/ pull failed, continuing"

echo "  shoonya-auth/{order_debug.log,.claude,CLAUDE.md,start_vps.sh}"
rsync -avz \
  "$SSH_HOST:$REMOTE_AUTH/order_debug.log" \
  "$SSH_HOST:$REMOTE_AUTH/.claude" \
  "$SSH_HOST:$REMOTE_AUTH/CLAUDE.md" \
  "$SSH_HOST:$REMOTE_AUTH/start_vps.sh" \
  "$BACKUP_DIR/shoonya-auth/" 2>&1 || echo "  ⚠️  some shoonya-auth paths missing, continuing"

echo ""
echo "🔀 Merging today's market_data_$STAMP/ into the live regimetrader tree..."
if [ -d "$BACKUP_DIR/regimetrader/market_data_$STAMP" ]; then
  # Already pulled from the droplet above — copy locally instead of a second SSH round-trip.
  rsync -a "$BACKUP_DIR/regimetrader/market_data_$STAMP/" \
    "$LOCAL_ROOT/../regimetrader/market_data_$STAMP/"
else
  echo "  ⚠️  no market_data_$STAMP/ yet, skipping"
fi

echo ""
echo "✅ Backup complete: $BACKUP_DIR"
