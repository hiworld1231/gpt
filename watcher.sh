#!/usr/bin/env bash
set -u

APP_DIR="${APP_DIR:-/opt/linuxdo-hunter}"
BRANCH="${BRANCH:-main}"
INTERVAL="${WATCH_INTERVAL:-45}"
LOCK="/var/run/linuxdo-hunter-watcher.lock"
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
    curl -fsS --max-time 15 -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" -d "chat_id=${TG_CHAT_ID}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

ensure_runtime() {
    if [ ! -x "$VENV/bin/python" ]; then
        echo "[$(date -Is)] creating virtualenv"
        "$PYTHON" -m venv "$VENV" || { notify "❌ Hunter: не удалось создать Python venv."; return 1; }
    fi
    if [ -f "$APP_DIR/requirements.txt" ]; then
        "$VENV/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt" || { notify "⚠️ Hunter: ошибка установки зависимостей."; return 1; }
    fi
}

update_repo() {
    if ! git fetch origin "$BRANCH" --quiet; then return 1; fi
    LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo none)"
    REMOTE_SHA="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)"
    [ "$LOCAL_SHA" = "$REMOTE_SHA" ] && return 0

    REMOTE_VERSION="$(git show "origin/$BRANCH:version.txt" 2>/dev/null || echo '?')"
    LOCAL_VERSION="$(git show HEAD:version.txt 2>/dev/null || echo '?')"
    echo "[$(date -Is)] update $LOCAL_SHA -> $REMOTE_SHA (v$LOCAL_VERSION -> v$REMOTE_VERSION)"

    # GitHub is the source of truth. Never delete the venv or runtime state.
    git reset --hard "origin/$BRANCH" >/dev/null || { notify "❌ Hunter: не удалось применить v$REMOTE_VERSION."; return 1; }

    ensure_runtime || return 1
    systemctl daemon-reload || true

    # Router first, then both consumers. This makes provider failover available before requests start.
    if systemctl list-unit-files | grep -q '^linuxdo-hunter-llm-router.service'; then
        systemctl restart linuxdo-hunter-llm-router.service || true
    fi
    systemctl restart linuxdo-hunter.service linuxdo-hunter-checknow.service || {
        notify "⚠️ Hunter: v$REMOTE_VERSION загружена, но один из сервисов не перезапустился."; return 1;
    }

    notify "✅ <b>Linux.do Hunter обновлён</b>\n\n📦 Версия: $LOCAL_VERSION → $REMOTE_VERSION\n🔄 Router + hunter + checknow перезапущены\n📝 prompt.txt взят из GitHub\n🔨 ${REMOTE_SHA:0:12}"
}

ensure_runtime || true
while true; do
    update_repo || true
    sleep "$INTERVAL"
done
