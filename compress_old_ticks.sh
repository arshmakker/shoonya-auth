#!/usr/bin/env bash
# compress_old_ticks.sh — gzip aged tick CSVs on the droplet.
#
# tick_persist.py writes one CSV per subscribed instrument per day into
# market_data_YYYYMMDD/raw_data/ticks/. Since MCX capture began (2026-08-28)
# that directory is ~115MB/day, against ~3MB/day for everything else combined
# — a 40x step change that puts the 24G disk on a ~6 month runway.
#
# Measured on the largest real file (SILVERMIC30NOV26_20260831.csv):
# 1169KB -> 227KB, 5x. That takes ~115MB/day to ~23MB/day and stretches the
# runway to roughly 2.5 years, without deleting anything.
#
# Nothing in either repo reads these files programmatically (tick_persist.py is
# the only toucher, and it only ever appends to *today's*). Ad-hoc analysis via
# pandas.read_csv() decompresses .gz transparently from the extension, so this
# is invisible to the research workflow.
#
# Safety:
#   - selects on the DATE IN THE DIRECTORY NAME, never mtime, so a file the
#     writer still holds open can't be picked up
#   - refuses to touch anything newer than MIN_AGE_DAYS (default 7)
#   - dry-run unless --apply is passed
#   - gzip -t verifies each archive before the original is replaced
set -euo pipefail

SSH_HOST="${SSH_HOST:-droplet}"
REMOTE_REGIME="/root/git/trading/regimetrader"
MIN_AGE_DAYS="${MIN_AGE_DAYS:-7}"
APPLY=0

usage() {
    echo "Usage: $0 [--apply] [--age-days N]"
    echo "  Default is a dry run. --apply performs the compression."
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --age-days) MIN_AGE_DAYS="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

if [ "$APPLY" = 1 ]; then
    echo "🗜  Compressing tick CSVs older than $MIN_AGE_DAYS days on $SSH_HOST"
else
    echo "🔎 DRY RUN — tick CSVs older than $MIN_AGE_DAYS days on $SSH_HOST (pass --apply to act)"
fi
echo ""

ssh "$SSH_HOST" "
set -euo pipefail
cd '$REMOTE_REGIME'

CUTOFF=\$(date -d '-$MIN_AGE_DAYS days' +%Y%m%d)
echo \"   cutoff: compressing days strictly older than \$CUTOFF\"
echo ''

total_before=0
total_after=0
files=0
dirs=0

for d in market_data_*/; do
    day=\${d#market_data_}
    day=\${day%/}
    # Directory names are market_data_YYYYMMDD; skip anything that isn't.
    case \"\$day\" in
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
        *) continue ;;
    esac
    [ \"\$day\" -lt \"\$CUTOFF\" ] || continue

    ticks=\"\${d}raw_data/ticks\"
    [ -d \"\$ticks\" ] || continue

    n=\$(find \"\$ticks\" -maxdepth 1 -name '*.csv' -type f | wc -l)
    [ \"\$n\" -gt 0 ] || continue
    dirs=\$((dirs+1))

    before=\$(du -sk \"\$ticks\" | cut -f1)
    total_before=\$((total_before+before))

    if [ '$APPLY' = 1 ]; then
        while IFS= read -r f; do
            # gzip writes f.gz then unlinks f only on success; -t re-verifies.
            gzip -q \"\$f\"
            gzip -t \"\$f.gz\"
            files=\$((files+1))
        done < <(find \"\$ticks\" -maxdepth 1 -name '*.csv' -type f)
        after=\$(du -sk \"\$ticks\" | cut -f1)
    else
        files=\$((files+n))
        after=\$((before/5))   # measured 5x ratio, for the dry-run estimate
    fi
    total_after=\$((total_after+after))

    printf '   %s  %s: %5dMB -> %4dMB  (%d files)\n' \\
        \"\$([ '$APPLY' = 1 ] && echo 'done' || echo 'est ')\" \\
        \"\$day\" \$((before/1024)) \$((after/1024)) \"\$n\"
done

echo ''
if [ \"\$dirs\" = 0 ]; then
    echo '   Nothing to do — no uncompressed tick CSVs older than the cutoff.'
else
    printf '   %d days, %d files: %dMB -> %dMB (saves %dMB)\n' \\
        \"\$dirs\" \"\$files\" \$((total_before/1024)) \$((total_after/1024)) \\
        \$(((total_before-total_after)/1024))
fi
echo ''
echo '   Disk after:'
df -h / | tail -1 | sed 's/^/     /'
"

echo ""
if [ "$APPLY" = 1 ]; then
    echo "✅ Done. Re-run any time; already-compressed days are skipped."
    echo "   To read one back:  zcat <file>.csv.gz | head"
    echo "   pandas reads .gz directly: pd.read_csv('<file>.csv.gz')"
else
    echo "ℹ️  Dry run only — nothing changed. Re-run with --apply to compress."
fi
