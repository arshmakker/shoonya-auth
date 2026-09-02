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
# The trading day being archived, not necessarily today. eod_housekeeping.sh
# runs at 00:20 — after the proxy's 23:58 shutdown, so already the NEXT
# calendar day — and passes the day that actually just ended. Without the
# override the archive would be named for a day with no data in it, and the
# market_data_$STAMP merge below would find nothing and silently skip.
STAMP="${BACKUP_STAMP:-$(date +%Y%m%d)}"
BACKUP_DIR="$LOCAL_ROOT/droplet_backup_$STAMP"

# Hardlink unchanged files against the most recent previous backup instead of
# re-copying them. Each run pulls the FULL market_data_* history, so without
# this every backup carried another complete copy — fine at ~3MB/day, but tick
# persistence took that to ~115MB/day (2026-08-31), so the archive was set to
# grow quadratically. With --link-dest an unchanged file costs one inode and no
# blocks; only genuinely new/changed data consumes space. Each backup dir still
# reads as a complete standalone snapshot.
PREV_BACKUP=""
for d in $(ls -d "$LOCAL_ROOT"/droplet_backup_* 2>/dev/null | sort -r); do
    [ "$d" = "$BACKUP_DIR" ] && continue   # a re-run on the same day
    [ -d "$d" ] || continue
    PREV_BACKUP="$d"
    break
done

LINK_REGIME=()
LINK_AUTH=()
if [ -n "$PREV_BACKUP" ]; then
    echo "🔗 Hardlinking unchanged files against $(basename "$PREV_BACKUP")"
    [ -d "$PREV_BACKUP/regimetrader" ] && LINK_REGIME=(--link-dest="$PREV_BACKUP/regimetrader")
    [ -d "$PREV_BACKUP/shoonya-auth" ] && LINK_AUTH=(--link-dest="$PREV_BACKUP/shoonya-auth")
fi

echo "📦 Pulling droplet logs/data → $BACKUP_DIR"
mkdir -p "$BACKUP_DIR/regimetrader" "$BACKUP_DIR/shoonya-auth"

echo "  regimetrader/{logs,data,market_data_*,option_snapshots,docs,ledger_summary.csv}"
rsync -avz --exclude='venv/' --exclude='symbols/' "${LINK_REGIME[@]}" \
  "$SSH_HOST:$REMOTE_REGIME/logs" \
  "$SSH_HOST:$REMOTE_REGIME/data" \
  "$SSH_HOST:$REMOTE_REGIME/option_snapshots" \
  "$SSH_HOST:$REMOTE_REGIME/docs" \
  "$SSH_HOST:$REMOTE_REGIME/ledger_summary.csv" \
  "$BACKUP_DIR/regimetrader/" 2>&1 || echo "  ⚠️  some regimetrader paths missing, continuing"

rsync -avz --include='market_data_*/***' --exclude='*' "${LINK_REGIME[@]}" \
  "$SSH_HOST:$REMOTE_REGIME/" "$BACKUP_DIR/regimetrader/" \
  2>&1 || echo "  ⚠️  market_data_*/ pull failed, continuing"

echo "  shoonya-auth/{order_debug.log,.claude,CLAUDE.md,start_vps.sh}"
rsync -avz "${LINK_AUTH[@]}" \
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
echo "   Size as a standalone snapshot: $(du -sh "$BACKUP_DIR" | cut -f1)"
if [ -n "$PREV_BACKUP" ]; then
    # Count files carried over as hardlinks rather than trying to compute a
    # byte delta — du's hardlink accounting differs between GNU and BSD/macOS
    # and silently gives nonsense when the two dirs are measured separately.
    LINKED=$(find "$BACKUP_DIR" -type f -links +1 2>/dev/null | wc -l | tr -d ' ')
    TOTAL=$(find "$BACKUP_DIR" -type f 2>/dev/null | wc -l | tr -d ' ')
    echo "   $LINKED of $TOTAL files are hardlinks shared with $(basename "$PREV_BACKUP") (no extra disk)"
fi
