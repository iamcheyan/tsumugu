"""Tsumugu TUI — main Textual application."""
from __future__ import annotations

import os

from textual.app import App
from textual.binding import Binding

from ..db import SessionLocal, create_tables
from .services_init import init_services
from .screens.file_browser import MainScreen


class TsumuguApp(App):
    """NAS file browser + YouTube audio downloader — TUI edition."""

    CSS_PATH = "styles.tcss"
    TITLE = "Tsumugu"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.db = SessionLocal()

    def on_mount(self) -> None:
        create_tables()
        from ..services import config_service
        config_service.seed_defaults(self.db)
        config_service.refresh_dependency_status(self.db)
        mount_result = config_service.auto_mount_nas(self.db)
        if mount_result.get("success"):
            self.notify(f"NAS mounted: {mount_result['mount_point']}")
        elif mount_result.get("message") and "not configured" not in mount_result["message"]:
            self.notify(f"NAS mount: {mount_result['message']}", severity="warning")
        config_service.ensure_default_sync_folder(self.db)
        init_services(self)
        self.push_screen(MainScreen())

    def on_unmount(self) -> None:
        self.db.close()

    def get_db(self):
        return self.db


def run() -> None:
    """Entry point for ``python -m tsumugu`` or run_tui.py."""
    app = TsumuguApp()
    app.run()


if __name__ == "__main__":
    run()