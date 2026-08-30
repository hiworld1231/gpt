#!/usr/bin/env python3
"""Compatibility wrapper for the unrestricted AI Telegram search."""
import search_engine_v2 as _v2
from search_engine_v2 import *

MAX_RETRIEVED = 10**9
MAX_COMMENTS_PER_POST = COMMENTS_PER_POST

import search_clarifier as _clarifier
_v2.plan_search = _clarifier.plan_search
plan_search = _clarifier.plan_search

# Keep every discovered result available, but remove obvious semantic junk before
# expensive deep analysis. This is a relevance floor, not a top-N result cap.
_original_rank_hits = _v2._rank_hits

def _rank_hits_relevant(request, posts, comments):
    ranked = _original_rank_hits(request, posts, comments)
    useful = [(m, score) for m, score in ranked if score >= 60]
    if useful:
        return useful
    # If the ranking model failed or everything scored low, keep the best few
    # candidates rather than returning literally unrelated zero-score posts.
    return ranked[:5]

_v2._rank_hits = _rank_hits_relevant

_original_enhanced_search_now = enhanced_search_now

def enhanced_search_now(request, limit=None):
    return _clarifier.start_or_search(request, _original_enhanced_search_now)
