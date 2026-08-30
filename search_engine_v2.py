#!/usr/bin/env python3
"""Broad AI-assisted Telegram search.

Searches every configured source with many short overlapping queries, then uses
LLM only for semantic ranking and deep analysis. There is intentionally no
"top N"/max-results cap in this module.
"""
import asyncio
import html
import json
import os
from datetime import timezone

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon import functions

import checknow_bot as bot
import source_manager

MAX_QUERY_WORDS = 6
COMMENTS_PER_POST = 100


def _client():
    if not bot.TG_API_ID or not bot.TG_API_HASH or not bot.TG_CHECKNOW_STRING_SESSION:
        raise RuntimeError("Telegram API/session не настроены")
    return TelegramClient(StringSession(bot.TG_CHECKNOW_STRING_SESSION), int(bot.TG_API_ID), bot.TG_API_HASH)


def _languages():
    raw = os.getenv("SEARCH_LANGUAGES", "zh,en").strip()
    allowed = {"zh", "en", "ru", "ja", "ko"}
    out = [x.strip().lower() for x in raw.split(",") if x.strip() in allowed]
    return out or ["zh", "en"]


def _fallback_plan(request):
    return {"queries": [x for x in request.split() if x] or [request], "intent": request}


def plan_search(request):
    if not bot.LLM_API_KEY:
        return _fallback_plan(request)
    langs = _languages()
    names = {"zh": "简体中文", "en": "English", "ru": "Русский", "ja": "日本語", "ko": "한국어"}
    allowed = ", ".join(names[x] for x in langs)
    prompt = f'''Ты планировщик ПОИСКА по Telegram.
Пользовательский запрос:
{request}

Источник в основном содержит языки: {allowed}.
Генерируй поисковые фразы ТОЛЬКО на этих языках. Если ru не указан — русский запрещён.

Нужна МАКСИМАЛЬНО ШИРОКАЯ сетка коротких запросов. Telegram будет выполнять КАЖДЫЙ запрос отдельно.

Правила:
- Выдели все важные сущности: сервисы, продукты, технологии, функции, действия, объекты.
- Для каждой сущности делай отдельные базовые запросы.
- Делай варианты написания. Например ОБЯЗАТЕЛЬНО для TikTok: `tiktok` и `tik tok`.
- Затем комбинируй сущности короткими фразами: `tiktok api`, `tik tok api`, `tiktok username`, `tik tok username`, `tiktok abuse`, `tik tok abuse`, `tiktok api abuse` и т.д.
- Если пользователь явно ищет abuse/hack/exploit/bypass и т.п., эти слова разрешены и полезны, но НИКОГДА не заменяют базовое название сущности.
- Для каждого языка делай естественные варианты, а не перевод всего пользовательского предложения.
- Китайский должен быть настоящим китайским: например для TikTok можно использовать `抖音`, а не `TikTok abuse` как псевдокитайский запрос.
- Запросы короткие: обычно 1–4 слова/термина. Никаких предложений.
- Не придумывай новые продукты/цели, которых нет в запросе.
- НЕ ограничивай количество запросов. Чем больше полезных коротких пересечений, тем лучше.
- Не добавляй `max_results`, `limit` или любую искусственную цель количества результатов.

Верни строго JSON:
{{"queries":["tiktok","tik tok","tiktok api","tik tok api","tiktok username","tik tok username","tiktok abuse","tik tok abuse"],"intent":"краткая семантическая цель","language_notes":"языки"}}
'''
    try:
        data = bot.call_model(bot.MODELS[0], prompt)
        queries, seen = [], set()
        for raw in data.get("queries", []):
            q = str(raw).strip()
            if not q or len(q) > 100 or len(q.split()) > MAX_QUERY_WORDS:
                continue
            k = q.casefold()
            if k not in seen:
                seen.add(k)
                queries.append(q)
        if not queries:
            return _fallback_plan(request)
        return {"queries": queries, "intent": str(data.get("intent", request))[:1000], "language_notes": str(data.get("language_notes", ",".join(langs)))[:500]}
    except Exception as e:
        print(f"search planner fallback: {e}", flush=True)
        return _fallback_plan(request)


