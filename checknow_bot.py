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
CHECKNOW_LIMIT = max(1, min(int(os.getenv("CHECKNOW_LIMIT", "50")), 100))


def get_updates(offset=None):
    params = {"timeout": 25, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        params=params,
        timeout=35,
    )
    r.raise_for_status()
    return r.json().get("result", [])


def reply(text):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=20,
    ).raise_for_status()


async def get_recent_messages(limit=None):
    limit = limit or CHECKNOW_LIMIT
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH не настроены")
    if not TG_CHECKNOW_STRING_SESSION:
        raise RuntimeError("TG_CHECKNOW_STRING_SESSION не настроен")

    client = TelegramClient(
        StringSession(TG_CHECKNOW_STRING_SESSION),
        int(TG_API_ID),
        TG_API_HASH,
    )
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
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def analyze_without_db(title, text, url, published_at=None):
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY не настроен")

    original_url = extract_linuxdo_url(text)
    if original_url:
        original_title, original_text = fetch_linuxdo_thread(original_url)
        if original_text:
            title = original_title or title
            text = (
                f"[Telegram-пост]\n{text}\n\n"
                f"[ОРИГИНАЛЬНЫЙ LINUX.DO ТРЕД]\n{original_text}"
            )

    now = datetime.now(timezone.utc).isoformat()
    published = published_at or "неизвестно"
    base_prompt = read_prompt()
    prompt = f"""{base_prompt}

ТЕКУЩЕЕ ВРЕМЯ UTC: {now}
ДАТА ПУБЛИКАЦИИ ИСТОЧНИКА: {published}

Верни СТРОГО JSON без markdown и без дополнительного текста.
Обязательные поля:
- score (integer 0-100)
- is_new (boolean)
- is_working (boolean)
- category (короткий UPPER_SNAKE_CASE)
- summary (до 300 символов, русский)
- why (до 300 символов)
- how (до 700 символов; только то, что следует из исходного материала)
- risk (до 300 символов)

ВАЖНО ПРО ДАТЫ:
- Смотри на дату публикации и относительную свежесть информации.
- Не называй находку "новой" только потому, что пост свежий.
- is_new=true только если сама схема/сервис/лимит/раздача выглядит новой или содержит новые детали по сравнению с очевидно известным.
- Если материал старый или повторяет давнюю известную схему — is_new=false.
- Не выдумывай даты обновления, срок действия или актуальность, которых в материале нет.

Источник: {url}
Заголовок: {title}

ПОЛНЫЙ ДОСТУПНЫЙ МАТЕРИАЛ:
{text[:50000]}"""

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


async def check_now():
    messages = await get_recent_messages(CHECKNOW_LIMIT)
    reply(
        f"🔎 Проверяю последние <b>{len(messages)}</b> текстовых постов "
        f"@{html.escape(TG_SOURCE_CHANNEL)}...\n"
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
            result = await asyncio.to_thread(
                analyze_without_db,
                telegram_title(text),
                dated_text[:50000],
                url,
                published_at,
            )
            processed += 1
            if int(result.get("score", 0)) >= MIN_SCORE:
                reply(format_result(result, url, original_url))
                sent += 1
        except Exception as e:
            print(f"check item {message.id}: {e}", flush=True)

        # Avoid hammering Groq's token-per-minute limits during a manual 50-post scan.
        await asyncio.sleep(1.0)

    reply(
        f"✅ <b>/checknow завершён</b>\n"
        f"📊 Проанализировано: {processed}/{len(messages)}\n"
        f"🔥 Найдено подходящих: {sent}"
    )


def status():
    models = ", ".join(MODELS) if MODELS else "не настроены"
    key = "✅" if LLM_API_KEY else "❌"
    session = "✅ StringSession" if TG_CHECKNOW_STRING_SESSION else "❌ не настроена"
    reply(
        "🤖 <b>Linux.do Hunter</b>\n\n"
        f"📡 Источник: @{html.escape(TG_SOURCE_CHANNEL)}\n"
        f"🧠 Groq API: {key}\n"
        f"🧠 Модели: <code>{html.escape(models)}</code>\n"
        f"🎯 MIN_SCORE: {MIN_SCORE}\n"
        f"🔎 CHECKNOW_LIMIT: {CHECKNOW_LIMIT}\n"
        f"🔐 Checknow session: {session}\n"
        "💾 Checknow SQLite: НЕ используется\n"
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
