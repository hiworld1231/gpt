#!/usr/bin/env python3
"""AI-assisted Telegram search for Linux.do Hunter.

The LLM plans search queries; Telegram performs the actual retrieval.  Results
are deduplicated, discussion replies are collected where Telegram exposes them,
and only the most relevant hits are sent to the user for analysis.
"""
import asyncio
import html
import json
import os
import re
from datetime import timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

import checknow_bot as bot


MAX_SEARCH_QUERIES = 12
MAX_RETRIEVED = 120
MAX_COMMENTS_PER_POST = 40


def _client():
    if not bot.TG_API_ID or not bot.TG_API_HASH or not bot.TG_CHECKNOW_STRING_SESSION:
        raise RuntimeError("Telegram API/session не настроены")
    return TelegramClient(
        StringSession(bot.TG_CHECKNOW_STRING_SESSION),
        int(bot.TG_API_ID),
        bot.TG_API_HASH,
    )


def _fallback_plan(request):
    return {
        "queries": [request],
        "max_results": 10,
        "language_notes": "",
        "intent": request,
    }


def plan_search(request):
    """Ask the router to turn a natural-language request into Telegram queries."""
    if not bot.LLM_API_KEY:
        return _fallback_plan(request)
    prompt = f"""Ты планировщик поиска по Telegram-каналу с IT/AI/dev материалами.
Пользователь написал естественным языком:
{request}

Сформируй поисковую стратегию. Важно: ты НЕ ищешь сам и НЕ придумываешь результаты.
Telegram будет выполнять каждый поисковый запрос отдельно.

Нужно:
- понять, что именно хочет пользователь;
- извлечь максимум результатов, если пользователь его указал (иначе 10);
- сделать до {MAX_SEARCH_QUERIES} коротких поисковых фраз;
- включить точную фразу/термины пользователя;
- добавить близкие синонимы, названия технологий и разговорные формулировки;
- для технических тем добавить английские варианты;
- при необходимости добавить русские и английские варианты терминов отдельно;
- искать не только очевидное слово, но и формулировки, которыми люди могли описывать тот же способ/инструмент/проблему.

НЕ добавляй слишком общие слова вроде "code", "python", "api", если они сами по себе не помогают найти нужное.

Верни СТРОГО JSON:
{{"queries":["..."],"max_results":10,"intent":"кратко что ищем","language_notes":"какие языки/синонимы использованы"}}
"""
    try:
        plan = bot.call_model(bot.MODELS[0], prompt)
        queries = []
        for q in plan.get("queries", []):
            q = str(q).strip()
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q[:100])
        if request.lower() not in {x.lower() for x in queries}:
            queries.insert(0, request[:100])
        try:
            max_results = max(1, min(int(plan.get("max_results", 10)), 30))
        except Exception:
            max_results = 10
        return {
            "queries": queries[:MAX_SEARCH_QUERIES],
            "max_results": max_results,
            "intent": str(plan.get("intent", request))[:500],
            "language_notes": str(plan.get("language_notes", ""))[:500],
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
        per_query = max(10, min(MAX_RETRIEVED, 40))
        for query in queries:
            try:
                async for message in client.iter_messages(entity, search=query, limit=per_query):
                    text = (message.raw_text or "").strip()
                    if text:
                        posts[message.id] = message
            except Exception as e:
                print(f"telegram search query '{query}': {e}", flush=True)

        # Search discussion replies for every candidate. Telethon can expose
        # replies to channel posts via reply_to; failures are intentionally non-fatal.
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
    """Use the LLM only for relevance/ranking, not for retrieval."""
    if not posts or not bot.LLM_API_KEY:
        return list(posts)
    chunks = []
    for m in posts[:MAX_RETRIEVED]:
        text = (m.raw_text or "").strip()
        reply_text = "\n".join(comments.get(m.id, [])[:12])
        chunks.append({"id": m.id, "text": text[:2500], "comments": reply_text[:2500]})
    prompt = f"""Оцени релевантность найденных Telegram-постов к запросу пользователя.
Запрос пользователя: {request}

Посты были реально найдены Telegram search. Не утверждай, что пост содержит то, чего нет в тексте.
Комментарии тоже реальны и могут уточнять, исправлять или подтверждать пост.

Верни СТРОГО JSON: {{"ranking":[{{"id":123,"relevance":0-100,"reason":"..."}}]}}
Отсортируй от самых релевантных к наименее релевантным.

КАНДИДАТЫ:
{json.dumps(chunks, ensure_ascii=False)}"""
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
    material = (
        f"[Поисковый запрос пользователя]\n{queries[0] if queries else ''}\n"
        f"[Дата публикации Telegram: {published}]\n{text}"
    )
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
    hard_limit = max(1, min(int(limit), 30))
    requested_max = min(requested_max, hard_limit)

    bot.send_message(
        "🔍 <b>AI Search запущен</b>\n\n"
        f"🧠 Запрос: <code>{html.escape(request)}</code>\n"
        f"🎯 Цель: максимум <b>{requested_max}</b> результатов\n"
        f"🔎 Поисковых фраз: <b>{len(queries)}</b>\n"
        "🌐 Ищу на русском + английском и по синонимам…"
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
            # Search is semantic: don't apply the global hunter threshold here.
            # The user asked for search results, so relevance/score is shown directly.
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
