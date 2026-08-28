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
    """Use Chrome TLS fingerprint; optionally add Discourse API credentials."""
    headers = dict(BROWSER_HEADERS)
    if accept:
        headers["Accept"] = accept
    api_key = os.getenv("LINUXDO_API_KEY", "")
    api_user = os.getenv("LINUXDO_API_USERNAME", "")
    if api_key:
        headers["Api-Key"] = api_key
        if api_user:
            headers["Api-Username"] = api_user
    return cf_requests.get(
        f"{BASE}{path}",
        headers=headers,
        impersonate="chrome",
        timeout=30,
        allow_redirects=True,
    )


def parse_rss(content):
    root = ET.fromstring(content)
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
    return topics


def fetch_latest():
    errors = []

    # Prefer the normal Discourse endpoint with a real Chrome TLS fingerprint.
    for path in ("/latest.json?order=created", "/latest.json"):
        try:
            r = linuxdo_get(path, "application/json, text/plain, */*")
            r.raise_for_status()
            topics = r.json().get("topic_list", {}).get("topics", [])
            if topics:
                return topics
        except Exception as e:
            errors.append(f"{path}: {e}")

    # RSS fallback.
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
    # Keep a large but bounded payload. The LLM sees the article and comments,
    # not a keyword-selected fragment.
    text = "\n\n".join(parts)
    return data.get("title", ""), text[:30000]


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
    }, timeout=120)
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def llm(con, title, text):
    if not LLM_API_KEY:
        return {"score": 0, "why": "LLM не настроена", "summary": title, "how": "", "risk": ""}

    prompt = f'''Ты — автономный охотник за САМЫМИ ЖИРНЫМИ находками на Linux.do.

ВАЖНО: ниже передан ПОЛНЫЙ ДОСТУПНЫЙ ТЕКСТ темы вместе с комментариями. НЕ используй keyword-фильтр и НЕ предполагай заранее, о чём тема. Сам прочитай материал и реши, есть ли там что-то ценное.

Ищи прежде всего:
- бесплатный или почти бесплатный доступ к сильным AI-моделям;
- бесплатные API, кредиты, trial, подписки, student/free-tier;
- огромные или необычные лимиты/квоты;
- баги тарификации, reset лимитов и необычные loophole;
- Sub2API, New API, CPA, gateway, Kiro, Codex, Claude, GPT, Gemini, Groq и любые новые аналоги;
- бесплатные VPS/cloud/credits;
- новые рабочие workaround и серые схемы;
- неожиданные способы получить ресурс за $0 или сильно дешевле.

Тема может быть на китайском, английском или русском. Понимай и переводь её сам.

Не считай ценным обычную новость, релиз модели, просьбу о помощи или теоретическое обсуждение без практического результата.

Верни СТРОГО JSON:
{{
  "score": 0,
  "summary": "кратко по-русски, что нашли",
  "why": "почему это реально жирно",
  "how": "что делать пользователю примерно по шагам",
  "risk": "лимиты, бан, условия и т.п."
}}

90-100 = очень редкая/жирная находка, которую стоит проверить сразу.
70-89 = реально полезная находка.
<70 = не отправлять.
Не выдумывай отсутствующие в тексте детали. Если способ требует украденных чужих ключей/аккаунтов или чужого доступа — score <= 60.
summary <= 300 символов; why <= 220; how <= 500; risk <= 250.

ЗАГОЛОВОК:
{title}

ПОЛНЫЙ ТЕКСТ ТЕМЫ И КОММЕНТАРИЕВ:
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
    tg("🟢 <b>Linux.do Hunter v3 запущен</b>\n\nТеперь LLM сама читает тему и комментарии целиком — без keyword-фильтра.")
    while True:
        try:
            process(con)
        except Exception as e:
            print(f"cycle: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
