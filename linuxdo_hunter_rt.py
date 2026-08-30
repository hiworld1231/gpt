#!/usr/bin/env python3
"""Reliable Telegram realtime front-end for linuxdo_hunter.

Uses both Telethon events and a short polling catch-up loop. Polling is only a
safety net; the existing seen table still performs persistent deduplication.
"""
import asyncio
import html
import os

from telethon import TelegramClient, events

import linuxdo_hunter as core

POLL_SECONDS = max(5, int(os.getenv("TG_REALTIME_POLL_SECONDS", "15")))
CATCHUP_LIMIT = max(5, min(int(os.getenv("TG_REALTIME_CATCHUP_LIMIT", "30")), 100))


async def main():
    con = core.db_init()
    if not core.TG_API_ID or not core.TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH не настроены")

    client = TelegramClient(core.TG_SESSION, int(core.TG_API_ID), core.TG_API_HASH)
    await client.start()
    entity = await client.get_entity(core.TG_SOURCE_CHANNEL)

    queue = asyncio.Queue()
    queued = set()
    completed = set()

    async def enqueue(message, source):
        if not message:
            return
        text = (message.raw_text or "").strip()
        if not text:
            return
        if message.id in queued or message.id in completed:
            return
        queued.add(message.id)
        await queue.put((message, source))
        print(f"TG RECEIVED id={message.id} via={source} chars={len(text)}", flush=True)

    @client.on(events.NewMessage(chats=entity))
    async def on_new(event):
        await enqueue(event.message, "event")

    async def poller():
        # Immediate first pass recovers posts missed during deploy/reconnect.
        while True:
            try:
                batch = []
                async for message in client.iter_messages(entity, limit=CATCHUP_LIMIT):
                    batch.append(message)
                for message in reversed(batch):
                    await enqueue(message, "poll")
            except Exception as e:
                print(f"TG POLL ERROR: {e}", flush=True)
            await asyncio.sleep(POLL_SECONDS)

    async def worker():
        while True:
            message, source = await queue.get()
            try:
                text = (message.raw_text or "").strip()
                url = core.telegram_url(message)
                if core.already_seen(con, url):
                    print(f"TG SKIP SEEN id={message.id}", flush=True)
                    completed.add(message.id)
                    continue

                print(f"TG ANALYZE START id={message.id} via={source}", flush=True)
                await asyncio.to_thread(
                    core.send_result,
                    con,
                    core.telegram_title(text),
                    text[:50000],
                    url,
                )

                if core.already_seen(con, url):
                    completed.add(message.id)
                    print(f"TG ANALYZE DONE id={message.id}", flush=True)
                else:
                    # send_result intentionally leaves failed items unseen, so the
                    # next poll retries them instead of silently losing the post.
                    print(f"TG ANALYZE RETRY id={message.id} (not marked seen)", flush=True)
            except Exception as e:
                print(f"TG WORKER ERROR id={getattr(message, 'id', '?')}: {e}", flush=True)
            finally:
                queued.discard(message.id)
                queue.task_done()

    try:
        core.tg(
            "🟢 <b>Linux.do Hunter realtime запущен</b>\n\n"
            f"📡 @{html.escape(core.TG_SOURCE_CHANNEL)}\n"
            f"⚡ NewMessage + backup poll каждые {POLL_SECONDS}с"
        )
    except Exception as e:
        print(f"startup notify failed: {e}", flush=True)

    print(
        f"Telegram reliable listener active: @{core.TG_SOURCE_CHANNEL}; "
        f"event + poll/{POLL_SECONDS}s; catchup={CATCHUP_LIMIT}",
        flush=True,
    )
    await asyncio.gather(poller(), worker(), client.run_until_disconnected())


if __name__ == "__main__":
    asyncio.run(main())
