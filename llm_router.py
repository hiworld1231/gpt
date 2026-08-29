#!/usr/bin/env python3
import json
import os
import random
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

PORT = int(os.getenv("LLM_ROUTER_PORT", "8099"))
STATE_DB = os.getenv("LLM_ROUTER_STATE_DB", "/var/lib/linuxdo-hunter/router_state.db")
COOLDOWN_DEFAULT = int(os.getenv("LLM_ROUTER_COOLDOWN", "60"))

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
}

DEFAULT_MODELS = [
    "groq:openai/gpt-oss-120b",
    "cerebras:gpt-oss-120b",
    "gemini:gemini-2.5-flash-lite",
    "mistral:mistral-small-latest",
]
MODELS = [x.strip() for x in os.getenv("LLM_ROUTER_MODELS", ",".join(DEFAULT_MODELS)).split(",") if x.strip()]

# Public baseline limits. Real response headers/account limits always win when available.
# These are intentionally conservative for free/trial access.
BASELINE = {
    "groq:openai/gpt-oss-120b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000, "priority": 8},
    "cerebras:gpt-oss-120b": {"rpm": 5, "rpd": None, "tpm": 30000, "tpd": 1000000, "priority": 10},
    "gemini:gemini-2.5-flash-lite": {"rpm": None, "rpd": 1500, "tpm": None, "tpd": None, "priority": 7},
    # Mistral limits are organization/model specific; headers are authoritative.
    "mistral:mistral-small-latest": {"rpm": None, "rpd": None, "tpm": None, "tpd": None, "priority": 5},
}


def db():
    os.makedirs(os.path.dirname(STATE_DB), exist_ok=True)
    con = sqlite3.connect(STATE_DB, check_same_thread=False)
    con.execute("""create table if not exists provider_state (
        model text primary key,
        cooldown_until real default 0,
        disabled_until real default 0,
        last_error text default '',
        requests_min integer default 0,
        requests_day integer default 0,
        tokens_min integer default 0,
        tokens_day integer default 0,
        window_min integer default 0,
        day_key text default '',
        remaining_requests real,
        limit_requests real,
        remaining_tokens real,
        limit_tokens real,
        reset_requests real,
        reset_tokens real,
        last_ok real default 0,
        last_latency real default 0
    )""")
    con.commit()
    return con


CON = db()


def ensure_model(model):
    CON.execute("insert or ignore into provider_state(model) values (?)", (model,))
    CON.commit()


