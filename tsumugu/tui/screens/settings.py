"""Settings screen — NAS config, deletion strategy, AI model, cache, dependencies."""
from __future__ import annotations

from textual.app import ComposeResult
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label, Select, Static

from ...services import config_service


class SettingsScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]

    PROTOCOL_OPTS = [("SMB/CIFS", "smb"), ("NFS", "nfs")]
    DELETE_OPTS = [("Recycle Bin", "recycle_bin"), ("Direct Delete", "direct_delete")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="settings-card"):
            yield Label("Settings", id="settings-title")
            yield Static("")
            yield Label("── NAS Connection ──")
            with Horizontal(classes="settings-row"):
                yield Label("Address")
                yield Input(id="nas-address", placeholder="192.168.1.100")
            with Horizontal(classes="settings-row"):
                yield Label("Protocol")
                yield Select(self.PROTOCOL_OPTS, value="smb", id="nas-protocol")
            with Horizontal(classes="settings-row"):
                yield Label("Share")
                yield Input(id="nas-share", placeholder="volume1/music")
            with Horizontal(classes="settings-row"):
                yield Label("Username")
                yield Input(id="nas-username")
            with Horizontal(classes="settings-row"):
                yield Label("Password")
                yield Input(id="nas-password", password=True)
            with Horizontal(classes="settings-row"):
                yield Label("Port")
                yield Input(id="nas-port", value="445")
            with Horizontal(classes="settings-row"):
                yield Button("Test", id="test-btn")
                yield Button("Mount", id="mount-btn", variant="success")
                yield Button("Unmount", id="unmount-btn", variant="warning")
            yield Static("", id="mount-status")
            yield Static("")
            yield Label("── File Deletion ──")
            with Horizontal(classes="settings-row"):
                yield Label("Strategy")
                yield Select(self.DELETE_OPTS, value="recycle_bin", id="delete-strategy")
            yield Static("")
            yield Label("── Dependencies ──")
            yield Static("", id="dep-status")
            yield Button("Refresh deps", id="refresh-dep-btn")
            yield Static("")
            yield Label("── Cache ──")
            yield Static("", id="cache-stats")
            yield Button("Clear cache", id="clear-cache-btn", variant="error")
            yield Static("")
        yield Footer()

    def on_mount(self) -> None:
        self._load_config()

    @work(thread=True)
    def _load_config(self) -> None:
        db = self.app.get_db()
        vals = {
            "nas-address": config_service.get_config_value(db, "nas_address"),
            "nas-share": config_service.get_config_value(db, "nas_share"),
            "nas-username": config_service.get_config_value(db, "nas_username"),
            "nas-password": config_service.get_config_value(db, "nas_password"),
            "nas-port": config_service.get_config_value(db, "nas_port", "445"),
        }
        for wid, val in vals.items():
            self.query_one(f"#{wid}", Input).value = val
        self.query_one("#nas-protocol", Select).value = config_service.get_config_value(db, "nas_protocol", "smb")
        self.query_one("#delete-strategy", Select).value = config_service.get_config_value(
            db, "deletion_strategy", "recycle_bin"
        )
        # Deps
        deps = config_service.refresh_dependency_status(db)
        self.query_one("#dep-status", Static).update(
            f"yt-dlp: {'✅' if deps['yt-dlp']=='installed' else '❌ missing'}  "
            f"ffmpeg: {'✅' if deps['ffmpeg']=='installed' else '❌ missing'}"
        )
        # Cache
        stats = config_service.get_cache_stats(db)
        lines = [f"Total: {config_service._format_size(stats['total_size'])}"]
        for c in stats["categories"]:
            lines.append(f"  {c['name']}: {config_service._format_size(c['size'])} ({c['count']} items)")
        self.query_one("#cache-stats", Static).update("\n".join(lines))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "test-btn":
            self._test_connection()
        elif bid == "mount-btn":
            self._save_and_mount()
        elif bid == "unmount-btn":
            self._unmount()
        elif bid == "refresh-dep-btn":
            db = self.app.get_db()
            deps = config_service.refresh_dependency_status(db)
            self.query_one("#dep-status", Static).update(
                f"yt-dlp: {'✅' if deps['yt-dlp']=='installed' else '❌ missing'}  "
                f"ffmpeg: {'✅' if deps['ffmpeg']=='installed' else '❌ missing'}"
            )
        elif bid == "clear-cache-btn":
            self._clear_cache()

    def _save_config(self) -> None:
        db = self.app.get_db()
        config_service.set_config_value(db, "nas_address", self.query_one("#nas-address", Input).value)
        config_service.set_config_value(db, "nas_protocol", str(self.query_one("#nas-protocol", Select).value))
        config_service.set_config_value(db, "nas_share", self.query_one("#nas-share", Input).value)
        config_service.set_config_value(db, "nas_username", self.query_one("#nas-username", Input).value)
        config_service.set_config_value(db, "nas_password", self.query_one("#nas-password", Input).value)
        config_service.set_config_value(db, "nas_port", self.query_one("#nas-port", Input).value)
        config_service.set_config_value(db, "deletion_strategy", str(self.query_one("#delete-strategy", Select).value))

    def _test_connection(self) -> None:
        addr = self.query_one("#nas-address", Input).value
        port = self.query_one("#nas-port", Input).value
        proto = str(self.query_one("#nas-protocol", Select).value)
        result = config_service.test_connection(addr, port, proto)
        self.app.notify(result["message"], severity="information" if result["success"] else "error")

    def _save_and_mount(self) -> None:
        self._save_config()
        db = self.app.get_db()
        result = config_service.mount_share(db)
        self.app.notify(result["message"], severity="information" if result["success"] else "error")
        self._load_config()

    def _unmount(self) -> None:
        db = self.app.get_db()
        result = config_service.unmount_share(db)
        self.app.notify(result["message"], severity="information" if result["success"] else "error")

    def _clear_cache(self) -> None:
        db = self.app.get_db()
        result = config_service.clear_cache(db)
        self.app.notify(result["message"], severity="information")
        self._load_config()

    def action_back(self) -> None:
        self._save_config()
        self.app.pop_screen()