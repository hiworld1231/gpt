#!/usr/bin/env bash
set -u

APP_DIR="${APP_DIR:-/opt/linuxdo-hunter}"
BRANCH="${BRANCH:-main}"
INTERVAL="${WATCH_INTERVAL:-45}"
VERSION_FILE="version.txt"
LOCK="/var/run/linuxdo-hunter-watcher.lock"
SERVICES=("linuxdo-hunter.service" "linuxdo-hunter-checknow.service")

exec 9>"$LOCK"
flock -n 9 || exit 0

cd "$APP_DIR" || exit 1

restore_prompt=()

update_repo() {
    if ! git fetch origin "$BRANCH" --quiet; then
        echo "[$(date -Is)] git fetch failed"
        return 1
    fi

    LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo none)"
    REMOTE_SHA="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)"

    if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
        return 0
    fi

    echo "[$(date -Is)] update $LOCAL_SHA -> $REMOTE_SHA"

    # Keep the user's private local prompt and restore it after pulling code.
    PROMPT_BACKUP=""
    if [ -f prompt.txt ]; then
        PROMPT_BACKUP="$(mktemp)"
        cp -f prompt.txt "$PROMPT_BACKUP"
    fi

    # Save unrelated local changes so code updates can never get stuck on them.
    STASH_CREATED=0
    if ! git diff --quiet -- prompt.txt; then
        git stash push -m "watcher-local-prompt" -- prompt.txt >/dev/null 2>&1 || true
        STASH_CREATED=1
    fi

    # watcher.sh itself and any other tracked local changes must not block updates.
    git reset --hard "origin/$BRANCH" >/dev/null
    git clean -fd >/dev/null

    if [ -n "$PROMPT_BACKUP" ] && [ -f "$PROMPT_BACKUP" ]; then
        cp -f "$PROMPT_BACKUP" prompt.txt
        rm -f "$PROMPT_BACKUP"
    elif [ "$STASH_CREATED" -eq 1 ]; then
        git checkout -- prompt.txt >/dev/null 2>&1 || true
        git stash pop --index >/dev/null 2>&1 || true
    fi

    if [ -f requirements.txt ]; then
        .venv/bin/pip install -q -r requirements.txt || echo "[$(date -Is)] pip install had errors"
    fi

    systemctl daemon-reload || true

    # Restart every Hunter component after every code update.
    systemctl restart "${SERVICES[@]}"
    echo "[$(date -Is)] hunter services restarted"

    return 0
}

while true; do
    update_repo || true
    sleep "$INTERVAL"
done
