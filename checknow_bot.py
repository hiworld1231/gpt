#!/usr/bin/env python3
import asyncio
import html
import json
import os
import time
from datetime import datetime, timezone

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

from linuxdo_hunter import (
    telegram_url, telegram_title, TG_SOURCE_CHANNEL, TG_API_ID, TG_API_HASH,
    LLM_API_KEY, LLM_BASE, MODELS, MIN_SCORE, extract_linuxdo_url,
    fetch_linuxdo_thread, read_prompt,
)

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID = str(os.environ["TG_CHAT_ID"])
TG_CHECKNOW_STRING_SESSION = os.getenv("TG_CHECKNOW_STRING_SESSION", "").strip()
DEFAULT_LIMIT = max(1, min(int(os.getenv("CHECKNOW_LIMIT", "50")), 100))
SETTINGS_FILE = os.getenv("BOT_SETTINGS_FILE", "/var/lib/linuxdo-hunter/bot_settings.json")
STARTED_AT = time.time()

MODES = {
    "normal": "Обычный: баланс ценности, новизны и доказательств.",
    "aggressive": "Агрессивный: не пропускать редкие free-tier, раздачи, loophole и abuse-находки; всё сомнительное маркировать.",
    "giveaway": "Раздачи: максимум внимания акциям, promo, credits, free-tier и массовым выдачам.",
    "ai": "AI: приоритет AI API, модели, gateway, proxy, credits и дешёвому доступу.",
    "cloud": "Cloud: приоритет VPS, cloud credits, storage и developer-инфраструктуре.",
    "chaos": "Максимальная чувствительность: ловить даже странные/серые находки, но не выдумывать факты и не выдавать инструкции для незаконного доступа.",
}


def load_settings():
    defaults = {"score": MIN_SCORE, "limit": DEFAULT_LIMIT, "mode": "aggressive", "alerts": True}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        defaults.update({k: data[k] for k in defaults if k in data})
    except Exception:
        pass
    defaults["score"] = max(0, min(int(defaults["score"]), 100))
    defaults["limit"] = max(1, min(int(defaults["limit"]), 100))
    if defaults["mode"] not in MODES:
        defaults["mode"] = "aggressive"
    return defaults


def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def get_updates(offset=None):
    # Telegram expects allowed_updates as a JSON-serialized array.
    # Passing a Python list directly through requests creates repeated query
    # parameters and can result in callback_query updates being omitted.
    params = {
        "timeout": 25,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params=params, timeout=35)
    r.raise_for_status()
    return r.json().get("result", [])


def api_call(method, payload):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=25)
    r.raise_for_status()
    return r.json()


