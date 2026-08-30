#!/usr/bin/env python3
"""Entrypoint that installs AI search clarification before starting the bot."""
import runpy

# Import first so search_clarifier can wrap get_updates before the command loop starts.
import search_clarifier  # noqa: F401,E402

runpy.run_module("checknow_buttons", run_name="__main__")
