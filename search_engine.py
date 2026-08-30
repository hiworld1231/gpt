#!/usr/bin/env python3
"""AI-assisted Telegram search for Linux.do Hunter."""
import asyncio
import html
import json
import os
from datetime import timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

import checknow_bot as bot

MAX_RETRIEVED = 120
MAX_COMMENTS_PER_POST = 40


def _client():
    if not bot.TG_API_ID or not bot.TG_API_HASH or not bot.TG_CHECKNOW_STRING_SESSION:
        raise RuntimeError("Telegram API/session не настроены")
    return TelegramClient(StringSession(bot.TG_CHECKNOW_STRING_SESSION), int(bot.TG_API_ID), bot.TG_API_HASH)


def _search_languages():
    """Languages used for Telegram search, in priority order.

    Configure with SEARCH_LANGUAGES=zh,en for a Chinese/English source.
    Russian is intentionally NOT a default for Chinese-focused sources.
    """
    raw = os.getenv("SEARCH_LANGUAGES", "zh,en").strip()
    langs = [x.strip().lower() for x in raw.split(",") if x.strip()]
    allowed = {"zh", "en", "ru", "ja", "ko"}
    return [x for x in langs if x in allowed] or ["zh", "en"]


def _fallback_plan(request):
    # Never send a long natural-language user request directly to Telegram search.
    words = request.split()
    literal = request if len(words) <= 4 else (words[0] if words else request)
    return {"queries": [literal], "max_results": 10, "language_notes": "", "intent": request}


def plan_search(request):
    """Use the LLM only to expand the request into short Telegram search terms."""
    if not bot.LLM_API_KEY:
        return _fallback_plan(request)

    languages = _search_languages()
    language_names = {"zh": "китайском (简体中文)", "en": "английском", "ru": "русском", "ja": "японском", "ko": "корейском"}
    requested_languages = ", ".join(language_names[x] for x in languages)
    prompt = f'''Ты планировщик поиска по Telegram.
Пользователь написал:
{request}

Источник ориентирован на языки: {requested_languages}.
Генерируй поисковые запросы ТОЛЬКО на этих языках. НЕ генерируй русский, если ru не указан.
Telegram будет выполнять КАЖДЫЙ запрос отдельно. Ограничения на количество запросов НЕТ.

Задача: найти реальные сообщения, относящиеся к смыслу запроса пользователя.

ВАЖНО:
- НЕ копируй пользовательский запрос целиком, если это длинное предложение.
- Каждый запрос должен быть коротким: обычно 1–4 слова.
- Для каждой важной сущности делай варианты написания и короткие комбинации.
- Пример для TikTok: tiktok, tik tok, tiktok python, tik tok python, tiktok username, tik tok username, tiktok api.
- Для китайского поиска используй естественные китайские термины, а не машинный перевод русского предложения.
- Для английского используй естественные английские технические термины.
- Ищи названия продуктов, функций, API, инструментов и короткие тематические сочетания.
- Не добавляй длинные вопросы, предложения, инструкции или объяснения.
- Не добавляй русские слова/фразы, если русский язык не входит в список источников.
- Не добавляй сверхобщие одиночные слова вроде code/python/api, если они сами по себе не являются полезным поисковым термином.
- Дедуплицируй запросы без учёта регистра.

Верни СТРОГО JSON:
{{"queries":["tiktok","tik tok","tiktok python","tiktok username"],"max_results":10,"intent":"кратко что ищем","language_notes":"использованные языки"}}
'''
    try:
        plan = bot.call_model(bot.MODELS[0], prompt)
        queries = []
        seen = set()
        for raw in plan.get("queries", []):
            q = str(raw).strip()
            if not q or len(q) > 100:
                continue
            # Reject long natural-language queries even if the model ignores the prompt.
            if len(q.split()) > 6:
                continue
            key = q.casefold()
            if key not in seen:
                seen.add(key)
                queries.append(q)

        # Only retain the literal request when it is already a short search term.
        if len(request.split()) <= 4 and request.casefold() not in seen:
            queries.insert(0, request)

        if not queries:
            return _fallback_plan(request)
        try:
            max_results = max(1, int(plan.get("max_results", 10)))
        except Exception:
            max_results = 10
        return {
            "queries": queries,
            "max_results": max_results,
            "intent": str(plan.get("intent", request))[:500],
            "language_notes": str(plan.get("language_notes", requested_languages))[:500],
        }
    except Exception as e:
        print(f"search planner fallback: {e}", flush=True)
        return _fallback_plan(request)


async def _search_and_comments(queries):
    client = _client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("checknow Telegram session не авторизована")
        entity = await client.get_entity(bot.TG_SOURCE_CHANNEL)
        posts = {}
        per_query = 40
        for query in queries:
            try:
                async for message in client.iter_messages(entity, search=query, limit=per_query):
                    text = (message.raw_text or "").strip()
                    if text:
                        posts[message.id] = message
            except Exception as e:
                print(f"telegram search query '{query}': {e}", flush=True)

        comments = {}
        for mid, message in list(posts.items())[:MAX_RETRIEVED]:
            try:
                replies = []
                async for reply in client.iter_messages(entity, reply_to=mid, limit=MAX_COMMENTS_PER_POST):
                    text = (reply.raw_text or "").strip()
                    if text:
                        replies.append(text)
                if replies:
                    comments[mid] = replies
            except Exception as e:
                print(f"discussion {mid}: {e}", flush=True)
        return list(posts.values()), comments
    finally:
        await client.disconnect()