def send_message(text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api_call("sendMessage", payload)


def edit_message(message_id, text, reply_markup=None):
    payload = {"chat_id": CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api_call("editMessageText", payload)


def answer_callback(callback_id, text="", alert=False):
    return api_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": alert})


def kb(rows):
    return {"inline_keyboard": rows}


def main_menu():
    return kb([
        [{"text": "🔎 Проверить 50", "callback_data": "check:50"}, {"text": "⚡ Проверить 100", "callback_data": "check:100"}],
        [{"text": "🎯 Score", "callback_data": "menu:score"}, {"text": "🧠 Режим", "callback_data": "menu:mode"}],
        [{"text": "📊 Статус", "callback_data": "menu:status"}, {"text": "📝 Промпт", "callback_data": "menu:prompt"}],
        [{"text": "⚙️ Все настройки", "callback_data": "menu:settings"}, {"text": "❓ Помощь", "callback_data": "menu:help"}],
    ])


def score_menu():
    return kb([
        [{"text": "60", "callback_data": "score:60"}, {"text": "70", "callback_data": "score:70"}, {"text": "80", "callback_data": "score:80"}],
        [{"text": "90", "callback_data": "score:90"}, {"text": "95", "callback_data": "score:95"}],
        [{"text": "⬅️ Назад", "callback_data": "menu:settings"}],
    ])


def limit_menu():
    return kb([
        [{"text": "20", "callback_data": "limit:20"}, {"text": "50", "callback_data": "limit:50"}, {"text": "75", "callback_data": "limit:75"}],
        [{"text": "100", "callback_data": "limit:100"}],
        [{"text": "⬅️ Назад", "callback_data": "menu:settings"}],
    ])


def mode_menu():
    settings = load_settings()
    rows = []
    for name, description in MODES.items():
        rows.append([{"text": f"{'✅ ' if settings['mode'] == name else ''}{name}", "callback_data": f"mode:{name}"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "menu:main"}])
    return kb(rows)


def settings_menu():
    s = load_settings()
    alerts = "ON" if s.get("alerts", True) else "OFF"
    return kb([
        [{"text": f"🎯 Score: {s['score']}", "callback_data": "menu:score"}, {"text": f"🔎 Limit: {s['limit']}", "callback_data": "menu:limit"}],
        [{"text": f"🧠 Mode: {s['mode']}", "callback_data": "menu:mode"}],
        [{"text": f"🔔 Alerts: {alerts}", "callback_data": "alerts:toggle"}],
        [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}],
    ])


def status_text():
    s = load_settings()
    models = ", ".join(MODELS) if MODELS else "не настроены"
    key = "✅" if LLM_API_KEY else "❌"
    session = "✅" if TG_CHECKNOW_STRING_SESSION else "❌"
    uptime = int(time.time() - STARTED_AT)
    return (
        "🤖 <b>Linux.do Hunter</b>\n\n"
        f"📡 Источник: @{html.escape(TG_SOURCE_CHANNEL)}\n"
        f"🧠 Router: {key}\n"
        f"🧠 Модели: <code>{html.escape(models)}</code>\n"
        f"🎯 Score: <b>{s['score']}</b>\n"
        f"🔎 Check limit: <b>{s['limit']}</b>\n"
        f"🧠 Mode: <b>{html.escape(s['mode'])}</b>\n"
        f"🔔 Alerts: <b>{'ON' if s.get('alerts', True) else 'OFF'}</b>\n"
        f"🔐 Checknow session: {session}\n"
        "💾 Checknow SQLite: НЕ используется\n"
        f"⏱ Bot uptime: {uptime // 3600}ч {(uptime % 3600) // 60}м\n\n"
        "Кнопки ниже управляют Hunter без SSH."
    )


def help_text():
    return (
        "<b>🤖 Hunter — управление</b>\n\n"
        "/menu — панель управления\n"
        "/checknow — проверить последние посты\n"
        "/checknow 100 — проверить 100\n"
        "/score 80 — изменить порог\n"
        "/limit 50 — изменить размер проверки\n"
        "/mode aggressive — режим охоты\n"
        "/status — статус\n"
        "/prompt — показать первые строки prompt.txt\n\n"
        "Основное управление — кнопками, поэтому команды нужны редко."
    )


async def get_recent_messages(limit=None):
    limit = limit or load_settings()["limit"]
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH не настроены")
    if not TG_CHECKNOW_STRING_SESSION:
        raise RuntimeError("TG_CHECKNOW_STRING_SESSION не настроен")
    client = TelegramClient(StringSession(TG_CHECKNOW_STRING_SESSION), int(TG_API_ID), TG_API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("checknow Telegram session не авторизована")
        entity = await client.get_entity(TG_SOURCE_CHANNEL)
        messages = []
        async for message in client.iter_messages(entity, limit=limit):
            if (message.raw_text or "").strip():
                messages.append(message)
        return messages
    finally:
        await client.disconnect()


def call_model(model, prompt):
    r = requests.post(
        f"{LLM_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
        json={"model": model, "temperature": 0.1, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def analyze_without_db(title, text, url, published_at=None, mode=None):
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY не настроен")
    original_url = extract_linuxdo_url(text)
    if original_url:
        original_title, original_text = fetch_linuxdo_thread(original_url)
        if original_text:
            title = original_title or title
            text = f"[Telegram-пост]\n{text}\n\n[ОРИГИНАЛЬНЫЙ LINUX.DO ТРЕД]\n{original_text}"
    settings = load_settings()
    mode = mode or settings["mode"]
    mode_note = MODES.get(mode, MODES["aggressive"])
    now = datetime.now(timezone.utc).isoformat()
    published = published_at or "неизвестно"
    base_prompt = read_prompt()
    prompt = f"""{base_prompt}\n\nРЕЖИМ ОХОТЫ: {mode}\n{mode_note}\n\nТЕКУЩЕЕ ВРЕМЯ UTC: {now}\nДАТА ПУБЛИКАЦИИ ИСТОЧНИКА: {published}\n\nВерни СТРОГО JSON без markdown и без дополнительного текста.\nОбязательные поля: score (integer 0-100), is_new (boolean), is_working (boolean), category (UPPER_SNAKE_CASE), summary (до 300 символов, русский), why (до 300), how (до 700; только из материала), risk (до 300).\n\nИсточник: {url}\nЗаголовок: {title}\n\nПОЛНЫЙ ДОСТУПНЫЙ МАТЕРИАЛ:\n{text[:50000]}"""
    last_error = None
    for model in MODELS:
        try:
            return call_model(model, prompt)
        except RuntimeError as e:
            last_error = str(e)
            if last_error == "RATE_LIMIT":
                print(f"checknow: model {model} rate limited, trying next", flush=True)
                continue
            raise
    raise RuntimeError(f"all models unavailable: {last_error}")


def format_result(result, url, original_url=""):
    score = int(result.get("score", 0))
    tier = "S-TIER" if score >= 90 else "A-TIER"
    status = "🟢 подтверждено" if result.get("is_working") else "🟡 требует проверки"
    novelty = "🆕 новое" if result.get("is_new") else "♻️ уже известное"
    msg = (
        f"🔥 <b>{tier} — {score}/100</b> · {status} · {novelty}\n"
        f"🏷 {html.escape(str(result.get('category', 'OTHER')))}\n\n"
        f"💎 <b>{html.escape(str(result.get('summary', '')))}</b>\n\n"
        f"💰 <b>Почему жирно:</b> {html.escape(str(result.get('why', '')))}\n"
        f"🛠 <b>Как примерно повторить:</b> {html.escape(str(result.get('how', '')))}\n"
        f"⚠️ <b>Риск/лимиты:</b> {html.escape(str(result.get('risk', '')))}\n\n"
        f"🔗 {html.escape(url)}"
    )
    if original_url and original_url != url:
        msg += f"\n🔎 <b>Оригинал:</b> {html.escape(original_url)}"
    return msg


def result_keyboard(message_id):
    return kb([
        [{"text": "🔍 Deep Check", "callback_data": f"deep:{message_id}"}, {"text": "✅ Verify", "callback_data": f"verify:{message_id}"}],
        [{"text": "🔗 Открыть", "url": f"https://t.me/{TG_SOURCE_CHANNEL}/{message_id}"}, {"text": "⬅️ Меню", "callback_data": "menu:main"}],
    ])


async def check_now(limit=None):
    settings = load_settings()
    limit = limit or settings["limit"]
    messages = await get_recent_messages(limit)
    send_message(
        f"🔎 Проверяю последние <b>{len(messages)}</b> текстовых постов @{html.escape(TG_SOURCE_CHANNEL)}...\n"
        f"🎯 Score ≥ {settings['score']} · 🧠 режим: {html.escape(settings['mode'])}\n"
        "💾 SQLite-сессия Telethon НЕ используется"
    )
    sent = 0
    processed = 0
    for message in reversed(messages):
        text = (message.raw_text or "").strip()
        if not text:
            continue
        url = telegram_url(message)
        published_at = message.date.astimezone(timezone.utc).isoformat() if message.date else "неизвестно"
        dated_text = f"[Дата публикации Telegram: {published_at}]\n{text}"
        try:
            original_url = extract_linuxdo_url(text)
            result = await asyncio.to_thread(analyze_without_db, telegram_title(text), dated_text[:50000], url, published_at, settings["mode"])
            processed += 1
            if int(result.get("score", 0)) >= settings["score"]:
                send_message(format_result(result, url, original_url), result_keyboard(message.id))
                sent += 1
        except Exception as e:
            print(f"check item {message.id}: {e}", flush=True)
        await asyncio.sleep(1.0)
    send_message(f"✅ <b>/checknow завершён</b>\n📊 Проанализировано: {processed}/{len(messages)}\n🔥 Найдено подходящих: {sent}")


def parse_int_arg(raw, default):
    try:
        return int((raw.split()[1] if len(raw.split()) > 1 else "").strip())
    except Exception:
        return default


def show_menu_message(prefix=""):
    s = load_settings()
    text = (
        "🤖 <b>Linux.do Hunter</b>\n\n"
        "Выбирай действие кнопками ниже.\n\n"
        f"🎯 Score: <b>{s['score']}</b>\n"
        f"🔎 Limit: <b>{s['limit']}</b>\n"
        f"🧠 Mode: <b>{html.escape(s['mode'])}</b>\n"
        f"📡 Source: @{html.escape(TG_SOURCE_CHANNEL)}"
    )
    if prefix:
        text = prefix + "\n\n" + text
    send_message(text, main_menu())


def handle_callback(update):
    cq = update["callback_query"]
    data = cq.get("data", "")
    message = cq.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != CHAT_ID:
        return
    print(f"callback received: {data}", flush=True)
    try:
        answer_callback(cq["id"])
    except Exception as e:
        print(f"callback answer error: {e}", flush=True)
    message_id = message.get("message_id")
    try:
        if data == "menu:main":
            edit_message(message_id, "🤖 <b>Linux.do Hunter</b>\n\nВыбирай действие кнопками ниже.", main_menu())
        elif data == "menu:status":
            edit_message(message_id, status_text(), kb([[{"text": "⬅️ Главное меню", "callback_data": "menu:main"}]]))
        elif data == "menu:help":
            edit_message(message_id, help_text(), kb([[{"text": "⬅️ Главное меню", "callback_data": "menu:main"}]]))
        elif data == "menu:prompt":
            p = read_prompt()
            preview = p[:2500] + ("\n…" if len(p) > 2500 else "")
            edit_message(message_id, "📝 <b>Текущий prompt.txt</b>\n\n<pre>" + html.escape(preview) + "</pre>", kb([[{"text": "⬅️ Главное меню", "callback_data": "menu:main"}]]))
        elif data == "menu:settings":
            edit_message(message_id, "⚙️ <b>Настройки</b>\n\nВыбирай параметр:", settings_menu())
        elif data == "menu:score":
            edit_message(message_id, "🎯 <b>Минимальный Score</b>\n\nНиже — быстрый выбор.", score_menu())
        elif data == "menu:limit":
            edit_message(message_id, "🔎 <b>Сколько постов проверять</b>\n\nМожно выбрать 20 / 50 / 75 / 100.", limit_menu())
        elif data == "menu:mode":
            edit_message(message_id, "🧠 <b>Режим охоты</b>\n\nВыбери режим. Изменение применяется сразу к следующему анализу.", mode_menu())
        elif data.startswith("score:"):
            value = max(0, min(int(data.split(":", 1)[1]), 100))
            s = load_settings(); s["score"] = value; save_settings(s)
            edit_message(message_id, f"✅ Score установлен: <b>{value}</b>", settings_menu())
        elif data.startswith("limit:"):
            value = max(1, min(int(data.split(":", 1)[1]), 100))
            s = load_settings(); s["limit"] = value; save_settings(s)
            edit_message(message_id, f"✅ Лимит установлен: <b>{value}</b>", settings_menu())
        elif data.startswith("mode:"):
            mode = data.split(":", 1)[1]
            if mode in MODES:
                s = load_settings(); s["mode"] = mode; save_settings(s)
                edit_message(message_id, f"✅ Режим: <b>{html.escape(mode)}</b>\n\n{html.escape(MODES[mode])}", mode_menu())
        elif data == "alerts:toggle":
            s = load_settings(); s["alerts"] = not bool(s.get("alerts", True)); save_settings(s)
            edit_message(message_id, "⚙️ <b>Настройки</b>", settings_menu())
        elif data.startswith("check:"):
            limit = max(1, min(int(data.split(":", 1)[1]), 100))
            try:
                answer_callback(cq["id"], f"Запускаю проверку {limit} постов")
            except Exception:
                pass
            asyncio.run(check_now(limit))
        else:
            print(f"unknown callback: {data}", flush=True)
    except Exception as e:
        print(f"callback {data}: {e}", flush=True)
        try:
            answer_callback(cq["id"], "Ошибка: смотри журнал бота", alert=True)
        except Exception:
            pass


def main():
    offset = None
    # Make polling mode deterministic. This is harmless when no webhook is configured.
    try:
        api_call("deleteWebhook", {"drop_pending_updates": False})
    except Exception as e:
        print(f"deleteWebhook: {e}", flush=True)
    print("Telegram command bot active: /menu /checknow /status /settings", flush=True)
    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                if update.get("callback_query"):
                    try:
                        handle_callback(update)
                    except Exception as e:
                        print(f"callback handler: {e}", flush=True)
                    continue
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                if str(chat.get("id")) != CHAT_ID:
                    continue
                raw = (message.get("text") or "").strip()
                command = raw.split()[0].lower() if raw else ""
                if command == "/menu":
                    show_menu_message()
                elif command in ("/checknow", "/check"):
                    try:
                        value = parse_int_arg(raw, load_settings()["limit"])
                        asyncio.run(check_now(max(1, min(value, 100))))
                    except Exception as e:
                        print(f"checknow: {e}", flush=True)
                        send_message(f"❌ /checknow: <code>{html.escape(str(e)[:700])}</code>")
                elif command == "/score":
                    value = parse_int_arg(raw, load_settings()["score"])
                    if len(raw.split()) > 1:
                        s = load_settings(); s["score"] = max(0, min(value, 100)); save_settings(s)
                        send_message(f"✅ Score: <b>{s['score']}</b>", main_menu())
                    else:
                        send_message("🎯 <b>Score</b>", score_menu())
                elif command == "/limit":
                    value = parse_int_arg(raw, load_settings()["limit"])
                    if len(raw.split()) > 1:
                        s = load_settings(); s["limit"] = max(1, min(value, 100)); save_settings(s)
                        send_message(f"✅ Limit: <b>{s['limit']}</b>", main_menu())
                    else:
                        send_message("🔎 <b>Лимит</b>", limit_menu())
                elif command == "/mode":
                    parts = raw.split()
                    if len(parts) > 1 and parts[1] in MODES:
                        s = load_settings(); s["mode"] = parts[1]; save_settings(s)
                        send_message(f"✅ Mode: <b>{parts[1]}</b>", main_menu())
                    else:
                        send_message("🧠 <b>Режим</b>", mode_menu())
                elif command == "/status":
                    send_message(status_text(), main_menu())
                elif command == "/settings":
                    send_message("⚙️ <b>Настройки</b>", settings_menu())
                elif command == "/prompt":
                    p = read_prompt(); preview = p[:2500] + ("\n…" if len(p) > 2500 else "")
                    send_message("📝 <b>prompt.txt</b>\n\n<pre>" + html.escape(preview) + "</pre>", main_menu())
                elif command in ("/help", "/start"):
                    show_menu_message(help_text())
        except Exception as e:
            print(f"command bot: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
