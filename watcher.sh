#!/usr/bin/env bash
set -u

APP_DIR="${APP_DIR:-/opt/linuxdo-hunter}"
BRANCH="${BRANCH:-main}"
INTERVAL="${WATCH_INTERVAL:-45}"
LOCK="/var/run/linuxdo-hunter-watcher.lock"
VENV="$APP_DIR/.venv"
PYTHON="${PYTHON_BIN:-/usr/bin/python3}"
ENV_FILE="/etc/linuxdo-hunter.env"

exec 9>"$LOCK"
flock -n 9 || exit 0
cd "$APP_DIR" || exit 1

notify() {
    [ -f "$ENV_FILE" ] || return 0
    set -a; . "$ENV_FILE"; set +a
    [ -n "${TG_BOT_TOKEN:-}" ] || return 0
    [ -n "${TG_CHAT_ID:-}" ] || return 0
    curl -fsS --max-time 15 -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${TG_CHAT_ID}" --data-urlencode "parse_mode=HTML" \
      --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

install_unit() {
    local src="$1" dst="$2"
    [ -f "$src" ] || return 0
    install -m 644 "$src" "$dst" || return 1
}

install_service_units() {
    # Keep systemd definitions synchronized with GitHub on every watcher start/update.
    install_unit "$APP_DIR/linuxdo-hunter.service" "/etc/systemd/system/linuxdo-hunter.service" || return 1
    install_unit "$APP_DIR/linuxdo-hunter-checknow.service" "/etc/systemd/system/linuxdo-hunter-checknow.service" || return 1
    install_unit "$APP_DIR/linuxdo-hunter-watcher.service" "/etc/systemd/system/linuxdo-hunter-watcher.service" || return 1
    install_unit "$APP_DIR/linuxdo-hunter-llm-router.service" "/etc/systemd/system/linuxdo-hunter-llm-router.service" || return 1
    systemctl daemon-reload || return 1
    systemctl enable linuxdo-hunter.service linuxdo-hunter-checknow.service linuxdo-hunter-watcher.service >/dev/null 2>&1 || true
    if [ -f "$APP_DIR/llm_router.py" ]; then
        systemctl enable linuxdo-hunter-llm-router.service >/dev/null 2>&1 || true
    fi
}

install_router() {
    [ -f "$APP_DIR/llm_router.py" ] || return 0
    [ -x "$VENV/bin/python" ] || return 1
    install_service_units || return 1
    systemctl restart linuxdo-hunter-llm-router.service || return 1
    for _ in 1 2 3 4 5; do
        if curl -fsS --max-time 3 http://127.0.0.1:8099/health >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

ensure_runtime() {
    if [ ! -x "$VENV/bin/python" ]; then
        echo "[$(date -Is)] creating virtualenv"
        "$PYTHON" -m venv "$VENV" || { notify "❌ Hunter: не удалось создать Python venv."; return 1; }
    fi
    "$VENV/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    if [ -f "$APP_DIR/requirements.txt" ]; then
        "$VENV/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt" || {
            notify "⚠️ Hunter: ошибка установки зависимостей."; return 1;
        }
    fi
}

restart_app() {
    systemctl daemon-reload || return 1
    systemctl restart linuxdo-hunter.service linuxdo-hunter-checknow.service || return 1
}

update_repo() {
    git fetch origin "$BRANCH" --quiet || return 1
    LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo none)"
    REMOTE_SHA="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)"
    [ "$LOCAL_SHA" = "$REMOTE_SHA" ] && return 0

    REMOTE_VERSION="$(git show "origin/$BRANCH:version.txt" 2>/dev/null || echo '?')"
    LOCAL_VERSION="$(git show HEAD:version.txt 2>/dev/null || echo '?')"
    echo "[$(date -Is)] update $LOCAL_SHA -> $REMOTE_SHA (v$LOCAL_VERSION -> v$REMOTE_VERSION)"

    # prompt.txt is intentionally taken from GitHub too; local prompt edits are not preserved.
    git reset --hard "origin/$BRANCH" >/dev/null || {
        notify "❌ Hunter: не удалось применить v$REMOTE_VERSION."; return 1;
    }

    ensure_runtime || return 1
    install_service_units || return 1

    if [ -f "$APP_DIR/llm_router.py" ]; then
        if ! install_router; then
            notify "⚠️ Hunter: v$REMOTE_VERSION загружена, но LLM router не прошёл health-check." 
            # Do not restart the app against a known-dead router.
            return 1
        fi
    fi

    restart_app || {
        notify "⚠️ Hunter: v$REMOTE_VERSION загружена, но hunter/checknow не перезапустились."; return 1;
    }

    notify "✅ <b>Linux.do Hunter обновлён</b>\n\n📦 Версия: $LOCAL_VERSION → $REMOTE_VERSION\n🔄 Router + hunter + checknow перезапущены\n📝 prompt.txt взят из GitHub\n🔨 ${REMOTE_SHA:0:12}"
}

# Initial self-heal: repair units/runtime/router even without a new Git commit.
ensure_runtime || true
install_service_units || true
if [ -f "$APP_DIR/llm_router.py" ]; then
    install_router || notify "⚠️ Hunter: LLM router не прошёл стартовый health-check."
fi

while true; do
    update_repo || true
    sleep "$INTERVAL"
done