def reset_windows(row):
    now = time.time()
    minute = int(now // 60)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if row["window_min"] != minute or row["day_key"] != day:
        CON.execute("update provider_state set requests_min=0,tokens_min=0,window_min=?,requests_day=case when day_key=? then requests_day else 0 end,tokens_day=case when day_key=? then tokens_day else 0 end,day_key=? where model=?", (minute, day, day, day, row["model"]))
        CON.commit()
        return load(row["model"])
    return row


def load(model):
    ensure_model(model)
    cur = CON.execute("select model,cooldown_until,disabled_until,last_error,requests_min,requests_day,tokens_min,tokens_day,window_min,day_key,remaining_requests,limit_requests,remaining_tokens,limit_tokens,reset_requests,reset_tokens,last_ok,last_latency from provider_state where model=?", (model,))
    r = cur.fetchone()
    keys = ["model","cooldown_until","disabled_until","last_error","requests_min","requests_day","tokens_min","tokens_day","window_min","day_key","remaining_requests","limit_requests","remaining_tokens","limit_tokens","reset_requests","reset_tokens","last_ok","last_latency"]
    return reset_windows(dict(zip(keys, r)))


def save(model, **fields):
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    CON.execute(f"update provider_state set {sets} where model=?", (*fields.values(), model))
    CON.commit()


def headers_number(headers, name):
    try:
        return float(headers.get(name)) if headers.get(name) is not None else None
    except Exception:
        return None


def learn_headers(model, headers):
    # Groq exposes these directly. Other providers may expose compatible headers.
    save(model,
         remaining_requests=headers_number(headers, "x-ratelimit-remaining-requests"),
         limit_requests=headers_number(headers, "x-ratelimit-limit-requests"),
         remaining_tokens=headers_number(headers, "x-ratelimit-remaining-tokens"),
         limit_tokens=headers_number(headers, "x-ratelimit-limit-tokens"),
         reset_requests=headers_number(headers, "x-ratelimit-reset-requests"),
         reset_tokens=headers_number(headers, "x-ratelimit-reset-tokens"))


def mark_usage(model, response, body, ok=True, latency=0):
    usage = {}
    try:
        usage = response.json().get("usage", {})
    except Exception:
        pass
    total = int(usage.get("total_tokens") or 0)
    row = load(model)
    save(model,
         requests_min=row["requests_min"] + 1,
         requests_day=row["requests_day"] + 1,
         tokens_min=row["tokens_min"] + total,
         tokens_day=row["tokens_day"] + total,
         last_ok=time.time() if ok else row["last_ok"],
         last_latency=latency)
    learn_headers(model, response.headers)


def request_provider(provider, name, body):
    base, keyname = PROVIDERS[provider]
    key = os.getenv(keyname, "")
    if not key:
        raise RuntimeError(f"{provider} key missing")
    payload = dict(body)
    payload["model"] = name
    # Keep requests bounded so one huge estimate does not unnecessarily burn a free bucket.
    payload.setdefault("max_completion_tokens", 1200)
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/hiworld1231/gpt"
        headers["X-Title"] = "Linux.do Hunter"
    return requests.post(base + "/chat/completions", headers=headers, json=payload, timeout=120)


def baseline(model, key):
    return BASELINE.get(model, {}).get(key)


def candidate_score(model):
    row = load(model)
    now = time.time()
    if now < row["cooldown_until"] or now < row["disabled_until"]:
        return -1
    provider = model.split(":", 1)[0]
    key = PROVIDERS.get(provider)
    if not key or not os.getenv(key[1], ""):
        return -1

    b = BASELINE.get(model, {})
    score = float(b.get("priority", 5))

    # Prefer providers with more observed remaining quota.
    ratios = []
    if row["remaining_tokens"] is not None and row["limit_tokens"]:
        ratios.append(max(0.0, min(1.0, row["remaining_tokens"] / row["limit_tokens"])))
    if row["remaining_requests"] is not None and row["limit_requests"]:
        ratios.append(max(0.0, min(1.0, row["remaining_requests"] / row["limit_requests"])))
    if ratios:
        score += 12 * (sum(ratios) / len(ratios))

    # If we don't have headers yet, estimate from documented baseline usage.
    for counter, keyname in ((row["requests_min"], "rpm"), (row["tokens_min"], "tpm"), (row["requests_day"], "rpd"), (row["tokens_day"], "tpd")):
        lim = b.get(keyname)
        if lim:
            ratio = counter / lim
            if ratio >= 1:
                return -1
            score += 10 * max(0.0, 1.0 - ratio)
    if row["last_latency"]:
        score += max(0.0, min(3.0, 3.0 - row["last_latency"] / 10.0))
    return score


def route(body):
    candidates = [(m, candidate_score(m)) for m in MODELS]
    candidates = [(m, s) for m, s in candidates if s >= 0]
    if not candidates:
        raise RuntimeError("all LLM providers are in cooldown, disabled, out of quota, or missing API keys")

    errors = []
    # Weighted random among the best available providers: this distributes load while
    # still strongly preferring providers with healthy/remaining quota.
    candidates.sort(key=lambda x: x[1], reverse=True)
    top = candidates[:3]
    weights = [max(0.1, s - top[-1][1] + 1.0) for _, s in top]
    order = []
    pool = list(zip([m for m, _ in top], weights))
    while pool:
        names = [x[0] for x in pool]
        ws = [x[1] for x in pool]
        chosen = random.choices(names, weights=ws, k=1)[0]
        order.append(chosen)
        pool = [x for x in pool if x[0] != chosen]
    # Remaining candidates are deterministic fallbacks.
    order.extend(m for m, _ in candidates if m not in order)

    for item in order:
        provider, name = item.split(":", 1) if ":" in item else ("groq", item)
        started = time.time()
        try:
            r = request_provider(provider, name, body)
            latency = time.time() - started
            mark_usage(item, r, body, ok=(200 <= r.status_code < 300), latency=latency)
            if 200 <= r.status_code < 300:
                save(item, cooldown_until=0, disabled_until=0, last_error="")
                return r

            learn_headers(item, r.headers)
            retry_after = headers_number(r.headers, "retry-after")
            if r.status_code == 429:
                cd = max(COOLDOWN_DEFAULT, int(retry_after or 60))
                save(item, cooldown_until=time.time() + cd, last_error=f"HTTP 429; cooldown {cd}s")
                errors.append(f"{item}: 429 ({cd}s)")
                continue
            if r.status_code in (402, 401, 403):
                # 402 is exactly what the current Mistral key returns when billing/free
                # access is not enabled. Don't hammer it on every post.
                save(item, disabled_until=time.time() + 3600, last_error=f"HTTP {r.status_code}; disabled 1h")
                errors.append(f"{item}: HTTP {r.status_code} (disabled 1h)")
                continue
            if 500 <= r.status_code < 600:
                save(item, cooldown_until=time.time() + 30, last_error=f"HTTP {r.status_code}; cooldown 30s")
                errors.append(f"{item}: HTTP {r.status_code}")
                continue
            errors.append(f"{item}: HTTP {r.status_code}")
        except Exception as e:
            save(item, cooldown_until=time.time() + 10, last_error=str(e)[:300])
            errors.append(f"{item}: {e}")
    raise RuntimeError("all LLM providers failed: " + " | ".join(errors))


def status_obj():
    out = []
    for item in MODELS:
        row = load(item)
        base = BASELINE.get(item, {})
        out.append({
            "model": item,
            "available": candidate_score(item) >= 0,
            "cooldown_seconds": max(0, int(row["cooldown_until"] - time.time())),
            "disabled_seconds": max(0, int(row["disabled_until"] - time.time())),
            "rpm": base.get("rpm"), "rpd": base.get("rpd"), "tpm": base.get("tpm"), "tpd": base.get("tpd"),
            "requests_min": row["requests_min"], "requests_day": row["requests_day"],
            "tokens_min": row["tokens_min"], "tokens_day": row["tokens_day"],
            "remaining_requests": row["remaining_requests"], "limit_requests": row["limit_requests"],
            "remaining_tokens": row["remaining_tokens"], "limit_tokens": row["limit_tokens"],
            "last_error": row["last_error"], "last_latency": row["last_latency"],
        })
    return {"ok": True, "models": out}


class H(BaseHTTPRequestHandler):
    def sendj(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print("router:", fmt % args, flush=True)

    def do_GET(self):
        if self.path == "/health":
            return self.sendj(200, {"ok": True, "models": MODELS})
        if self.path == "/status":
            return self.sendj(200, status_obj())
        return self.sendj(200, {"ok": True})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            return self.sendj(404, {"error": {"message": "not found"}})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n))
            r = route(body)
            try:
                obj = r.json()
            except Exception:
                obj = {"error": {"message": r.text[:1000]}}
            self.sendj(r.status_code, obj)
        except Exception as e:
            self.sendj(503, {"error": {"message": str(e)}})


if __name__ == "__main__":
    print("LLM router active on 127.0.0.1:%s; models=%s" % (PORT, ",".join(MODELS)), flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
