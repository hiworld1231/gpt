#!/usr/bin/env python3
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import requests

BASE = os.getenv("LINUXDO_URL", "https://linux.do").rstrip("/")
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "3600"))
DB = os.getenv("DB_PATH", "/var/lib/linuxdo-hunter/state.db")
MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))

KEYWORDS = {
    "free": 8, "бесплат": 8, "халяв": 10, "free tier": 10, "เครดิตฟรี": 8,
    "credit": 6, "credits": 6, "trial": 7, "student": 6, "promo": 6,
    "quota": 8, "limit": 7, "лимит": 8, "reset": 7, "abuse": 10,
    "bypass": 10, "обход": 10, "bug": 9, "уязв": 9, "api": 5,
    "codex": 8, "claude": 8, "opus": 8, "gpt": 8, "gemini": 8,
    "grok": 7, "kiro": 9, "sub2api": 10, "newapi": 9, "openrouter": 6,
    "groq": 7, "oracle": 7, "vps": 7, "credits": 7, "เครดิตฟรี": 8,
}

BAD = ["куплю", "продам", "продажа аккаунт", "ищу аккаунт", "рефералка", "новости дня"]


def db_init():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("create table if not exists seen (id integer primary key, url text unique, created text)")
    con.commit()
    return con


def tg(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": False}, timeout=20)
    r.raise_for_status()


def fetch_latest():
    r = requests.get(f"{BASE}/latest.json?order=created", timeout=30, headers={"User-Agent": "linuxdo-hunter/1.0"})
    r.raise_for_status()
    return r.json().get("topic_list", {}).get("topics", [])


def topic_text(topic_id):
    r = requests.get(f"{BASE}/t/{topic_id}.json", timeout=30, headers={"User-Agent": "linuxdo-hunter/1.0"})
    r.raise_for_status()
    data = r.json()
    posts = data.get("post_stream", {}).get("posts", [])
    text = "\n\n".join(p.get("cooked", "") for p in posts[:5])
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return data.get("title", ""), text[:12000]


def heuristic(title, text):
    s = (title + " " + text).lower()
    score = 0
    for k, v in KEYWORDS.items():
        if k in s:
            score += v
    for k in BAD:
        if k in s:
            score -= 20
    return min(100, score)


def llm(title, text):
    if not LLM_API_KEY:
        return {"score": heuristic(title, text), "why": "LLM не настроена", "summary": title, "how": "Открой пост и проверь детали.", "risk": "Проверить вручную."}
    prompt = f'''Ты — охотник за самой жирной халявой и серыми AI-находками на Linux.do. Твоя задача — отбирать только реально полезные новые находки. Особенно интересны бесплатные/почти бесплатные AI API, кредиты, подписки, большие лимиты, баги квот, необычные free-tier, Kiro/Codex/Claude/GPT/Gemini/Groq, Sub2API/NewAPI и похожие проекты, VPS/cloud credits и подтверждённые обходы ограничений. Не считай ценным обычную новость или вопрос без результата.

Верни СТРОГО JSON с полями:
score (0-100), summary (до 240 символов на русском), how (до 350 символов: как примерно повторить), risk (до 200 символов), why (до 160 символов).
70+ = отправлять. Не придумывай деталей, которых нет в тексте. Если схема требует чужих украденных ключей/аккаунтов, поставь score <= 60.

ЗАГОЛОВОК: {title}
ТЕКСТ: {text}'''
    r = requests.post(f"{LLM_BASE}/chat/completions", headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}, json={"model": LLM_MODEL, "temperature": 0.1, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]}, timeout=60)
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    return json.loads(raw)


def process(con):
    topics = fetch_latest()
    # latest.json can contain pinned/old items; only process unseen URLs.
    for t in reversed(topics):
        tid = t.get("id")
        if not tid:
            continue
        url = f"{BASE}/t/{t.get('slug', 'topic')}/{tid}"
        if con.execute("select 1 from seen where url=?", (url,)).fetchone():
            continue
        con.execute("insert or ignore into seen(url, created) values(?,?)", (url, datetime.now(timezone.utc).isoformat()))
        con.commit()
        try:
            title, text = topic_text(tid)
            result = llm(title, text)
            score = int(result.get("score", 0))
            if score < MIN_SCORE:
                continue
            msg = (f"🔥 <b>{'S-TIER' if score >= 90 else 'A-TIER'} — {score}/100</b>\n\n"
                   f"💎 <b>{result.get('summary','')}</b>\n\n"
                   f"💰 <b>Почему жирно:</b> {result.get('why','')}\n"
                   f"🛠 <b>Как примерно повторить:</b> {result.get('how','')}\n"
                   f"⚠️ <b>Риск/лимиты:</b> {result.get('risk','')}\n\n"
                   f"🔗 {url}")
            # Telegram HTML mode; strip accidental tags from model output.
            msg = re.sub(r"<(?!/?b>|/?i>|/?code>|/?pre>)", "", msg)
            tg(msg)
        except Exception as e:
            print(f"topic {tid}: {e}", flush=True)


def main():
    con = db_init()
    tg("🟢 Linux.do Hunter запущен. Ищу только самые жирные находки: халява, AI API, лимиты, free-tier, абузы/workarounds.")
    while True:
        try:
            process(con)
        except Exception as e:
            print(f"cycle: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