def _rank_hits(request, posts, comments):
    if not posts or not bot.LLM_API_KEY:
        return list(posts)
    chunks = []
    for m in posts[:MAX_RETRIEVED]:
        text = (m.raw_text or "").strip()
        reply_text = "\n".join(comments.get(m.id, [])[:12])
        chunks.append({"id": m.id, "text": text[:2500], "comments": reply_text[:2500]})
    prompt = f'''Оцени релевантность найденных Telegram-постов к запросу пользователя.
Запрос: {request}
Посты реально найдены через Telegram search. Комментарии тоже реальные.
Верни СТРОГО JSON: {{"ranking":[{{"id":123,"relevance":0-100,"reason":"..."}}]}}
Отсортируй от самых релевантных.
КАНДИДАТЫ:
{json.dumps(chunks, ensure_ascii=False)}'''
    try:
        plan = bot.call_model(bot.MODELS[0], prompt)
        ranking = plan.get("ranking", [])
        scores = {int(x["id"]): int(x.get("relevance", 0)) for x in ranking if str(x.get("id", "")).isdigit()}
        return sorted(posts, key=lambda m: scores.get(m.id, 0), reverse=True)
    except Exception as e:
        print(f"search ranking fallback: {e}", flush=True)
        return posts


def _search_context(message, comments, queries):
    text = (message.raw_text or "").strip()
    published = message.date.astimezone(timezone.utc).isoformat() if message.date else "неизвестно"
    replies = comments.get(message.id, [])
    material = f"[Поисковый запрос пользователя]\n{queries[0] if queries else ''}\n[Дата публикации Telegram: {published}]\n{text}"
    if replies:
        material += "\n\n[КОММЕНТАРИИ / DISCUSSION]\n" + "\n---\n".join(replies[:MAX_COMMENTS_PER_POST])
    return material


def _enhanced_search_result(result, message, comments):
    url = bot.telegram_url(message)
    original_url = bot.extract_linuxdo_url(message.raw_text or "")
    out = bot.format_result(result, url, original_url)
    if comments.get(message.id):
        out += f"\n💬 <b>Комментарии учтены:</b> {len(comments[message.id])}"
    return out


async def enhanced_search_now(request, limit=30):
    request = request.strip()
    if not request:
        raise RuntimeError("укажи запрос: /search найди максимум 5 Python калькуляторов")
    if len(request) > 500:
        raise RuntimeError("запрос слишком длинный (максимум 500 символов)")

    plan = await asyncio.to_thread(plan_search, request)
    queries = plan["queries"]
    requested_max = plan["max_results"]
    try:
        hard_limit = max(1, int(limit))
    except Exception:
        hard_limit = requested_max
    requested_max = min(requested_max, hard_limit)

    bot.send_message(
        "🔍 <b>AI Search запущен</b>\n\n"
        f"🧠 Запрос: <code>{html.escape(request)}</code>\n"
        f"🎯 Цель: максимум <b>{requested_max}</b> результатов\n"
        f"🔎 Поисковых фраз: <b>{len(queries)}</b>\n"
        f"🌐 Языки: <b>{html.escape(', '.join(_search_languages()))}</b>\n"
        "🔎 Ищу короткими ключевыми фразами…"
    )
    bot.send_message("<b>Стратегия:</b>\n" + "\n".join(f"• {html.escape(q)}" for q in queries))

    posts, comments = await _search_and_comments(queries)
    if not posts:
        bot.send_message("❌ <b>Ничего не найдено.</b> Попробуй более широкое описание запроса.")
        return

    ranked = await asyncio.to_thread(_rank_hits, request, posts, comments)
    candidates = ranked[:min(MAX_RETRIEVED, max(requested_max * 4, requested_max))]
    bot.send_message(
        f"📚 Telegram нашёл <b>{len(posts)}</b> уникальных постов.\n"
        f"💬 Обработаны обсуждения найденных постов.\n"
        f"🤖 Глубоко анализирую топ <b>{len(candidates)}</b>…"
    )

    results = []
    settings = bot.load_settings()
    for idx, message in enumerate(candidates, 1):
        text = (message.raw_text or "").strip()
        url = bot.telegram_url(message)
        published_at = message.date.astimezone(timezone.utc).isoformat() if message.date else "неизвестно"
        try:
            material = _search_context(message, comments, queries)
            result = await asyncio.to_thread(
                bot.analyze_without_db,
                bot.telegram_title(text),
                material,
                url,
                published_at,
                settings["mode"],
            )
            score = int(result.get("score", 0))
            results.append((score, message, result))
        except Exception as e:
            print(f"search analyze {message.id}: {e}", flush=True)
        if idx % 5 == 0:
            bot.send_message(f"⏳ Проанализировано {idx}/{len(candidates)}…")

    results.sort(key=lambda x: x[0], reverse=True)
    results = results[:requested_max]
    if not results:
        bot.send_message("❌ Найденные сообщения не удалось проанализировать.")
        return

    for score, message, result in results:
        bot.send_message(_enhanced_search_result(result, message, comments), bot.result_keyboard(message.id))
        await asyncio.sleep(0.2)

    bot.send_message(
        f"✅ <b>AI Search завершён</b>\n"
        f"🔎 Запрос: <code>{html.escape(request)}</code>\n"
        f"📚 Найдено Telegram: {len(posts)}\n"
        f"💬 Посты с обсуждениями: {sum(1 for x in comments.values() if x)}\n"
        f"🏆 Выдано результатов: <b>{len(results)}</b>"
    )
