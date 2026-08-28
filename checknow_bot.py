#!/usr/bin/env python3
import asyncio
import html
import os
import time

import requests
from telethon import TelegramClient

from linuxdo_hunter import (
    llm,
    telegram_url,
    telegram_title,
    TG_SOURCE_CHANNEL,
    TG_API_ID,
    TG_API_HASH,
    TG_SESSION,
    LLM_API_KEY,
    MIN_SCORE,
    extract_linuxdo_url,
    fetch_linuxdo_thread,
)

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID = str(os.environ["TG_CHAT_ID"])


def get_updates(offset=None):
    params = {"timeout": 25, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params=params, timeout=35)
    r.raise_for_status()
    return r.json().get("result", [])


def reply(text):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=20,
    ).raise_for_status()


async def get_recent_messages(limit=15):
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH не настроены")
    client = TelegramClient(TG_SESSION, int(TG_API_ID), TG_API_HASH)
    await client.start()
    try:
        entity = await client.get_entity(TG_SOURCE_CHANNEL)
        messages = []
        async for message in client.iter_messages(entity, limit=limit):
            if (message.raw_text or "").strip():
                messages.append(message)
        return messages
    finally:
        await client.disconnect()


def analyze_without_db(title, text, url):
    """Analyze a forced /checknow item without opening the shared SQLite DB."""
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY не настроен")

    original_url = extract_linuxdo_url(text)
    if original_url:
        original_title, original_text = fetch_linuxdo_thread(original_url)
        if original_text:
            title = original_title or title
            text = f"[Telegram-пост]\n{text}\n\n[ОРИГИНАЛЬНЫЙ LINUX.DO ТРЕД]\n{original_text}"

    # llm() accepts con=None; it must not use SQLite when called this way.
    result = llm(None, title, text, url)
    score = int(result.get("score", 0))
    if score < MIN_SCORE:
        return False

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
    reply(msg)
    return True


async def check_now():
    messages = await get_recent_messages(15)
    reply(f"🔎 Проверяю последние {len(messages)} постов @{TG_SOURCE_CHANNEL}...")
    sent = 0
    for message in reversed(messages):
        text = (message.raw_text or "").strip()
        if not text:
            continue
        try:
            if await asyncio.to_thread(analyze_without_db, telegram_title(text), text[:50000], telegram_url(message)):
                sent += 1
        except Exception as e:
            print(f"check item {message.id}: {e}", flush=True)
    reply(f"✅ <b>/checknow завершён</b>\n🔥 Найдено подходящих: {sent}")


def status():
    models = os.getenv("LLM_MODELS", "") or os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
    key = "✅" if LLM_API_KEY else "❌"
    reply(
        "🤖 <b>Linux.do Hunter</b>\n\n"
        f"📡 Источник: @{TG_SOURCE_CHANNEL}\n"
        f"🧠 Groq API: {key}\n"
        f"🧠 Модели: <code>{html.escape(models)}</code>\n"
        f"🎯 MIN_SCORE: {MIN_SCORE}\n"
        "🗃 /checknow: отдельная БД не используется\n"
        "⚡ Команды: /checknow /status"
    )


def main():
    offset = None
    print("Telegram command bot active: /checknow /status", flush=True)
    while True:
        try:
            for update in get_updates(offset):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                raw = (message.get("text") or "").strip()
                command = raw.split()[0].lower() if raw else ""
                if str(chat.get("id")) != CHAT_ID:
                    continue
                if command in ("/checknow", "/check"):
                    try:
                        asyncio.run(check_now())
                    except Exception as e:
                        print(f"checknow: {e}", flush=True)
                        reply(f"❌ /checknow: <code>{html.escape(str(e)[:700])}</code>")
                elif command == "/status":
                    status()
        except Exception as e:
            print(f"command bot: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
