#!/usr/bin/env python3
"""Persistent Telegram search-source management."""
import json
import os


def _path(bot):
    return bot.SETTINGS_FILE


def _load_raw(bot):
    path = _path(bot)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_raw(bot, data):
    path = _path(bot)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def normalize_source(value):
    value = str(value or "").strip()
    if value.startswith("https://t.me/"):
        value = value.split("https://t.me/", 1)[1].split("/", 1)[0]
    value = value.strip().lstrip("@").strip()
    if not value or len(value) > 100 or any(c.isspace() for c in value):
        raise ValueError("некорректный источник")
    return value


def get_sources(bot):
    data = _load_raw(bot)
    sources = data.get("search_sources")
    if not isinstance(sources, list) or not sources:
        sources = [str(bot.TG_SOURCE_CHANNEL).lstrip("@").strip()]
    out = []
    for item in sources:
        try:
            item = normalize_source(item)
        except ValueError:
            continue
        if item and item.lower() not in {x.lower() for x in out}:
            out.append(item)
    return out or [str(bot.TG_SOURCE_CHANNEL).lstrip("@").strip()]


def add_source(bot, value):
    source = normalize_source(value)
    sources = get_sources(bot)
    if source.lower() in {x.lower() for x in sources}:
        return False, sources
    sources.append(source)
    data = _load_raw(bot)
    data["search_sources"] = sources
    _save_raw(bot, data)
    return True, sources


def remove_source(bot, value):
    source = normalize_source(value)
    sources = get_sources(bot)
    if source.lower() == str(bot.TG_SOURCE_CHANNEL).lstrip("@").lower():
        raise ValueError("основной источник нельзя удалить")
    new_sources = [x for x in sources if x.lower() != source.lower()]
    if len(new_sources) == len(sources):
        return False, sources
    data = _load_raw(bot)
    data["search_sources"] = new_sources
    _save_raw(bot, data)
    return True, new_sources


def format_sources(bot):
    sources = get_sources(bot)
    return "\n".join(f"{i}. @{s}" for i, s in enumerate(sources, 1))
