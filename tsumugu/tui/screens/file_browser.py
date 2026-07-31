"""Main screen: command input + log panel + file browser sidebar."""
from __future__ import annotations

import os
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    Label,
    RichLog,
    Static,
)

from ...services import config_service, file_service
from ...services.download_service import (
    DownloadStatus,
    DownloadTask,
    download_manager,
)
from ...utils.files import filesizeformat


class MainScreen(Screen):
    """Command-driven main screen: input bar on top, logs left, files right."""

    BINDINGS = [
        Binding("tab", "cycle_focus", "Switch"),
    ]

    def __init__(self):
        super().__init__()
        self._current_path = "/"

    # ── compose ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Input(id="cmd-input", placeholder="  paste URL to download  |  :help  |  :cd /Music")
        with Horizontal(id="main-area"):
            with Vertical(id="log-pane"):
                yield RichLog(id="log", markup=True, highlight=False)
            with VerticalScroll(id="files-pane"):
                yield Label("/", id="path-label")
                yield DataTable(id="file-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#cmd-input", Input).focus()
        table = self.query_one("#file-table", DataTable)
        table.add_column("Name", key="name", width=30)
        table.add_column("Size", key="size", width=10)
        download_manager.add_listener(self._on_download_progress)
        self._refresh_files()
        self.write_log("Welcome to Tsumugu. Paste a YouTube URL or type :help")

    def on_unmount(self) -> None:
        download_manager.remove_listener(self._on_download_progress)

    # ── helpers ─────────────────────────────────────────────────────────────

    def write_log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def get_db(self):
        return self.app.get_db()

    # ── command input ──────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "cmd-input":
            return
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        if text.startswith(":"):
            self._handle_command(text[1:])
        elif text.startswith("http"):
            self._handle_download(text)
        else:
            self.write_log(f"[dim]Unknown input: {text}[/dim]")

    def _handle_command(self, cmd: str) -> None:
        parts = cmd.split()
        c = parts[0].lower() if parts else ""
        if c in ("help", "h", "?"):
            self.write_log("[bold cyan]Commands:[/bold cyan]")
            self.write_log("  [green]:cd PATH[/green]       browse a folder")
            self.write_log("  [green]:ls[/green]             list current folder")
            self.write_log("  [green]:mkdir NAME[/green]     create folder")
            self.write_log("  [green]:settings[/green]       open settings")
            self.write_log("  [green]:sync[/green]           open sync management")
            self.write_log("  [green]:clear[/green]          clear log")
            self.write_log("  Paste a URL to download audio")
        elif c == "cd" and len(parts) > 1:
            self._cd(parts[1])
        elif c == "ls":
            self._refresh_files()
            self.write_log(f"[dim]Listing {self._current_path}[/dim]")
        elif c == "mkdir" and len(parts) > 1:
            self._mkdir(parts[1])
        elif c == "settings":
            from .settings import SettingsScreen
            self.app.push_screen(SettingsScreen())
        elif c == "sync":
            from .sync import SyncScreen
            self.app.push_screen(SyncScreen())
        elif c == "clear":
            self.query_one("#log", RichLog).clear()
        else:
            self.write_log(f"[dim red]Unknown command: :{cmd}[/dim red]")

    def _cd(self, path: str) -> None:
        if path == "..":
            parent = os.path.dirname(self._current_path.rstrip("/")) or "/"
            self._current_path = parent
        elif path == "/" or path == "~":
            self._current_path = "/"
        elif path.startswith("/"):
            self._current_path = path
        else:
            base = self._current_path.rstrip("/")
            self._current_path = f"{base}/{path}" if base else f"/{path}"
        self._refresh_files()

    def _mkdir(self, name: str) -> None:
        db = self.get_db()
        nas_root = config_service.get_nas_root(db)
        ok, msg = file_service.create_folder(nas_root, self._current_path, name)
        self.write_log(f"{'[green]✅' if ok else '[red]❌'} {msg}[/]" )
        if ok:
            self._refresh_files()

    def _handle_download(self, url: str) -> None:
        from urllib.parse import urlparse
        from ...db import DownloadHistory
        from ...services.download_service import DownloadTask

        parsed = urlparse(url)
        is_yt = "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc
        dl_type = "youtube" if is_yt else "direct"
        title = os.path.basename(parsed.path) or "download" if dl_type == "direct" else ""

        db = self.get_db()
        nas_root = config_service.get_nas_root(db)
        save_path = os.path.join(nas_root, self._current_path.lstrip("/"))

        download = DownloadHistory(
            url=url, title=title, format="mp3",
            file_path=save_path, status="pending",
        )
        db.add(download)
        db.commit()
        db.refresh(download)

        task = DownloadTask(
            id=int(download.id),
            url=url, title=title, format="mp3",
            save_path=save_path, download_type=dl_type, current_file=title,
        )

        import asyncio
        asyncio.get_event_loop().create_task(download_manager.add_download(task))
        self.write_log(f"[cyan]⬇ Queued:[/cyan] {url}")

    # ── file browser sidebar ───────────────────────────────────────────────

    @work(thread=True)
    def _refresh_files(self) -> None:
        db = self.get_db()
        nas_root = config_service.get_nas_root(db)
        files = file_service.list_files(nas_root, self._current_path, db=db)
        self.query_one("#path-label", Label).update(self._current_path)
        table = self.query_one("#file-table", DataTable)
        table.clear()
        for f in files:
            tag = {"music": " ♪", "podcast": " ☎"}.get(f.get("tag"), "")
            name = f["name"] + ("/" if f["type"] == "folder" else "")
            size = filesizeformat(f["size"]) if f["type"] != "folder" else ""
            table.add_row(name, size, key=f["path"])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        path = str(event.row_key.value) if event.row_key.value else ""
        if not path:
            return
        table = event.data_table
        row = table.get_row(event.row_key)
        if row and str(row[0]).endswith("/"):
            self._current_path = path
            self._refresh_files()

    # ── download progress listener ────────────────────────────────────────

    async def _on_download_progress(self, task: DownloadTask) -> None:
        self.call_after_refresh(lambda: self._render_progress(task))

    def _render_progress(self, task: DownloadTask) -> None:
        status = task.status.value
        if status == "downloading":
            self.write_log(
                f"  [yellow]{task.progress:.0f}%[/yellow] {task.speed} {task.eta}  "
                f"[dim]{task.current_file}[/dim]"
            )
        elif status == "converting":
            self.write_log("[yellow]Converting to mp3...[/yellow]")
        elif status == "splitting":
            self.write_log("[yellow]Splitting audio...[/yellow]")
        elif status == "completed":
            self.write_log(f"[green]✅ Done: {task.title or task.current_file}[/green]")
        elif status == "failed":
            self.write_log(f"[red]❌ Failed: {task.error}[/red]")

    # ── actions ────────────────────────────────────────────────────────────

    def action_cycle_focus(self) -> None:
        """Cycle focus between input and file table."""
        if self.query_one("#cmd-input", Input).has_focus:
            self.query_one("#file-table", DataTable).focus()
        else:
            self.query_one("#cmd-input", Input).focus()