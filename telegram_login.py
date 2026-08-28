#!/usr/bin/env python3
import os
from telethon.sync import TelegramClient

api_id = os.environ.get("TG_API_ID")
api_hash = os.environ.get("TG_API_HASH")
session = os.environ.get("TG_SESSION", "/var/lib/linuxdo-hunter/linuxdo_hunter")

if not api_id or not api_hash:
    raise SystemExit("Set TG_API_ID and TG_API_HASH in /etc/linuxdo-hunter.env first")

client = TelegramClient(session, int(api_id), api_hash)
client.start()
print("Telegram login OK. Session saved to:", session + ".session")
client.disconnect()
