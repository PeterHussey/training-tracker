#!/bin/sh
# Install (or reinstall) the training-tracker launchd agents.
# Substitutes __REPO__ with this checkout's path, copies the plists into
# ~/Library/LaunchAgents and loads them.
set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$REPO/logs"
for name in dashboard refresh health; do
    sed "s|__REPO__|$REPO|g" "$REPO/deploy/launchd/com.trainingtracker.$name.plist" \
        > "$AGENTS/com.trainingtracker.$name.plist"
    launchctl bootout "gui/$(id -u)/com.trainingtracker.$name" 2>/dev/null || true
    # bootout is asynchronous: bootstrapping straight after it fails with
    # "Bootstrap failed: 5" while the label is still held. Retry briefly.
    loaded=0
    for _ in 1 2 3 4 5; do
        if launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.trainingtracker.$name.plist" 2>/dev/null; then
            loaded=1
            break
        fi
        sleep 1
    done
    [ "$loaded" = 1 ] || { echo "failed to load com.trainingtracker.$name" >&2; exit 1; }
done
launchctl list | grep trainingtracker
