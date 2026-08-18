#!/usr/bin/env bash
# vps_power.sh — power the DO droplet on/off/status via doctl
# Usage: ./vps_power.sh {on|off|status}

set -euo pipefail

DROPLET_NAME="trading-vps"

usage() {
    echo "Usage: $0 {on|off|status}"
    exit 1
}

[ $# -eq 1 ] || usage

droplet_id() {
    doctl compute droplet list --format ID,Name --no-header \
        | awk -v name="$DROPLET_NAME" '$2 == name {print $1}'
}

ID=$(droplet_id)
if [ -z "$ID" ]; then
    echo "❌ Droplet '$DROPLET_NAME' not found."
    exit 1
fi

case "$1" in
    on)
        STATUS=$(doctl compute droplet get "$ID" --format Status --no-header)
        if [ "$STATUS" = "active" ]; then
            echo "✅ Droplet already active — nothing to do."
        else
            echo "🔌 Powering on $DROPLET_NAME ($ID)..."
            doctl compute droplet-action power-on "$ID" --wait
            echo "✅ Droplet is on."
            echo "⏳ Waiting for SSH to come up..."
            for _ in $(seq 1 30); do
                if ssh -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new droplet true 2>/dev/null; then
                    echo "✅ SSH is ready."
                    break
                fi
                sleep 5
            done
        fi
        ;;
    off)
        STATUS=$(doctl compute droplet get "$ID" --format Status --no-header)
        if [ "$STATUS" != "active" ]; then
            echo "⚠️  Droplet is already '$STATUS' — skipping backup (nothing reachable to pull)."
        else
            "$(dirname "${BASH_SOURCE[0]}")/vps_backup.sh"
        fi
        echo ""
        echo "🔌 Powering off $DROPLET_NAME ($ID)..."
        doctl compute droplet-action power-off "$ID" --wait
        echo "✅ Droplet is off."
        ;;
    status)
        doctl compute droplet get "$ID" --format ID,Name,Status,PublicIPv4
        ;;
    *)
        usage
        ;;
esac
