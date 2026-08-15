"""Sync management screen — sync folders, rescan, index status."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from ...services import sync_service_wrapper as sync_svc


class SyncScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="settings-card"):
            yield Static("", id="sync-status")
            yield Label("── Sync Folders ──")
            yield DataTable(id="sync-table", cursor_type="row")
            with Horizontal(classes="settings-row"):
                yield Label("Path")
                yield Input(id="sync-path", placeholder="/Music")
                yield Button("Add", id="sync-add", variant="success")
            yield Static("")
            with Horizontal():
                yield Button("Rescan", id="sync-rescan", variant="primary")
                yield Button("Download index", id="sync-download")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#settings-card").border_title = " Sync Management "
        table = self.query_one("#sync-table", DataTable)
        table.add_column("Path", width=30)
        table.add_column("Name", width=20)
        table.add_column("Enabled", width=10)
        self._refresh()

    def _refresh(self) -> None:
        db = self.app.get_db()
        # Status
        status = sync_svc.sync_status(db)
        lines = [
            f"Indexed: {'yes' if status.get('indexed') else 'no'}",
            f"Files: {status.get('file_count', 0)}",
            f"Enabled folders: {status.get('folder_count', 0)}",
        ]
        self.query_one("#sync-status", Static).update("\n".join(lines))
        # Folders
        table = self.query_one("#sync-table", DataTable)
        table.clear()
        for f in sync_svc.list_sync_folders(db):
            table.add_row(f["path"], f["name"], "✅" if f["enabled"] else "⬚", key=str(f["id"]))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        db = self.app.get_db()
        if bid == "sync-add":
            path = self.query_one("#sync-path", Input).value.strip()
            if path:
                result = sync_svc.add_sync_folder(db, path)
                self.app.notify(result["message"], severity="information" if result["success"] else "error")
                self.query_one("#sync-path", Input).value = ""
                self._refresh()
        elif bid == "sync-rescan":
            result = sync_svc.rescan(db)
            self.app.notify(result["message"], severity="information")
            self._refresh()
        elif bid == "sync-download":
            data, name = sync_svc.download_file_index(db)
            import tempfile, os
            path = os.path.join(tempfile.gettempdir(), name)
            with open(path, "wb") as f:
                f.write(data)
            self.app.notify(f"Index saved to {path}", severity="information")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        db = self.app.get_db()
        try:
            fid = int(event.row_key.value)
        except (TypeError, ValueError):
            return
        result = sync_svc.toggle_sync_folder(db, fid)
        self.app.notify(f"Enabled: {result['enabled']}", severity="information")
        self._refresh()

    def action_back(self) -> None:
        self.app.pop_screen()