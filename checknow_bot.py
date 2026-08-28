#!/usr/bin/env python3
import asyncio
import os
import sqlite3

import requests
from telethon import TelegramClient

from linuxdo_hunter import send_result, telegram_url, telegram_title, TG_SOURCE_CHANNEL, TG_API_ID, TG_API_HASH, TG_SESSION, DB

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


async def check_now():
    if not TG_API_ID or not TG_API_HASH:
        reply("❌ TG_API_ID/TG_API_HASH не настроены")
        return

    con = sqlite3.connect(DB, check_same_thread=False)
    client = TelegramClient(TG_SESSION, int(TG_API_ID), TG_API_HASH)
    await client.start()
    entity = await client.get_entity(TG_SOURCE_CHANNEL)

    messages = []
    async for message in client.iter_messages(entity, limit=15):
        if (message.raw_text or "").strip():
            messages.append(message)

    reply(f"🔎 Проверяю последние {len(messages)} постов @{TG_SOURCE_CHANNEL}...")

    # Process oldest -> newest. send_result handles deduplication.
    for message in reversed(messages):
        text = (message.raw_text or "").strip()
        await asyncio.to_thread(
            send_result,
            con,
            telegram_title(text),
            text[:50000],
            telegram_url(message),
        )

    reply("✅ /checknow завершён")
    await client.disconnect()


def main():
    offset = None
    while True:
        try:
            for update in get_updates(offset):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                text = (message.get("text") or "").strip().split()[0] if message.get("text") else ""
                if str(chat.get("id")) != CHAT_ID:
                    continue
                if text.lower() in ("/checknow", "/check"):
                    asyncio.run(check_now())
        except Exception as e:
            print(f"checknow bot: {e}", flush=True)
            import time
            time.sleep(5)


if __name__ == "__main__":
    main()
