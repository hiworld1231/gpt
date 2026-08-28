#!/usr/bin/env python3
import asyncio
import os
import sqlite3
import time

import requests
from telethon import TelegramClient

from linuxdo_hunter import (
    send_result,
    telegram_url,
    telegram_title,
    TG_SOURCE_CHANNEL,
    TG_API_ID,
    TG_API_HASH,
    TG_SESSION,
    DB,
    MODELS,
    LLM_API_KEY,
    MIN_SCORE,
)

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID = str(os.environ["TG_CHAT_ID"])


def open_db():
    # IMPORTANT: do not execute PRAGMA journal_mode=WAL here.
    # Changing the journal mode requires an exclusive SQLite lock and
    # races with the main Hunter process. This was the source of the
    # /checknow "database is locked" error.
    con = sqlite3.connect(DB, timeout=60, check_same_thread=False)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


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


async def check_now():
    con = open_db()
    try:
        messages = await get_recent_messages(15)
        reply(f"🔎 Проверяю последние {len(messages)} постов @{TG_SOURCE_CHANNEL}...")

        for message in reversed(messages):
            text = (message.raw_text or "").strip()
            await asyncio.to_thread(
                send_result,
                con,
                telegram_title(text),
                text[:50000],
                telegram_url(message),
            )

        reply("✅ <b>/checknow завершён</b>")
    finally:
        con.close()


def status():
    try:
        con = open_db()
        row = con.execute("select count(*) from seen").fetchone()
        seen = int(row[0]) if row else 0
        con.close()
    except Exception as e:
        seen = f"ошибка: {e}"

    models = ", ".join(MODELS) if MODELS else "не настроены"
    key = "✅" if LLM_API_KEY else "❌"

    reply(
        "🤖 <b>Linux.do Hunter</b>\n\n"
        f"📡 Источник: @{TG_SOURCE_CHANNEL}\n"
        f"🧠 Groq API: {key}\n"
        f"🧠 Модели: <code>{models}</code>\n"
        f"🎯 MIN_SCORE: {MIN_SCORE}\n"
        f"🗃 Обработано: {seen}\n"
        f"⚡ Команды: /checknow /status"
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
                        reply(f"❌ /checknow: <code>{str(e)[:700]}</code>")

                elif command == "/status":
                    status()

        except Exception as e:
            print(f"command bot: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
