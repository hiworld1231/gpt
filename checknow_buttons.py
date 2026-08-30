#!/usr/bin/env python3
"""Compatibility patch for Telegram result buttons and AI Search.
Loads the existing checknow bot, fixes Deep Check / Verify callbacks, and
replaces /search with an AI-planned Telegram search that also checks discussions.
"""
import asyncio
import html
import threading

from telethon import TelegramClient
from telethon.sessions import StringSession

import checknow_bot as bot
import search_engine

_LOCK = threading.Lock()


def _fetch_message(message_id):
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
            entity = await client.get_entity(bot.TG_SOURCE_CHANNEL)
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
            task_note = "Verify: строго оцени доказательства работоспособности. Раздели на доказано автором, косвенно подтверждено и не подтверждено; учитывай комментарии оригинального Linux.do треда."
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


def fixed_handle_callback(update):
    cq = update.get("callback_query") or {}
    data = cq.get("data", "")
    message = cq.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != bot.CHAT_ID:
        return
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

# /search is handled by checknow_bot.main(), so monkey-patch the function it resolves.
# The search engine itself uses Telegram search for retrieval; the LLM only expands
# and ranks the query, then comments/discussions are added as analysis material.
bot.search_now = search_engine.enhanced_search_now


if __name__ == "__main__":
    bot.main()
