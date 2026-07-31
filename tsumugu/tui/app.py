"""Tsumugu TUI — main Textual application."""
from __future__ import annotations

import os
import shutil
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Label, Input, Button, Select, ProgressBar, Static

from ..db import SessionLocal, create_tables
from .services_init import init_services
from .screens.file_browser import FileBrowserScreen


class TsumuguApp(App):
    """NAS file browser + YouTube audio downloader — TUI edition."""

    CSS_PATH = "styles.tcss"
    TITLE = "Tsumugu"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "goto_settings", "Settings"),
        Binding("y", "goto_sync", "Sync"),
        Binding("d", "focus_download", "Download"),
        Binding("t", "focus_tree", "Tree"),
        Binding("f", "focus_files", "Files"),
    ]

    def __init__(self):
        super().__init__()
        self.db = SessionLocal()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        create_tables()
        from ..services import config_service

        # Seed defaults, detect deps, auto-mount NAS
        config_service.seed_defaults(self.db)
        config_service.refresh_dependency_status(self.db)
        mount_result = config_service.auto_mount_nas(self.db)
        if mount_result.get("success"):
            self.notify(f"NAS mounted: {mount_result['mount_point']}")
        elif mount_result.get("message") and "not configured" not in mount_result["message"]:
            self.notify(f"NAS mount: {mount_result['message']}", severity="warning")
        config_service.ensure_default_sync_folder(self.db)

        # Start download manager + compressor in this event loop
        init_services(self)

        self.push_screen(FileBrowserScreen())

    def on_unmount(self) -> None:
        self.db.close()

    # ── actions ────────────────────────────────────────────────────────────

    def action_goto_settings(self) -> None:
        from .screens.settings import SettingsScreen
        self.push_screen(SettingsScreen())

    def action_goto_sync(self) -> None:
        from .screens.sync import SyncScreen
        self.push_screen(SyncScreen())

    def action_focus_download(self) -> None:
        try:
            self.query_one("#url-input", Input).focus()
        except Exception:
            pass

    def action_focus_tree(self) -> None:
        try:
            self.query_one("#dir-tree").focus()
        except Exception:
            pass

    def action_focus_files(self) -> None:
        try:
            self.query_one("#file-table").focus()
        except Exception:
            pass

    # ── helpers ────────────────────────────────────────────────────────────

    def get_db(self):
        """Other screens can grab the shared session."""
        return self.db


def run() -> None:
    """Entry point for ``python -m tsumugu`` or run_tui.py."""
    app = TsumuguApp()
    app.run()


if __name__ == "__main__":
    run()