#!/usr/bin/env python3
"""Tsumugu TUI launcher."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tsumugu.tui.app import run

if __name__ == "__main__":
    run()