#!/usr/bin/env bash
# Refresh Shoonya symbol master files for regimetrader and flowTrader.
# Run on the 1st of each month before starting the trading session.

set -euo pipefail

TARGETS=(
    "/Users/arshdeep/git/regimetrader/symbols"
    "/Users/arshdeep/git/flowTrader/symbols"
    "/Users/arshdeep/git/bsensearb/symbols"
)

SEGMENTS=(NSE NFO BSE MCX)
BASE_URL="https://api.Shoonya.com"
TMPDIR_BASE=$(mktemp -d)
trap 'rm -rf "$TMPDIR_BASE"' EXIT

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "=== Shoonya symbol master refresh ==="

# Download and unzip each segment
for seg in "${SEGMENTS[@]}"; do
    url="${BASE_URL}/${seg}_symbols.txt.zip"
    zip_file="${TMPDIR_BASE}/${seg}.zip"
    log "Downloading $seg..."
    if ! curl -fsSL "$url" -o "$zip_file"; then
        log "ERROR: Failed to download $url — aborting"
        exit 1
    fi
    unzip -q "$zip_file" -d "$TMPDIR_BASE"
    # Shoonya zips contain e.g. NSE_symbols.txt — rename to NSE.csv
    txt_file="${TMPDIR_BASE}/${seg}_symbols.txt"
    if [[ ! -f "$txt_file" ]]; then
        log "ERROR: Expected $txt_file not found in zip — aborting"
        exit 1
    fi
    mv "$txt_file" "${TMPDIR_BASE}/${seg}.csv"
    log "  $seg: $(wc -l < "${TMPDIR_BASE}/${seg}.csv") rows"
done

# Copy into each project, backing up existing files
DATE_TAG=$(date '+%Y%m%d')
for target in "${TARGETS[@]}"; do
    if [[ ! -d "$target" ]]; then
        log "WARNING: $target not found — skipping"
        continue
    fi
    backup_dir="${target}/.backup_${DATE_TAG}"
    mkdir -p "$backup_dir"
    for seg in "${SEGMENTS[@]}"; do
        src="${TMPDIR_BASE}/${seg}.csv"
        dst="${target}/${seg}.csv"
        if [[ -f "$dst" ]]; then
            cp "$dst" "${backup_dir}/${seg}.csv"
        fi
        cp "$src" "$dst"
    done
    log "Updated $target (backup in .backup_${DATE_TAG})"
done

log "=== Symbol refresh complete ==="
