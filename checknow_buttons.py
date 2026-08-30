#!/usr/bin/env python3
"""Telegram bot compatibility/extensions.

Adds working Deep Check / Verify callbacks, AI-planned multi-source Telegram
search, persistent /source management, discussion retrieval, and the Telegram
slash-command menu shown when the user types '/'.
"""
import asyncio
import html
import json
import threading

from telethon import TelegramClient
from telethon.sessions import StringSession

import checknow_bot as bot
import search_engine
import source_manager

_LOCK = threading.Lock()


def _sources():
    return source_manager.get_sources(bot)


def _fetch_message(message_id, source=None):
    async def run():
        if not bot.TG_API_ID or not bot.TG_API_HASH:
            raise RuntimeError("TG_API_ID/TG_API_HASH не настроены")
        if not bot.TG_CHECKNOW_STRING_SESSION:
            raise RuntimeError("TG_CHECKNOW_STRING_SESSION не настроен")
        client = TelegramClient(StringSession(bot.TG_CHECKNOW_STRING_SESSION), int(bot.TG_API_ID), bot.TG_API_HASH)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("checknow Telegram session не авторизована")
            entity = await client.get_entity(source or bot.TG_SOURCE_CHANNEL)
            msg = await client.get_messages(entity, ids=int(message_id))
            if not msg or not (msg.raw_text or "").strip():
                raise RuntimeError(f"пост {message_id} не найден")
            return msg
        finally:
            await client.disconnect()
    return asyncio.run(run())


def _run_detail(source_id, callback_message_id, task):
    if not _LOCK.acquire(blocking=False):
        bot.edit_message(callback_message_id, "⏳ <b>Другая проверка уже выполняется.</b>", bot.kb([[{"text": "⬅️ Меню", "callback_data": "menu:main"}]]))
        return
    try:
        bot.edit_message(callback_message_id, "⏳ <b>Проверяю…</b>\n\nПолучаю исходный пост и запускаю расширенный анализ.")
        message = _fetch_message(source_id)
        text = (message.raw_text or "").strip()
        url = bot.telegram_url(message)
        published = message.date.astimezone(bot.timezone.utc).isoformat() if message.date else "неизвестно"
        original_url = bot.extract_linuxdo_url(text)
        original_title, original_text = ("", "")
        if original_url:
            original_title, original_text = bot.fetch_linuxdo_thread(original_url)
        material = f"[Дата публикации Telegram: {published}]\n{text}"
        if original_text:
            material += f"\n\n[ОРИГИНАЛЬНЫЙ LINUX.DO ТРЕД]\n{original_text}"
        base_prompt = bot.read_prompt()
        mode = bot.load_settings()["mode"]
        if task == "deep":
            task_note = "Deep Check: подробно разложи утверждение по фактам, условиям, ограничениям, доказательствам, противоречиям и практической воспроизводимости. Не выдумывай внешние проверки."
            heading = "🔍 <b>Deep Check</b>"
        else:
            task_note = "Verify: строго оцени доказательства работоспособности. Раздели на доказано автором, косвенно подтверждено и не подтверждено; учитывай доступные комментарии."
            heading = "✅ <b>Verify</b>"
        prompt = f"""{base_prompt}\n\nРЕЖИМ: {mode}\n{bot.MODES.get(mode, bot.MODES['aggressive'])}\nЗАДАЧА: {task_note}\nТЕКУЩЕЕ UTC: {bot.datetime.now(bot.timezone.utc).isoformat()}\n\nВерни СТРОГО JSON без markdown. Поля: score (0-100), is_new (boolean), is_working (boolean), category (UPPER_SNAKE_CASE), summary (до 500 символов, русский), why (до 500), how (до 1000; только из материала), risk (до 500), evidence (до 1000), verdict (до 500).\n\nИсточник: {url}\nЗаголовок: {original_title or bot.telegram_title(text)}\n\nПОЛНЫЙ ДОСТУПНЫЙ МАТЕРИАЛ:\n{material[:50000]}"""
        last_error = None
        result = None
        for model in bot.MODELS:
            try:
                result = bot.call_model(model, prompt)
                break
            except RuntimeError as e:
                last_error = str(e)
                if last_error != "RATE_LIMIT":
                    raise
        if result is None:
            raise RuntimeError(f"all models unavailable: {last_error}")
        score = int(result.get("score", 0))
        status = "🟢 подтверждено" if result.get("is_working") else "🟡 требует проверки"
        novelty = "🆕 новое" if result.get("is_new") else "♻️ уже известное"
        out = (
            f"{heading}\n\n<b>{score}/100</b> · {status} · {novelty}\n"
            f"🏷 {html.escape(str(result.get('category', 'OTHER')))}\n\n"
            f"💎 <b>{html.escape(str(result.get('summary', '')))}</b>\n\n"
            f"💰 <b>Почему:</b> {html.escape(str(result.get('why', '')))}\n"
            f"🛠 <b>Как:</b> {html.escape(str(result.get('how', '')))}\n"
            f"⚠️ <b>Риск/лимиты:</b> {html.escape(str(result.get('risk', '')))}\n"
            f"📚 <b>Доказательства:</b> {html.escape(str(result.get('evidence', '')))}\n"
            f"⚖️ <b>Вердикт:</b> {html.escape(str(result.get('verdict', '')))}\n\n"
            f"🔗 {html.escape(url)}"
        )
        if original_url:
            out += f"\n🔎 <b>Оригинал:</b> {html.escape(original_url)}"
        bot.edit_message(callback_message_id, out, bot.result_keyboard(source_id))
    except Exception as e:
        print(f"{task} {source_id}: {e}", flush=True)
        try:
            bot.edit_message(callback_message_id, f"❌ <b>{task.title()} не удался</b>\n\n<code>{html.escape(str(e)[:1000])}</code>", bot.result_keyboard(source_id))
        except Exception:
            pass
    finally:
        _LOCK.release()


