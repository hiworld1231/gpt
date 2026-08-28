import os, time, requests

MODELS = [m.strip() for m in os.getenv("LLM_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if m.strip()]
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
API_KEY = os.environ["LLM_API_KEY"]


def chat(messages, **kwargs):
    """Try models in order. On 429, honor Retry-After briefly then move to next model."""
    last = None
    for model in MODELS:
        try:
            r = requests.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, **kwargs},
                timeout=90,
            )
            if r.status_code == 429:
                delay = min(float(r.headers.get("retry-after", "2")), 10)
                time.sleep(delay)
                last = RuntimeError(f"{model}: rate limited")
                continue
            if r.status_code >= 500:
                last = RuntimeError(f"{model}: HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r.json(), model
        except requests.RequestException as exc:
            last = exc
            continue
    raise RuntimeError(f"All configured models failed: {last}")
