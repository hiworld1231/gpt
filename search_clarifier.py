#!/usr/bin/env python3
"""AI clarification + focused query planner for Telegram Search."""
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
    """Generate broad but relevant 1-3 term queries. Never invent unrelated actions."""
    if not bot.LLM_API_KEY:
        return {"queries": [request], "intent": request}
    langs = ", ".join(_LANG[x] for x in _languages())
    prompt = f'''You are a Telegram search-query planner.
User request: {request}
Allowed languages: {langs}. If Russian is not listed, NEVER output Russian.
Create a broad set of SHORT search queries. Telegram runs each query separately.

Rules:
- First extract every explicit entity/topic the user actually named.
- Always include each important entity alone and spelling variants. Example: tiktok, tik tok.
- Add useful 2-3 term combinations only when they preserve the user's intent: tiktok api, tik tok api, tiktok username, tik tok username, tiktok abuse, tik tok abuse.
- If the user explicitly asks for abuse, include abuse variants; do not silently replace that with generic hacking vocabulary.
- NEVER invent spam, flood, attack, manipulation, injection, automation, vulnerability, exploit, bypass, hack, giveaway, etc. unless that concept is explicitly requested or is a direct synonym clearly present in the user's wording.
- Do NOT make a Cartesian product. Do not produce permutations just to increase the count.
- Do not translate the whole sentence. Use natural search terms used on the source.
- For Chinese use real Chinese terms where appropriate (e.g. TikTok -> 抖音). Do not mix English modifiers into fake Chinese phrases.
- Queries must be 1-3 words/terms. No sentences.
- No artificial query-count limit: return as many genuinely useful distinct queries as needed.
Return strict JSON: {{"queries":["tiktok","tik tok","tiktok api","tik tok api","tiktok username","tik tok username","tiktok abuse","tik tok abuse"],"intent":"..."}}'''
    data = _call(prompt, {"queries": [request], "intent": request})
    queries, seen = [], set()
    for raw in data.get("queries", []):
        q = str(raw).strip()
        if not q or len(q) > 100 or len(q.split()) > 3: continue
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
        bot.send_message("🧠 Поиск пока не запущен. После ответа ИИ заново построит поисковые фразы.")
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
