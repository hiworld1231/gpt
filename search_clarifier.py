#!/usr/bin/env python3
"""Focused Telegram-search planner + optional clarification UI."""
import html
import os
import secrets
import threading

import checknow_bot as bot

_pending = {}
_lock = threading.Lock()
_ALLOWED = {"zh": "简体中文", "en": "English", "ru": "Русский", "ja": "日本語", "ko": "한국어"}


def _languages():
    raw = os.getenv("SEARCH_LANGUAGES", "zh,en").strip()
    out = [x.strip().lower() for x in raw.split(",") if x.strip() in _ALLOWED]
    return out or ["zh", "en"]


def plan_search(request):
    """Generate focused short queries. No invented spam/flood/etc terms."""
    if not bot.LLM_API_KEY:
        return {"queries": [request], "intent": request, "clarification_needed": False}
    langs = ", ".join(_ALLOWED[x] for x in _languages())
    prompt = f'''Ты планировщик ПОИСКА по Telegram. Пользователь хочет найти информацию внутри каналов/форумов.
Запрос пользователя:
{request}

Языки источника: {langs}. Если русский не входит в список — НЕ генерируй русский.

Сделай ШИРОКУЮ, но ОСМЫСЛЕННУЮ сетку коротких поисковых фраз.
КЛЮЧЕВОЕ ПРАВИЛО: не придумывай действия/темы, которых пользователь не просил.
Например, если он просит TikTok abuse, допустимы tiktok, tik tok, tiktok abuse, tik tok abuse, tiktok api, tik tok api, tiktok username, tik tok username и их полезные комбинации. Но НЕ добавляй spam, flood, attack, manipulation, injection, automation, vulnerability, hack, exploit, bypass только потому, что они часто встречаются. Такие термины добавляй ТОЛЬКО если пользователь сам явно просил соответствующую тему или она явно является частью его цели.

Правила:
- Всегда ищи базовую сущность отдельно: tiktok и tik tok.
- Делай альтернативные написания отдельно.
- Делай короткие комбинации 1-3 терминов, когда комбинация имеет смысл.
- Не превращай каждый термин в декартово произведение со всеми остальными.
- Не добавляй шумные синонимы ради количества.
- Если пользователь просит abuse/выгоду/приколы/коды — сохраняй именно этот смысл.
- Китайский должен быть настоящим китайским, например TikTok -> 抖音; английские термины можно использовать только если это реально употребляемый поисковый термин.
- Не переводи русский запрос дословно.
- Количество запросов НЕ ограничивай искусственно. Но каждый запрос должен иметь самостоятельную поисковую ценность.

Верни строго JSON:
{{"queries":[...],"intent":"краткая цель поиска","clarification_needed":true,"question":"один короткий уточняющий вопрос","options":[{{"label":"...","value":"..."}}]}}
clarification_needed=true только если ответ пользователя реально изменит поисковую стратегию. Если запрос уже достаточно точный — false.
Для options дай 2-5 коротких вариантов и последний вариант "🔎 Всё" со значением "всё".'''
    try:
        data = bot.call_model(bot.MODELS[0], prompt)
        queries, seen = [], set()
        for raw in data.get("queries", []):
            q = str(raw).strip()
            if not q or len(q) > 100 or len(q.split()) > 4:
                continue
            k = q.casefold()
            if k not in seen:
                seen.add(k)
                queries.append(q)
        if not queries:
            queries = [request]
        options = data.get("options", []) if isinstance(data.get("options", []), list) else []
        return {
            "queries": queries,
            "intent": str(data.get("intent", request))[:1000],
            "clarification_needed": bool(data.get("clarification_needed", False)),
            "question": str(data.get("question", "")).strip()[:500],
            "options": options[:5],
        }
    except Exception as e:
        print(f"focused search planner fallback: {e}", flush=True)
        return {"queries": [request], "intent": request, "clarification_needed": False}


def _ask(request, plan, original):
    token = secrets.token_urlsafe(8)
    options = []
    values = []
    for i, item in enumerate(plan.get("options", [])):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()[:40]
        value = str(item.get("value", "")).strip()[:200]
        if label and value:
            options.append({"text": label, "callback_data": f"searchclarify:{token}:{len(values)}"})
            values.append(value)
    if not options:
        return False
    with _lock:
        _pending[token] = {"request": request, "values": values, "original": original}
    rows = [options[i:i + 2] for i in range(0, len(options), 2)]
    rows.append([{"text": "🚀 Искать как есть", "callback_data": f"searchclarify:{token}:skip"}])
    bot.send_message(
        "❓ <b>Уточнить поиск?</b>\n\n" + html.escape(plan.get("question") or "Что именно для тебя важнее?"),
        bot.kb(rows),
    )
    return True


def start_or_search(request, original):
    request = request.strip()
    plan = plan_search(request)
    if plan.get("clarification_needed") and plan.get("question") and plan.get("options"):
        if _ask(request, plan, original):
            bot.send_message("🧠 Я пока <b>не запускаю поиск</b>. Выбери направление выше — после этого построю поисковые фразы и начну поиск.")
            return
    return original(request, None)


def _callback(update):
    cq = update.get("callback_query") or {}
    data = str(cq.get("data", ""))
    if not data.startswith("searchclarify:"):
        return False
    parts = data.split(":", 2)
    if len(parts) != 3:
        return True
    token, choice = parts[1], parts[2]
    with _lock:
        state = _pending.pop(token, None)
    if not state:
        try:
            bot.answer_callback(cq.get("id"), "Уточнение устарело. Напиши /search заново.", alert=True)
        except Exception:
            pass
        return True
    if choice == "skip":
        extra = "Без дополнительного уточнения; ищи по исходному запросу максимально широко."
    else:
        try:
            extra = f"Пользователь уточнил направление поиска: {state['values'][int(choice)]}"
        except Exception:
            extra = "Пользователь выбрал максимально широкий поиск."
    try:
        bot.answer_callback(cq.get("id"), "Принято. Запускаю поиск…")
    except Exception:
        pass
    request = state["request"] + "\n\n[УТОЧНЕНИЕ ПОЛЬЗОВАТЕЛЯ]\n" + extra
    threading.Thread(target=state["original"], args=(request, None), daemon=True).start()
    return True


_original = bot.handle_callback


def _handle_callback(update):
    if _callback(update):
        return
    return _original(update)


bot.handle_callback = _handle_callback
