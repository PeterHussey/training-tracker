#!/bin/sh
# Install (or reinstall) the training-tracker launchd agents.
# Substitutes __REPO__ with this checkout's path, copies the plists into
# ~/Library/LaunchAgents and loads them.
set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$REPO/logs"
for name in dashboard refresh; do
    sed "s|__REPO__|$REPO|g" "$REPO/deploy/launchd/com.trainingtracker.$name.plist" \
        > "$AGENTS/com.trainingtracker.$name.plist"
    launchctl bootout "gui/$(id -u)/com.trainingtracker.$name" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.trainingtracker.$name.plist"
done
launchctl list | grep trainingtracker
