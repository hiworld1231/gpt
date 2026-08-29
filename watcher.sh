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
    set -a; . /etc/linuxdo-hunter.env; set +a
    [ -n "${TG_BOT_TOKEN:-}" ] || return 0
    [ -n "${TG_CHAT_ID:-}" ] || return 0
    curl -fsS --max-time 15 -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" -d "chat_id=${TG_CHAT_ID}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

install_router_unit() {
    local src="$APP_DIR/linuxdo-hunter-llm-router.service"
    local dst="/etc/systemd/system/linuxdo-hunter-llm-router.service"
    if [ -f "$src" ]; then
        install -m 644 "$src" "$dst" || return 1
    elif [ ! -f "$dst" ]; then
        cat >"$dst" <<'EOF'
[Unit]
Description=Linux.do Hunter multi-provider LLM router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/linuxdo-hunter
EnvironmentFile=/etc/linuxdo-hunter.env
ExecStart=/opt/linuxdo-hunter/.venv/bin/python /opt/linuxdo-hunter/llm_router.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    fi
    systemctl daemon-reload || return 1
    systemctl enable linuxdo-hunter-llm-router.service >/dev/null 2>&1 || true
    systemctl restart linuxdo-hunter-llm-router.service || return 1
}

ensure_runtime() {
    if [ ! -x "$VENV/bin/python" ]; then
        echo "[$(date -Is)] creating virtualenv"
        "$PYTHON" -m venv "$VENV" || { notify "❌ Hunter: не удалось создать Python venv."; return 1; }
    fi
    "$VENV/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    if [ -f "$APP_DIR/requirements.txt" ]; then
        "$VENV/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt" || { notify "⚠️ Hunter: ошибка установки зависимостей."; return 1; }
    fi
}

update_repo() {
    git fetch origin "$BRANCH" --quiet || return 1
    LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo none)"
    REMOTE_SHA="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)"
    [ "$LOCAL_SHA" = "$REMOTE_SHA" ] && return 0

    REMOTE_VERSION="$(git show "origin/$BRANCH:version.txt" 2>/dev/null || echo '?')"
    LOCAL_VERSION="$(git show HEAD:version.txt 2>/dev/null || echo '?')"
    echo "[$(date -Is)] update $LOCAL_SHA -> $REMOTE_SHA (v$LOCAL_VERSION -> v$REMOTE_VERSION)"

    git reset --hard "origin/$BRANCH" >/dev/null || { notify "❌ Hunter: не удалось применить v$REMOTE_VERSION."; return 1; }
    ensure_runtime || return 1

    # Always install/reload the router unit from the current GitHub checkout.
    install_router_unit || { notify "⚠️ Hunter: router v$REMOTE_VERSION не запустился."; return 1; }
    systemctl daemon-reload || true
    systemctl restart linuxdo-hunter.service linuxdo-hunter-checknow.service || {
        notify "⚠️ Hunter: v$REMOTE_VERSION загружена, но hunter/checknow не перезапустились."; return 1;
    }

    notify "✅ <b>Linux.do Hunter обновлён</b>\n\n📦 Версия: $LOCAL_VERSION → $REMOTE_VERSION\n🔄 Router + hunter + checknow перезапущены\n📝 prompt.txt взят из GitHub\n🔨 ${REMOTE_SHA:0:12}"
}

ensure_runtime || true
# Also repair the router immediately, even if there was no new Git commit.
install_router_unit || true
while true; do
    update_repo || true
    sleep "$INTERVAL"
done