async def _discussion_comments(client, entity, message_id):
    """Read linked Telegram forum discussion when the source channel has one."""
    try:
        discussion = await client(functions.messages.GetDiscussionMessageRequest(peer=entity, msg_id=message_id))
        if not discussion or not discussion.messages:
            return []
        root = discussion.messages[0]
        discussion_peer = None
        if getattr(discussion, "chats", None):
            discussion_peer = discussion.chats[0]
        if discussion_peer is None:
            return []
        replies = []
        async for reply in client.iter_messages(discussion_peer, reply_to=root.id, limit=COMMENTS_PER_POST):
            text = (reply.raw_text or "").strip()
            if text:
                replies.append(text)
        return replies
    except Exception as e:
        print(f"discussion {message_id}: {e}", flush=True)
        return []


async def _search_everywhere(queries):
    client = _client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("checknow Telegram session не авторизована")
        sources = source_manager.get_sources(bot)
        posts = {}
        comments = {}
        for source in sources:
            entity = await client.get_entity(source)
            print(f"search source @{source}: {len(queries)} queries", flush=True)
            for query in queries:
                try:
                    # limit=None is intentional: do not impose an application-side result cap.
                    async for message in client.iter_messages(entity, search=query, limit=None):
                        text = (message.raw_text or "").strip()
                        if text:
                            posts[(source, message.id)] = (message, source)
                except Exception as e:
                    print(f"telegram search @{source} '{query}': {e}", flush=True)
        for (source, mid), (message, _) in posts.items():
            replies = await _discussion_comments(client, await client.get_entity(source), mid)
            if replies:
                comments[(source, mid)] = replies
        result = []
        for (source, _), (message, _) in posts.items():
            message._hunter_source = source
            result.append(message)
        return result, comments
    finally:
        await client.disconnect()


def _rank_hits(request, posts, comments):
    if not posts or not bot.LLM_API_KEY:
        return [(m, 100) for m in posts]
    chunks = []
    for m in posts:
        source = getattr(m, "_hunter_source", bot.TG_SOURCE_CHANNEL)
        reply_text = "\n".join(comments.get((source, m.id), [])[:20])
        chunks.append({"id": f"{source}:{m.id}", "text": (m.raw_text or "")[:3000], "comments": reply_text[:4000]})
    ranked = []
    # LLM JSON payloads can be large; rank in batches but NEVER throw candidates away by count.
    batch_size = 40
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        prompt = f'''Ты ранжировщик результатов Telegram Search.
Исходный запрос пользователя:
{request}

Оцени КАЖДЫЙ кандидат по смысловой релевантности 0-100.
100 = прямо содержит нужную пользователю информацию/код/метод/обсуждение.
70-99 = очень полезно.
40-69 = связано, но частично.
1-39 = слабое совпадение.
0 = мусор, случайное совпадение, новость без нужной информации, вакансия, общий анонс и т.п.

Не повышай оценку только за совпадение слова. Учитывай комментарии.
Верни строго JSON: {{"ranking":[{{"id":"source:123","relevance":0,"reason":"кратко"}}]}}
Включи ВСЕ кандидаты.

КАНДИДАТЫ:
{json.dumps(batch, ensure_ascii=False)}'''
        try:
            data = bot.call_model(bot.MODELS[0], prompt)
            for x in data.get("ranking", []):
                try:
                    ranked.append((str(x["id"]), max(0, min(int(x.get("relevance", 0)), 100))))
                except Exception:
                    pass
        except Exception as e:
            print(f"search ranking batch {start}: {e}", flush=True)
            ranked.extend((x["id"], 50) for x in batch)
    scores = dict(ranked)
    return sorted(
        [(m, scores.get(f"{getattr(m, '_hunter_source', bot.TG_SOURCE_CHANNEL)}:{m.id}", 0)) for m in posts],
        key=lambda x: x[1], reverse=True,
    )


def _context(message, comments, queries, request):
    source = getattr(message, "_hunter_source", bot.TG_SOURCE_CHANNEL)
    text = (message.raw_text or "").strip()
    published = message.date.astimezone(timezone.utc).isoformat() if message.date else "неизвестно"
    material = f"[ПОИСКОВОЙ INTENT]\n{request}\n[ПОИСКОВЫЕ ТЕРМИНЫ]\n{', '.join(queries)}\n[ИСТОЧНИК]\n@{source}\n[Дата]\n{published}\n{text}"
    replies = comments.get((source, message.id), [])
    if replies:
        material += "\n\n[TELEGRAM DISCUSSION / КОММЕНТАРИИ]\n" + "\n---\n".join(replies)
    return material


