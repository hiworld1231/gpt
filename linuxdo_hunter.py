#!/usr/bin/env python3
import html
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from curl_cffi import requests as cf_requests

try:
    from telethon import TelegramClient, events
except ImportError:
    TelegramClient = None
    events = None

BASE = os.getenv("LINUXDO_URL", "https://linux.do").rstrip("/")
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
models_env = os.getenv("LLM_MODELS", "")
MODELS = [x.strip() for x in models_env.split(",") if x.strip()] if models_env else [os.getenv("LLM_MODEL", "openai/gpt-oss-120b")]
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "3600"))
DB = os.getenv("DB_PATH", "/var/lib/linuxdo-hunter/state.db")
MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))
TG_SOURCE_CHANNEL = os.getenv("TG_SOURCE_CHANNEL", "linuxdoit").lstrip("@")
TG_API_ID = os.getenv("TG_API_ID", "")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "/var/lib/linuxdo-hunter/linuxdo_hunter")

BROWSER_HEADERS = {
    "Accept": "application/json, text/html, application/rss+xml, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def db_init():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("create table if not exists seen (id integer primary key, url text unique, created text)")
    con.execute("create table if not exists model_cooldown (model text primary key, until_ts real)")
    con.commit()
    return con


def tg(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={
        "chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False
    }, timeout=20)
    r.raise_for_status()


def clean_html(text):
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def linuxdo_get(path, accept=None):
    headers = dict(BROWSER_HEADERS)
    if accept:
        headers["Accept"] = accept
    api_key = os.getenv("LINUXDO_API_KEY", "")
    api_user = os.getenv("LINUXDO_API_USERNAME", "")
    if api_key:
        headers["Api-Key"] = api_key
        if api_user:
            headers["Api-Username"] = api_user
    return cf_requests.get(f"{BASE}{path}", headers=headers, impersonate="chrome", timeout=30, allow_redirects=True)


def parse_rss(content):
    root = ET.fromstring(content)
    topics = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        m = re.search(r"/t/(?:[^/]+/)?(\d+)", link)
        if not m:
            continue
        topics.append({"id": int(m.group(1)), "slug": link.split("/t/")[-1].rsplit("/", 1)[0], "title": clean_html(title), "url": link})
    return topics


def fetch_latest():
    errors = []
    for path in ("/latest.json?order=created", "/latest.json"):
        try:
            r = linuxdo_get(path, "application/json, text/plain, */*")
            r.raise_for_status()
            topics = r.json().get("topic_list", {}).get("topics", [])
            if topics:
                return topics
        except Exception as e:
            errors.append(f"{path}: {e}")
    for path in ("/latest.rss", "/top.rss?period=daily"):
        try:
            r = linuxdo_get(path, "application/rss+xml, application/xml, text/xml, */*")
            r.raise_for_status()
            topics = parse_rss(r.content)
            if topics:
                return topics
        except Exception as e:
            errors.append(f"{path}: {e}")
    raise RuntimeError(" | ".join(errors))


def topic_text(topic_id):
    r = linuxdo_get(f"/t/{topic_id}.json", "application/json, text/plain, */*")
    r.raise_for_status()
    data = r.json()
    posts = data.get("post_stream", {}).get("posts", [])
    parts = []
    for i, p in enumerate(posts, 1):
        author = p.get("username") or "unknown"
        body = clean_html(p.get("cooked", ""))
        if body:
            parts.append(f"[Комментарий {i} — {author}]\n{body}")
    return data.get("title", ""), "\n\n".join(parts)[:30000]


def model_available(con, model):
    row = con.execute("select until_ts from model_cooldown where model=?", (model,)).fetchone()
    return not row or time.time() >= row[0]


def cooldown_model(con, model, seconds=300):
    con.execute("insert into model_cooldown(model, until_ts) values (?, ?) on conflict(model) do update set until_ts=excluded.until_ts", (model, time.time() + seconds))
    con.commit()


def call_model(model, prompt):
    r = requests.post(f"{LLM_BASE}/chat/completions", headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}, json={
        "model": model, "temperature": 0.1, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]
    }, timeout=120)
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def llm(con, title, text, source_url=""):
    if not LLM_API_KEY:
        return {"score": 0, "why": "LLM не настроена", "summary": title, "how": "", "risk": ""}
    prompt = f'''Ты — автономный охотник за САМЫМИ ЖИРНЫМИ находками.

Ниже передан ПОЛНЫЙ ДОСТУПНЫЙ ТЕКСТ поста/темы и комментариев. НЕ используй keyword-фильтр. Сам прочитай материал и реши, есть ли там практическая ценность.

Ищи прежде всего бесплатный/почти бесплатный доступ к сильным AI-моделям, API/credits/free-tier/trial, огромные лимиты, loophole/reset квот, Sub2API/New API/CPA/gateway, Kiro/Codex/Claude/GPT/Gemini/Groq, бесплатные VPS/cloud/credits, новые workaround и серые схемы, а также любые неожиданные способы получить ресурс за $0 или сильно дешевле.

Материал может быть на китайском, английском или русском. Переводи и объясняй по-русски.

Не считай ценным обычную новость, релиз, вопрос или теорию без практического результата. Не выдумывай детали. Если способ требует украденных чужих ключей/аккаунтов или чужого доступа — score <= 60.

Верни СТРОГО JSON:
{{"score":0,"summary":"кратко по-русски","why":"почему жирно","how":"примерные шаги","risk":"лимиты/риск"}}

90-100 = очень редкая/жирная находка, проверить сразу.
70-89 = реально полезная находка.
<70 = не отправлять.
summary <= 300 символов; why <= 220; how <= 500; risk <= 250.

ИСТОЧНИК: {source_url}
ЗАГОЛОВОК: {title}

ПОЛНЫЙ ТЕКСТ:
{text}'''
    last_error = None
    for model in MODELS:
        if not model_available(con, model):
            continue
        try:
            result = call_model(model, prompt)
            result["score"] = int(result.get("score", 0))
            return result
        except RuntimeError as e:
            last_error = str(e)
            if str(e) == "RATE_LIMIT":
                print(f"model {model}: 429, cooldown 5m", flush=True)
                cooldown_model(con, model, 300)
                continue
            raise
    raise RuntimeError(f"all models unavailable: {last_error}")