async def _multi_search_and_comments(queries):
    client = TelegramClient(StringSession(bot.TG_CHECKNOW_STRING_SESSION), int(bot.TG_API_ID), bot.TG_API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("checknow Telegram session не авторизована")
        posts = {}
        comments = {}
        per_query = max(10, min(search_engine.MAX_RETRIEVED, 40))
        sources = _sources()
        print(f"search sources: {', '.join('@' + s for s in sources)}", flush=True)
        for source in sources:
            entity = await client.get_entity(source)
            for query in queries:
                try:
                    async for message in client.iter_messages(entity, search=query, limit=per_query):
                        text = (message.raw_text or "").strip()
                        if text:
                            posts[(source, message.id)] = (message, source)
                except Exception as e:
                    print(f"telegram search @{source} '{query}': {e}", flush=True)

        for (source, mid), (message, _) in list(posts.items())[:search_engine.MAX_RETRIEVED]:
            try:
                entity = await client.get_entity(source)
                replies = []
                async for reply in client.iter_messages(entity, reply_to=mid, limit=search_engine.MAX_COMMENTS_PER_POST):
                    text = (reply.raw_text or "").strip()
                    if text:
                        replies.append(text)
                if replies:
                    comments[(source, mid)] = replies
            except Exception as e:
                print(f"discussion @{source} {mid}: {e}", flush=True)
        # Keep source metadata on the message object so existing ranking/analysis can use it.
        result = []
        for (source, mid), (message, _) in posts.items():
            try:
                message._hunter_source = source
            except Exception:
                pass
            result.append(message)
        return result, comments
    finally:
        await client.disconnect()


# Replace the retrieval stage used by the existing AI search engine. The LLM still
# plans/ranks; Telegram performs the actual search for every configured source.
search_engine._search_and_comments = _multi_search_and_comments


def _multi_search_context(message, comments, queries):
    source = getattr(message, "_hunter_source", bot.TG_SOURCE_CHANNEL)
    text = (message.raw_text or "").strip()
    published = message.date.astimezone(bot.timezone.utc).isoformat() if message.date else "неизвестно"
    replies = comments.get((source, message.id), [])
    material = (
        f"[Поисковый запрос пользователя]\n{queries[0] if queries else ''}\n"
        f"[Telegram-источник]\n@{source}\n"
        f"[Дата публикации Telegram: {published}]\n{text}"
    )
    if replies:
        material += "\n\n[КОММЕНТАРИИ / DISCUSSION]\n" + "\n---\n".join(replies[:search_engine.MAX_COMMENTS_PER_POST])
    return material


def _multi_result(result, message, comments):
    source = getattr(message, "_hunter_source", bot.TG_SOURCE_CHANNEL)
    url = f"https://t.me/{source}/{message.id}"
    original_url = bot.extract_linuxdo_url(message.raw_text or "")
    out = bot.format_result(result, url, original_url)
    count = len(comments.get((source, message.id), []))
    if count:
        out += f"\n💬 <b>Комментарии учтены:</b> {count}"
    out += f"\n📡 <b>Источник:</b> @{html.escape(source)}"
    return out


# Patch the two helper functions used by enhanced_search_now without duplicating its
# AI planning/ranking pipeline.
search_engine._search_context = _multi_search_context
search_engine._enhanced_search_result = _multi_result


def fixed_handle_callback(update):
    cq = update.get("callback_query") or {}
    data = cq.get("data", "")
    message = cq.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != bot.CHAT_ID:
        return
    print(f"callback received: {data}", flush=True)
    if data.startswith("deep:") or data.startswith("verify:"):
        try:
            source_id = int(data.split(":", 1)[1])
            task = "deep" if data.startswith("deep:") else "verify"
            bot.answer_callback(cq["id"], "Deep Check запущен" if task == "deep" else "Verify запущен")
            threading.Thread(target=_run_detail, args=(source_id, message.get("message_id"), task), daemon=True).start()
        except Exception as e:
            print(f"detail callback: {e}", flush=True)
        return
    return _original_handle_callback(update)


_original_handle_callback = bot.handle_callback
bot.handle_callback = fixed_handle_callback
bot.search_now = search_engine.enhanced_search_now


COMMANDS = [
    {"command": "menu", "description": "панель управления"},
    {"command": "checknow", "description": "проверить последние посты"},
    {"command": "search", "description": "AI-поиск по Telegram"},
    {"command": "source", "description": "добавить/удалить источник поиска"},
    {"command": "sources", "description": "список источников поиска"},
    {"command": "score", "description": "изменить порог Score"},
    {"command": "limit", "description": "изменить лимит проверки"},
    {"command": "mode", "description": "изменить режим охоты"},
    {"command": "status", "description": "статус бота"},
    {"command": "settings", "description": "настройки"},
    {"command": "prompt", "description": "показать prompt.txt"},
    {"command": "help", "description": "помощь"},
]


def _install_command_menu():
    try:
        bot.api_call("setMyCommands", {"commands": COMMANDS})
        print("Telegram slash-command menu installed", flush=True)
    except Exception as e:
        print(f"setMyCommands: {e}", flush=True)


def _handle_source_command(raw):
    parts = raw.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "list"
    try:
        if sub in ("list", "ls"):
            sources = _sources()
            bot.send_message("📡 <b>Источники поиска</b>\n\n" + "\n".join(f"{i}. @{s}" for i, s in enumerate(sources, 1)))
        elif sub in ("add", "+") and len(parts) >= 3:
            added, sources = source_manager.add_source(bot, parts[2])
            if added:
                bot.send_message("✅ Источник добавлен: <b>@%s</b>\n\n%s" % (html.escape(parts[2].lstrip("@")), "\n".join(f"{i}. @{s}" for i, s in enumerate(sources, 1))))
            else:
                bot.send_message("ℹ️ Этот источник уже есть в списке.")
        elif sub in ("remove", "rm", "-", "del") and len(parts) >= 3:
            removed, sources = source_manager.remove_source(bot, parts[2])
            bot.send_message(("🗑 Источник удалён." if removed else "ℹ️ Источник не найден.") + "\n\n" + "\n".join(f"{i}. @{s}" for i, s in enumerate(sources, 1)))
        else:
            bot.send_message("📡 <b>Источники поиска</b>\n\n<code>/source list</code>\n<code>/source add @channel</code>\n<code>/source remove @channel</code>\n\nОсновной источник удалить нельзя.")
    except Exception as e:
        bot.send_message(f"❌ /source: <code>{html.escape(str(e)[:700])}</code>")


_original_get_updates = bot.get_updates

def _get_updates_with_source(offset=None):
    updates = _original_get_updates(offset)
    keep = []
    for update in updates:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id")) == bot.CHAT_ID:
            raw = (message.get("text") or "").strip()
            command = raw.split()[0].lower() if raw else ""
            if command in ("/source", "/sources"):
                _handle_source_command(raw if command == "/source" else "/source list")
                continue
        keep.append(update)
    return keep

bot.get_updates = _get_updates_with_source


if __name__ == "__main__":
    _install_command_menu()
    print("Telegram command bot extensions active: /source /sources + multi-source search", flush=True)
    bot.main()