def _result(result, message, comments):
    source = getattr(message, "_hunter_source", bot.TG_SOURCE_CHANNEL)
    url = f"https://t.me/{source}/{message.id}"
    original = bot.extract_linuxdo_url(message.raw_text or "")
    out = bot.format_result(result, url, original)
    count = len(comments.get((source, message.id), []))
    out += f"\n📡 <b>Источник:</b> @{html.escape(source)}"
    if count:
        out += f"\n💬 <b>Комментарии учтены:</b> {count}"
    return out


async def enhanced_search_now(request, limit=None):
    request = request.strip()
    if not request:
        raise RuntimeError("укажи запрос: /search найди Python код для ...")
    if len(request) > 1000:
        raise RuntimeError("запрос слишком длинный (максимум 1000 символов)")

    plan = await asyncio.to_thread(plan_search, request)
    queries = plan["queries"]
    bot.send_message(
        "🔍 <b>AI Search запущен</b>\n\n"
        f"🧠 Запрос: <code>{html.escape(request)}</code>\n"
        f"🔎 Поисковых фраз: <b>{len(queries)}</b>\n"
        f"🌐 Языки: <b>{html.escape(', '.join(_languages()))}</b>\n"
        "♾️ Лимита результатов нет — собираю все совпадения.\n"
        "🔎 Ищу короткими ключевыми фразами…"
    )
    # Avoid dumping hundreds of queries into Telegram; the actual queries are logged on the server.
    preview = queries if len(queries) <= 40 else queries[:40] + [f"… ещё {len(queries) - 40} запросов"]
    bot.send_message("<b>Стратегия:</b>\n" + "\n".join(f"• {html.escape(q)}" for q in preview))

    posts, comments = await _search_everywhere(queries)
    if not posts:
        bot.send_message("❌ <b>Ничего не найдено.</b> Попробуй более широкие сущности или другое написание.")
        return
    bot.send_message(f"📚 Telegram нашёл <b>{len(posts)}</b> уникальных постов.\n💬 С обсуждениями: <b>{sum(bool(x) for x in comments.values())}</b>.\n🧠 Ранжирую все найденные посты без top-N.")

    ranked = await asyncio.to_thread(_rank_hits, request, posts, comments)
    # No score threshold and no result-count limit. Relevance is used only for ordering.
    settings = bot.load_settings()
    results = []
    for idx, (message, relevance) in enumerate(ranked, 1):
        try:
            text = (message.raw_text or "").strip()
            source = getattr(message, "_hunter_source", bot.TG_SOURCE_CHANNEL)
            url = f"https://t.me/{source}/{message.id}"
            published = message.date.astimezone(timezone.utc).isoformat() if message.date else "неизвестно"
            material = _context(message, comments, queries, request)
            result = await asyncio.to_thread(bot.analyze_without_db, bot.telegram_title(text), material, url, published, settings["mode"])
            results.append((relevance, int(result.get("score", 0)), message, result))
        except Exception as e:
            print(f"search analyze {getattr(message, 'id', '?')}: {e}", flush=True)
        if idx % 10 == 0:
            bot.send_message(f"⏳ Проанализировано {idx}/{len(ranked)}…")

    results.sort(key=lambda x: (x[0], x[1]), reverse=True)
    for relevance, score, message, result in results:
        bot.send_message(_result(result, message, comments), bot.result_keyboard(getattr(message, "_hunter_source", bot.TG_SOURCE_CHANNEL), message.id))
        await asyncio.sleep(0.15)
    bot.send_message(
        "✅ <b>AI Search завершён</b>\n"
        f"🔎 Запрос: <code>{html.escape(request)}</code>\n"
        f"📚 Найдено Telegram: {len(posts)}\n"
        f"💬 Посты с обсуждениями: {sum(bool(x) for x in comments.values())}\n"
        f"🏆 Выдано результатов: <b>{len(results)}</b>\n"
        "♾️ Искусственного max/top-N нет."
    )