def already_seen(con, url):
    return bool(con.execute("select 1 from seen where url=?", (url,)).fetchone())


def mark_seen(con, url):
    con.execute("insert or ignore into seen(url, created) values(?, ?)", (url, datetime.now(timezone.utc).isoformat()))
    con.commit()


def send_result(con, title, text, url):
    if already_seen(con, url):
        return
    try:
        result = llm(con, title, text, url)
        score = int(result.get("score", 0))
        if score >= MIN_SCORE:
            tier = "S-TIER" if score >= 90 else "A-TIER"
            msg = (
                f"🔥 <b>{tier} — {score}/100</b>\n\n"
                f"💎 <b>{html.escape(str(result.get('summary', '')))}</b>\n\n"
                f"💰 <b>Почему жирно:</b> {html.escape(str(result.get('why', '')))}\n"
                f"🛠 <b>Как примерно повторить:</b> {html.escape(str(result.get('how', '')))}\n"
                f"⚠️ <b>Риск/лимиты:</b> {html.escape(str(result.get('risk', '')))}\n\n"
                f"🔗 {html.escape(url)}"
            )
            tg(msg)
            print(f"SENT {score}: {title}", flush=True)
        mark_seen(con, url)
    except Exception as e:
        print(f"item {url}: {e}", flush=True)


def process_linuxdo(con):
    topics = fetch_latest()
    for t in reversed(topics):
        tid = t.get("id")
        if not tid:
            continue
        slug = t.get("slug") or "topic"
        url = f"{BASE}/t/{slug}/{tid}"
        if already_seen(con, url):
            continue
        try:
            title, text = topic_text(tid)
            send_result(con, title, text, url)
        except Exception as e:
            print(f"linuxdo topic {tid}: {e}", flush=True)


def telegram_text(message):
    text = message.raw_text or ""
    if not text and getattr(message, "message", None):
        text = str(message.message)
    return text.strip()


def telegram_url(message):
    return f"https://t.me/{TG_SOURCE_CHANNEL}/{message.id}"


def telegram_title(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines:
        return f"Telegram post {datetime.now(timezone.utc).isoformat()}"
    return lines[0][:300]


async def run_telegram(con):
    if not TelegramClient:
        raise RuntimeError("Telethon не установлен: pip install telethon")
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH не настроены")

    client = TelegramClient(TG_SESSION, int(TG_API_ID), TG_API_HASH)
    await client.start()
    entity = await client.get_entity(TG_SOURCE_CHANNEL)

    # On first run, don't flood Telegram with the whole history. Seed the last 10 posts as seen.
    seed_key = f"tg_seed_{TG_SOURCE_CHANNEL}"
    con.execute("create table if not exists meta (key text primary key, value text)")
    seeded = con.execute("select 1 from meta where key=?", (seed_key,)).fetchone()
    if not seeded:
        async for message in client.iter_messages(entity, limit=10):
            con.execute("insert or ignore into seen(url, created) values(?, ?)", (telegram_url(message), datetime.now(timezone.utc).isoformat()))
        con.execute("insert into meta(key,value) values(?,?)", (seed_key, "1"))
        con.commit()
        print(f"Telegram source @{TG_SOURCE_CHANNEL}: seeded last 10 posts", flush=True)

    @client.on(events.NewMessage(chats=entity))
    async def handler(event):
        message = event.message
        text = telegram_text(message)
        if not text:
            return
        url = telegram_url(message)
        title = telegram_title(text)
        # Run blocking LLM/network work off the event loop.
        import asyncio
        await asyncio.to_thread(send_result, con, title, text[:30000], url)

    print(f"Telegram listener active: @{TG_SOURCE_CHANNEL}", flush=True)
    await client.run_until_disconnected()


def main():
    con = db_init()
    tg("🟢 <b>Linux.do Hunter v4 запущен</b>\n\nИсточник: Telegram @" + html.escape(TG_SOURCE_CHANNEL) + "\nLLM читает посты целиком, без keyword-фильтра.")

    # Telegram is the primary real-time source. Linux.do HTTP remains an optional fallback.
    if TG_API_ID and TG_API_HASH and TelegramClient:
        import asyncio
        try:
            asyncio.run(run_telegram(con))
            return
        except Exception as e:
            print(f"telegram listener stopped: {e}", flush=True)

    # Fallback to the old hourly Linux.do polling when Telegram isn't configured.
    while True:
        try:
            process_linuxdo(con)
        except Exception as e:
            print(f"cycle: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
