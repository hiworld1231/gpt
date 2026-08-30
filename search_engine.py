#!/usr/bin/env python3
"""Compatibility wrapper for the unrestricted AI Telegram search."""
import search_engine_v2 as _v2
from search_engine_v2 import *

# Keep names expected by checknow_buttons.py.
MAX_RETRIEVED = 10**9
MAX_COMMENTS_PER_POST = COMMENTS_PER_POST

# Focused planner: patch the v2 module's global too, because
# enhanced_search_now() resolves plan_search inside search_engine_v2.
import search_clarifier as _clarifier
_v2.plan_search = _clarifier.plan_search
plan_search = _clarifier.plan_search
_original_enhanced_search_now = enhanced_search_now


def enhanced_search_now(request, limit=None):
    return _clarifier.start_or_search(request, _original_enhanced_search_now)
