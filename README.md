# Linux.do Hunter

Hourly Linux.do monitor with Groq model fallback and Telegram alerts.

## Model fallback

Primary: `openai/gpt-oss-120b`
Fallback: `openai/gpt-oss-20b`
Optional: `qwen/qwen3.8-27b`

The client switches models on HTTP 429/rate-limit errors and retries after `Retry-After` when available.
