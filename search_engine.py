#!/usr/bin/env python3
"""Compatibility wrapper for the unrestricted AI Telegram search."""
from search_engine_v2 import *

# Keep names expected by checknow_buttons.py.
MAX_RETRIEVED = 10**9
MAX_COMMENTS_PER_POST = COMMENTS_PER_POST

# Replace the overly combinatorial query planner with a focused planner and add
# an optional clarification step before expensive Telegram searching.
import search_clarifier as _clarifier

plan_search = _clarifier.plan_search
_original_enhanced_search_now = enhanced_search_now

def enhanced_search_now(request, limit=None):
    return _clarifier.start_or_search(request, _original_enhanced_search_now)
