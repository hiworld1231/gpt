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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/138 Safari/537.36 LinuxDO-Hunter/2.0",
    "Accept": "application/json, text/html, */*",
}

KEYWORDS = {
    "free": 8, "бесплат": 8, "халяв": 10, "free tier": 10,
    "credit": 6, "credits": 6, "trial": 7, "student": 6, "promo": 6,
    "quota": 8, "limit": 7, "лимит": 8, "reset": 7, "abuse": 10,
    "bypass": 10, "обход": 10, "bug": 9, "уязв": 9, "api": 5,
    "codex": 8, "claude": 8, "opus": 8, "gpt": 8, "gemini": 8,
    "grok": 7, "kiro": 9, "sub2api": 10, "newapi": 9, "openrouter": 6,
    "groq": 7, "oracle": 7, "vps": 7, "cloud": 6,
}
BAD = ["куплю", "продам", "продажа аккаунт", "ищу аккаунт", "рефералка", "новости дня"]


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


def fetch_latest():
    errors = []

    # RSS is usually less restricted than the Discourse JSON endpoint.
    try:
        r = requests.get(f"{BASE}/latest.rss", headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        topics = []
        for item in root.findall(".//item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            m = re.search(r"/t/(?:[^/]+/)?(\d+)", link)
            if not m:
                continue
            topics.append({
                "id": int(m.group(1)),
                "slug": link.split("/t/")[-1].rsplit("/", 1)[0],
                "title": clean_html(title),
                "url": link,
            })
        if topics:
            return topics
    except Exception as e:
        errors.append(f"RSS: {e}")

    # JSON fallbacks.
    for path in ("/latest.json", "/latest.json?order=created"):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=30)
            r.raise_for_status()
            topics = r.json().get("topic_list", {}).get("topics", [])
            if topics:
                return topics
        except Exception as e:
            errors.append(f"{path}: {e}")

    raise RuntimeError(" | ".join(errors))


def topic_text(topic_id):
    r = requests.get(f"{BASE}/t/{topic_id}.json", headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    posts = data.get("post_stream", {}).get("posts", [])
    text = "\n\n".join(clean_html(p.get("cooked", "")) for p in posts[:8])
    return data.get("title", ""), text[:16000]


def heuristic(title, text):
    s = (title + " " + text).lower()
    score = sum(v for k, v in KEYWORDS.items() if k in s)
    score -= sum(20 for k in BAD if k in s)
    return max(0, min(100, score))


def model_available(con, model):
    row = con.execute("select until_ts from model_cooldown where model=?", (model,)).fetchone()
    return not row or time.time() >= row[0]


def cooldown_model(con, model, seconds=300):
    con.execute("insert into model_cooldown(model, until_ts) values (?, ?) on conflict(model) do update set until_ts=excluded.until_ts", (model, time.time() + seconds))
    con.commit()


def call_model(model, prompt):
    r = requests.post(f"{LLM_BASE}/chat/completions", headers={
        "Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"
    }, json={
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }, timeout=90)
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def llm(con, title, text):
    if not LLM_API_KEY:
        return {"score": heuristic(title, text), "why": "LLM не настроена", "summary": title, "how": "Открой пост и проверь детали.", "risk": "Проверить вручную."}

    prompt = f'''Ты — охотник за самыми жирными находками на Linux.do. Ищи НЕ новости и НЕ обычные обсуждения.

Приоритет: бесплатные AI API, кредиты, большие лимиты, free-tier, GPT/Claude/Gemini/Grok/Codex/Kiro, Sub2API/NewAPI, VPS/cloud credits, баги квот, необычные workaround и серые схемы с реальной практической выгодой.

Верни СТРОГО JSON: {{"score":0,"summary":"","why":"","how":"","risk":""}}.
90-100 = ебануто жирная находка; 70-89 = реально полезная; <70 = не отправлять.
Не придумывай детали. Если автор только спрашивает/теоретизирует без результата — низкий score. Если схема требует чужих украденных ключей/аккаунтов — score <= 60.
summary <= 240 символов; why <= 180; how <= 400; risk <= 200.

ЗАГОЛОВОК: {title}
ТЕКСТ: {text}'''

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


def process(con):
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
            if heuristic(title, text) < 5:
                mark_seen(con, url)
                continue
            result = llm(con, title, text)
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
            print(f"topic {tid}: {e}", flush=True)


def main():
    con = db_init()
    tg("🟢 <b>Linux.do Hunter запущен</b>\n\nИщу только жирные находки: халява, AI API, кредиты, лимиты, free-tier, workaround и серые схемы.")
    while True:
        try:
            process(con)
        except Exception as e:
            print(f"cycle: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
