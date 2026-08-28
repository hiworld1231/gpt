#!/usr/bin/env bash
set -u

APP_DIR="${APP_DIR:-/opt/linuxdo-hunter}"
BRANCH="${BRANCH:-main}"
INTERVAL="${WATCH_INTERVAL:-45}"
LOCK="/var/run/linuxdo-hunter-watcher.lock"
SERVICES=("linuxdo-hunter.service" "linuxdo-hunter-checknow.service")
VENV="$APP_DIR/.venv"
PYTHON="${PYTHON_BIN:-/usr/bin/python3}"

exec 9>"$LOCK"
flock -n 9 || exit 0
cd "$APP_DIR" || exit 1

notify() {
    [ -f /etc/linuxdo-hunter.env ] || return 0
    set -a
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

ensure_runtime() {
    if [ ! -x "$VENV/bin/python" ]; then
        echo "[$(date -Is)] creating virtualenv"
        if ! "$PYTHON" -m venv "$VENV"; then
            echo "[$(date -Is)] venv creation failed"
            notify "❌ <b>Hunter</b>: не удалось создать Python venv на VPS."
            return 1
        fi
    fi
    if [ -f "$APP_DIR/requirements.txt" ]; then
        if ! "$VENV/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"; then
            echo "[$(date -Is)] pip install failed"
            notify "⚠️ <b>Hunter</b>: зависимости не установились полностью."
            return 1
        fi
    fi
    return 0
}

update_repo() {
    if ! git fetch origin "$BRANCH" --quiet; then
        echo "[$(date -Is)] git fetch failed"
        return 1
    fi

    LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo none)"
    REMOTE_SHA="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)"
    [ "$LOCAL_SHA" = "$REMOTE_SHA" ] && return 0

    REMOTE_VERSION="$(git show "origin/$BRANCH:version.txt" 2>/dev/null || echo '?')"
    LOCAL_VERSION="$(git show HEAD:version.txt 2>/dev/null || echo '?')"
    echo "[$(date -Is)] update $LOCAL_SHA -> $REMOTE_SHA (v$LOCAL_VERSION -> v$REMOTE_VERSION)"

    if ! git reset --hard "origin/$BRANCH" >/dev/null; then
        notify "❌ <b>Linux.do Hunter</b>: не удалось применить обновление v$REMOTE_VERSION."
        return 1
    fi
    git clean -fd >/dev/null 2>&1 || true

    ensure_runtime || return 1

    systemctl daemon-reload || true
    if ! systemctl restart "${SERVICES[@]}"; then
        notify "⚠️ <b>Linux.do Hunter</b>: v$REMOTE_VERSION загружена, но сервисы не удалось перезапустить."
        return 1
    fi

    notify "✅ <b>Linux.do Hunter обновлён</b>\n\n📦 Версия: $LOCAL_VERSION → $REMOTE_VERSION\n🔄 Перезапущены: hunter + checknow\n📝 prompt.txt взят из GitHub\n🔨 ${REMOTE_SHA:0:12}"
    return 0
}

ensure_runtime || true

while true; do
    update_repo || true
    sleep "$INTERVAL"
done
