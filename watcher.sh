#!/usr/bin/env bash
set -u

APP_DIR="${APP_DIR:-/opt/linuxdo-hunter}"
BRANCH="${BRANCH:-main}"
INTERVAL="${WATCH_INTERVAL:-60}"
VERSION_FILE="version.txt"
SERVICE="${SERVICE:-linuxdo-hunter.service}"
LOCK="/var/run/linuxdo-hunter-watcher.lock"

exec 9>"$LOCK"
flock -n 9 || exit 0

cd "$APP_DIR" || exit 1

while true; do
    if ! git fetch origin "$BRANCH" --quiet; then
        sleep "$INTERVAL"
        continue
    fi

    LOCAL_VERSION="$(cat "$VERSION_FILE" 2>/dev/null || echo 0)"
    REMOTE_VERSION="$(git show "origin/$BRANCH:$VERSION_FILE" 2>/dev/null || echo 0)"

    if [ "$LOCAL_VERSION" != "$REMOTE_VERSION" ]; then
        echo "[$(date -Is)] update $LOCAL_VERSION -> $REMOTE_VERSION"

        if git pull --ff-only origin "$BRANCH"; then
            if [ -f requirements.txt ]; then
                .venv/bin/pip install -q -r requirements.txt || true
            fi

            systemctl restart "$SERVICE"
            echo "[$(date -Is)] service restarted"
        else
            echo "[$(date -Is)] git pull failed"
        fi
    fi

    sleep "$INTERVAL"
done
