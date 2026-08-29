#!/usr/bin/env python3
import json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

PORT = int(os.getenv("LLM_ROUTER_PORT", "8099"))
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
COOLDOWN = int(os.getenv("LLM_ROUTER_COOLDOWN", "60"))
_cooldown_until = {}

def request_provider(provider, name, body):
    base, keyname = PROVIDERS[provider]
    key = os.getenv(keyname, "")
    if not key:
        raise RuntimeError(f"{provider} key missing")
    payload = dict(body)
    payload["model"] = name
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/hiworld1231/gpt"
        headers["X-Title"] = "Linux.do Hunter"
    return requests.post(base + "/chat/completions", headers=headers, json=payload, timeout=120)

def route(body):
    errors = []
    now = time.time()
    for item in MODELS:
        provider, name = item.split(":", 1) if ":" in item else ("groq", item)
        if provider not in PROVIDERS:
            errors.append(f"{item}: unknown provider")
            continue
        if now < _cooldown_until.get(item, 0):
            continue
        try:
            r = request_provider(provider, name, body)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                _cooldown_until[item] = time.time() + COOLDOWN
                errors.append(f"{item}: HTTP {r.status_code}")
                continue
            return r
        except Exception as e:
            _cooldown_until[item] = time.time() + 10
            errors.append(f"{item}: {e}")
    raise RuntimeError("all LLM providers failed: " + " | ".join(errors))

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
