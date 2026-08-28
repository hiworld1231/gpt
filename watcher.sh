#!/usr/bin/env bash
set -u

APP_DIR="${APP_DIR:-/opt/linuxdo-hunter}"
BRANCH="${BRANCH:-main}"
INTERVAL="${WATCH_INTERVAL:-45}"
LOCK="/var/run/linuxdo-hunter-watcher.lock"
SERVICES=("linuxdo-hunter.service" "linuxdo-hunter-checknow.service")

exec 9>"$LOCK"
flock -n 9 || exit 0

cd "$APP_DIR" || exit 1

notify() {
    [ -f /etc/linuxdo-hunter.env ] || return 0
    set -a
    # shellcheck disable=SC1091
    . /etc/linuxdo-hunter.env
    set +a
    [ -n "${TG_BOT_TOKEN:-}" ] || return 0
    [ -n "${TG_CHAT_ID:-}" ] || return 0
    local text="$1"
    curl -fsS --max-time 15 \
        -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT_ID}" \
        --data-urlencode "text=${text}" >/dev/null 2>&1 || true
}

update_repo() {
    if ! git fetch origin "$BRANCH" --quiet; then
        echo "[$(date -Is)] git fetch failed"
        notify "❌ Linux.do Hunter: не удалось получить обновления из GitHub."
        return 1
    fi

    LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo none)"
    REMOTE_SHA="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)"

    [ "$LOCAL_SHA" = "$REMOTE_SHA" ] && return 0

    REMOTE_VERSION="$(git show "origin/$BRANCH:version.txt" 2>/dev/null || echo '?')"
    LOCAL_VERSION="$(git show HEAD:version.txt 2>/dev/null || echo '?')"
    echo "[$(date -Is)] update $LOCAL_SHA -> $REMOTE_SHA (v$LOCAL_VERSION -> v$REMOTE_VERSION)"

    # GitHub is the source of truth, including prompt.txt.
    # Local tracked changes are intentionally discarded so the VPS always matches main.
    if ! git reset --hard "origin/$BRANCH" >/dev/null; then
        echo "[$(date -Is)] git reset failed"
        notify "❌ Linux.do Hunter: GitHub обновление не применилось (v$REMOTE_VERSION)."
        return 1
    fi

    git clean -fd >/dev/null 2>&1 || true

    if [ -f requirements.txt ]; then
        if ! .venv/bin/pip install -q -r requirements.txt; then
            echo "[$(date -Is)] pip install failed"
            notify "⚠️ Linux.do Hunter: код v$REMOTE_VERSION обновлён, но установка зависимостей завершилась ошибкой."
            return 1
        fi
    fi

    systemctl daemon-reload || true
    if ! systemctl restart "${SERVICES[@]}"; then
        echo "[$(date -Is)] service restart failed"
        notify "⚠️ Linux.do Hunter: v$REMOTE_VERSION загружена, но перезапуск сервисов завершился ошибкой."
        return 1
    fi

    echo "[$(date -Is)] hunter services restarted"
    notify "✅ Linux.do Hunter обновлён\n\n📦 Версия: $LOCAL_VERSION → $REMOTE_VERSION\n🔄 Перезапущены: hunter + checknow\n📝 prompt.txt взят из GitHub\n🔨 ${REMOTE_SHA:0:12}"
    return 0
}

while true; do
    update_repo || true
    sleep "$INTERVAL"
done
