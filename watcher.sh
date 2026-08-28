#!/usr/bin/env bash
set -u

APP_DIR="${APP_DIR:-/opt/linuxdo-hunter}"
BRANCH="${BRANCH:-main}"
INTERVAL="${WATCH_INTERVAL:-45}"
LOCK="/var/run/linuxdo-hunter-watcher.lock"
ENV_FILE="/etc/linuxdo-hunter.env"
SERVICES=("linuxdo-hunter.service" "linuxdo-hunter-checknow.service")

exec 9>"$LOCK"
flock -n 9 || exit 0

cd "$APP_DIR" || exit 1

if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

notify_telegram() {
    local text="$1"
    if [ -z "${TG_BOT_TOKEN:-}" ] || [ -z "${TG_CHAT_ID:-}" ]; then
        return 0
    fi
    curl -fsS --max-time 15 \
        -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "parse_mode=HTML" >/dev/null 2>&1 || true
}

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

    OLD_VERSION="$(cat version.txt 2>/dev/null || echo unknown)"
    echo "[$(date -Is)] update $LOCAL_SHA -> $REMOTE_SHA"

    PROMPT_BACKUP=""
    if [ -f prompt.txt ]; then
        PROMPT_BACKUP="$(mktemp)"
        cp -f prompt.txt "$PROMPT_BACKUP"
    fi

    git reset --hard "origin/$BRANCH" >/dev/null
    git clean -fd >/dev/null

    if [ -n "$PROMPT_BACKUP" ] && [ -f "$PROMPT_BACKUP" ]; then
        cp -f "$PROMPT_BACKUP" prompt.txt
        rm -f "$PROMPT_BACKUP"
    fi

    if [ -f requirements.txt ]; then
        .venv/bin/pip install -q -r requirements.txt || echo "[$(date -Is)] pip install had errors"
    fi

    systemctl daemon-reload || true

    if ! systemctl restart "${SERVICES[@]}"; then
        notify_telegram "❌ <b>Linux.do Hunter: update error</b>%0A%0A📦 Версия: $OLD_VERSION → $(cat version.txt 2>/dev/null || echo '?')%0A🔧 Не удалось полностью перезапустить сервисы."
        echo "[$(date -Is)] service restart failed"
        return 1
    fi

    NEW_VERSION="$(cat version.txt 2>/dev/null || echo unknown)"
    SHORT_SHA="${REMOTE_SHA:0:7}"
    notify_telegram "✅ <b>Linux.do Hunter обновлён</b>%0A%0A📦 Версия: <b>$OLD_VERSION → $NEW_VERSION</b>%0A🔨 Commit: <code>$SHORT_SHA</code>%0A🔄 Перезапущены: hunter + checknow%0A📝 prompt.txt сохранён.%0A🔗 https://github.com/hiworld1231/gpt/commit/${REMOTE_SHA}"
    echo "[$(date -Is)] hunter services restarted"
    return 0
}

while true; do
    update_repo || true
    sleep "$INTERVAL"
done
