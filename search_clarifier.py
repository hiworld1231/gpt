#!/usr/bin/env python3
"""AI clarification + minimal entity-first query planner for Telegram Search."""
import html
import os
import secrets
import threading
import checknow_bot as bot

_pending = {}
_lock = threading.Lock()
_LANG = {"zh":"Chinese", "en":"English", "ru":"Russian", "ja":"Japanese", "ko":"Korean"}


def _languages():
    raw = os.getenv("SEARCH_LANGUAGES", "zh,en").strip()
    return [x.strip().lower() for x in raw.split(",") if x.strip().lower() in _LANG] or ["zh", "en"]


def _call(prompt, fallback):
    if not bot.LLM_API_KEY:
        return fallback
    try:
        data = bot.call_model(bot.MODELS[0], prompt)
        return data if isinstance(data, dict) else fallback
    except Exception as e:
        print(f"search AI fallback: {e}", flush=True)
        return fallback


def clarify(request):
    langs = ", ".join(_LANG[x] for x in _languages())
    prompt = f'''You are a clarification agent for a Telegram historical search.
User request: {request}
Search languages: {langs}.
Do NOT create search queries yet. Decide if one human clarification would materially improve THIS search.
If there are multiple plausible targets, unclear scope, or an ambiguous important term, ask exactly ONE specific question generated from the user's request and give 2-5 concise inline answers. The last answer MUST be "🔎 Всё".
If the request is already clear, return clarification_needed=false.
Return strict JSON only: {{"clarification_needed":true,"question":"...","options":[{{"label":"...","value":"..."}}]}} or {{"clarification_needed":false}}'''
    data = _call(prompt, {"clarification_needed": False})
    options = []
    for x in data.get("options", []):
        if isinstance(x, dict):
            label, value = str(x.get("label", "")).strip()[:48], str(x.get("value", "")).strip()[:300]
            if label and value: options.append({"label": label, "value": value})
    return {"clarification_needed": bool(data.get("clarification_needed")) and bool(str(data.get("question", "")).strip()) and bool(options), "question": str(data.get("question", "")).strip()[:700], "options": options[:5]}


def plan_search(request):
    """Generate entity-first queries. Do not manufacture modifiers or permutations."""
    if not bot.LLM_API_KEY:
        return {"queries": [request], "intent": request}
    langs = ", ".join(_LANG[x] for x in _languages())
    prompt = f'''You are a Telegram search-query planner.
User request: {request}
Allowed languages: {langs}. If Russian is not listed, NEVER output Russian.

Your job is NOT to paraphrase the request and NOT to invent search intents. Extract the concrete names/entities/topics the user wants searched.

QUERY STRATEGY:
1. For EVERY important named entity, ALWAYS output the entity ALONE first. Examples: stripe, cursor, tiktok, tik tok, python.
2. Add spelling variants separately when they are genuinely used: tiktok and tik tok; Chinese equivalents such as 抖音 when appropriate.
3. Then add only a SMALL number of directly useful combinations that the user's request clearly calls for. Examples: stripe python, cursor python, tiktok api, tik tok api, tiktok username, tik tok username, tiktok abuse, tik tok abuse.
4. Keep the entity-alone queries. They are intentionally broad and are often more useful than long phrases.
5. If the user asks for abuse, "abuse" may be used as a modifier. Do NOT automatically add exploit/hack/bypass/attack/vulnerability/spam/flood/manipulation/injection/automation/etc.
6. NEVER create Cartesian products. Do not combine every entity with every modifier. Do not reverse words just to create more queries.
7. Do not add "code", "script", "tutorial", "example", "github", "python", or similar to a query unless the user explicitly wants that concept for that entity. Python itself is an entity if the user named Python.
8. Do not translate the user's whole sentence. Search using short natural terms likely to occur in posts.
9. Chinese queries must be natural Chinese search terms, not English words glued to Chinese. Keep English brand names when they are commonly used that way.
10. Each query must be 1-3 terms. One-term entity queries are REQUIRED.
11. No artificial maximum query count. Return every genuinely useful entity/variant/combo, but do not pad the list.

For the example request "find abuse stuff for cursor, stripe and tiktok username/api", a GOOD plan is roughly:
stripe
cursor
python
stripe python
cursor python
tiktok
tik tok
抖音
tiktok api
tik tok api
tiktok username
tik tok username
tiktok abuse
tik tok abuse

A BAD plan contains dozens of combinations like "python cursor flood", "python cursor attack", "cursor manipulation", etc.
Return strict JSON: {{"queries":[...],"intent":"..."}}'''
    data = _call(prompt, {"queries": [request], "intent": request})
    queries, seen = [], set()
    for raw in data.get("queries", []):
        q = str(raw).strip()
        if not q or len(q) > 80 or len(q.split()) > 3:
            continue
        k = q.casefold()
        if k not in seen:
            seen.add(k); queries.append(q)
    return {"queries": queries or [request], "intent": str(data.get("intent", request))[:1000]}


def _ask(request, plan, original):
    token = secrets.token_urlsafe(8)
    buttons, values = [], []
    for item in plan.get("options", []):
        values.append(item["value"])
        buttons.append({"text": item["label"], "callback_data": f"searchclarify:{token}:{len(values)-1}"})
    if not buttons: return False
    with _lock: _pending[token] = {"request": request, "values": values, "original": original}
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([{"text":"🚀 Искать без уточнения", "callback_data":f"searchclarify:{token}:skip"}])
    bot.send_message("❓ <b>ИИ хочет уточнить запрос</b>\n\n" + html.escape(plan["question"]), bot.kb(rows))
    return True


def start_or_search(request, original):
    request = request.strip()
    plan = clarify(request)
    if plan.get("clarification_needed") and _ask(request, plan, original):
        return
    return original(request)


def _callback(update):
    cq = update.get("callback_query") or {}; data = str(cq.get("data", ""))
    if not data.startswith("searchclarify:"): return False
    parts = data.split(":", 2)
    if len(parts) != 3: return True
    token, choice = parts[1], parts[2]
    with _lock: state = _pending.pop(token, None)
    if not state:
        bot.answer_callback(cq.get("id"), "Уточнение устарело. Напиши /search заново.", alert=True); return True
    if choice == "skip": extra = "Ищи широко по исходному запросу."
    else:
        try: extra = state["values"][int(choice)]
        except Exception: extra = "Ищи широко по исходному запросу."
    bot.answer_callback(cq.get("id"), "Принято. ИИ строит поиск…")
    request = state["request"] + "\n\n[УТОЧНЕНИЕ ПОЛЬЗОВАТЕЛЯ]\n" + extra
    threading.Thread(target=state["original"], args=(request,), daemon=True).start()
    return True

_original = bot.handle_callback

def _handle_callback(update):
    if _callback(update): return
    return _original(update)

bot.handle_callback = _handle_callback
