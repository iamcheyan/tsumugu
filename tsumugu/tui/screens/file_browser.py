"""Main file-browser screen: directory tree + file list + download bar + queue."""
from __future__ import annotations

import os
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    ProgressBar,
    Select,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from ...services import config_service, file_service
from ...services.download_service import (
    DownloadManager,
    DownloadStatus,
    DownloadTask,
    download_manager,
)
from ...utils.files import AUDIO_EXTENSIONS, filesizeformat


class FileBrowserScreen(Screen):
    """The primary screen — tree | file table, with download bar below."""

    BINDINGS = [
        Binding("n", "new_folder", "New folder"),
        Binding("r,f2", "rename", "Rename"),
        Binding("delete", "delete", "Delete"),
        Binding("m", "move", "Move"),
        Binding("c", "compress", "Compress"),
        Binding("o", "download_file", "Download"),
        Binding("/", "focus_search", "Search"),
        Binding("escape", "blur_search", "Cancel search"),
    ]

    SORT_OPTIONS = [
        ("Name", "name"),
        ("Size", "size"),
        ("Modified", "modified"),
        ("Type", "type"),
    ]
    FORMAT_OPTIONS = [("MP3", "mp3"), ("M4A", "m4a"), ("FLAC", "flac")]
    SPLIT_OPTIONS = [
        ("No split", "none"),
        ("Chapter", "chapter_info"),
        ("Silence", "silence_detection"),
    ]

    def __init__(self):
        super().__init__()
        self._current_path = "/"
        self._sort = "name"
        self._order = "asc"
        self._show_hidden = False
        self._search = ""

    # ── compose ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-container"):
            with Vertical(id="tree-pane"):
                yield Tree("NAS Root", id="dir-tree", data="/")
            with Vertical(id="content-pane"):
                with Horizontal(id="toolbar"):
                    yield Input(placeholder="Search...", id="search-input")
                    yield Select(self.SORT_OPTIONS, value="name", id="sort-select")
                    yield Button("⟳", id="refresh-btn", variant="primary")
                yield DataTable(id="file-table", cursor_type="row", zebra_stripes=True)
        with Vertical(id="download-bar"):
            with Horizontal(id="dl-input-row"):
                yield Input(placeholder="YouTube / direct URL...", id="url-input")
                yield Select(self.FORMAT_OPTIONS, value="mp3", id="format-select")
                yield Select(self.SPLIT_OPTIONS, value="none", id="split-select")
                yield Button("Download", id="dl-button", variant="success")
            with VerticalScroll(id="queue-area"):
                yield Label("Download queue", id="queue-label")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#file-table", DataTable)
        table.add_column("Name", key="name", width=40)
        table.add_column("Size", key="size", width=10)
        table.add_column("Modified", key="modified", width=20)
        table.add_column("Type", key="type", width=8)
        table.add_column("Tag", key="tag", width=8)

        self.query_one("#tree-pane").border_title = " Directories "
        self.query_one("#content-pane").border_title = " Files "
        self.query_one("#download-bar").border_title = " Download "

        tree = self.query_one("#dir-tree", Tree)
        tree.show_root = True

        # Register as a download progress listener
        download_manager.add_listener(self._on_download_progress)

        self._build_tree()
        self._refresh_files()

    def on_unmount(self) -> None:
        download_manager.remove_listener(self._on_download_progress)

    # ── directory tree ─────────────────────────────────────────────────────

    @work(thread=True)
    def _build_tree(self) -> None:
        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        tree = self.query_one("#dir-tree", Tree)
        tree.clear()
        tree_data = file_service.build_tree(nas_root, db, self._show_hidden)
        root_node = tree.root
        root_node.set_label(tree_data["name"])
        root_node.data = "/"
        root_node.expand()
        for child in tree_data.get("children", []):
            self._add_tree_node(root_node, child)

    def _add_tree_node(self, parent: TreeNode, data: dict[str, Any]) -> None:
        node = parent.add_leaf(
            data["name"], data=data["path"]
        )
        # If has_children, add a placeholder so the expand arrow shows
        if data.get("has_children"):
            node.allow_expand = True

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Lazy-load children when a node is expanded."""
        node = event.node
        if node.children:
            return  # Already loaded
        path = node.data
        if not path or path == "/":
            return
        self._load_children(node, path)

    @work(thread=True)
    def _load_children(self, node: TreeNode, path: str) -> None:
        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        children = file_service.get_children(nas_root, path, self._show_hidden)
        for child in children:
            self._add_tree_node(node, child)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        path = event.node.data
        if path:
            self._current_path = path
            self._refresh_files()

    # ── file list ──────────────────────────────────────────────────────────

    @work(thread=True)
    def _refresh_files(self) -> None:
        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        files = file_service.list_files(
            nas_root, self._current_path, self._search,
            self._sort, self._order, self._show_hidden, db,
        )
        table = self.query_one("#file-table", DataTable)
        table.clear()
        for f in files:
            tag_str = {"music": "🎵", "podcast": "🎙"}.get(f.get("tag"), "")
            table.add_row(
                f["name"],
                filesizeformat(f["size"]) if f["type"] != "folder" else "",
                f["modified"],
                f["type"],
                tag_str,
                key=f["path"],
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row = event.data_table.get_row(event.row_key)
        ftype = row[3] if len(row) > 3 else ""
        path = str(event.row_key.value) if event.row_key.value else ""
        if ftype == "folder":
            self._current_path = path
            self._refresh_files()

    # ── toolbar ────────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-input":
            self._search = event.value
            self._refresh_files()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sort-select":
            self._sort = str(event.value)
            # Toggle order on repeated selection of same field
            self._refresh_files()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh-btn":
            self._refresh_files()
        elif event.button.id == "dl-button":
            self._start_download()

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_blur_search(self) -> None:
        s = self.query_one("#search-input", Input)
        if s.value:
            s.value = ""
            self._search = ""
            self._refresh_files()
        else:
            self.query_one("#file-table", DataTable).focus()

    # ── file operations ────────────────────────────────────────────────────

    def action_new_folder(self) -> None:
        from .dialogs import InputDialog
        self.app.push_screen(InputDialog("New folder name:"), self._do_new_folder)

    def _do_new_folder(self, name: str | None) -> None:
        if not name:
            return
        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        ok, msg = file_service.create_folder(nas_root, self._current_path, name)
        self.app.notify(msg, severity="information" if ok else "error")
        self._refresh_files()

    def _get_selected_path(self) -> str | None:
        """Return the NAS-relative path of the currently selected table row."""
        table = self.query_one("#file-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            row_key = cell_key.row_key
            if row_key is None or row_key.value is None:
                return None
            return str(row_key.value)
        except Exception:
            return None

    def action_rename(self) -> None:
        path = self._get_selected_path()
        if not path:
            return
        from .dialogs import InputDialog
        self.app.push_screen(
            InputDialog("New name:", initial_value=os.path.basename(path)),
            lambda new_name: self._do_rename(path, new_name),
        )

    def _do_rename(self, path: str, new_name: str | None) -> None:
        if not new_name:
            return
        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        ok, msg = file_service.rename(nas_root, path, new_name)
        self.app.notify(msg, severity="information" if ok else "error")
        self._refresh_files()

    def action_delete(self) -> None:
        path = self._get_selected_path()
        if not path:
            return
        from .dialogs import ConfirmDialog
        self.app.push_screen(
            ConfirmDialog(f"Delete '{os.path.basename(path)}'?"),
            lambda confirmed: self._do_delete(path, confirmed),
        )

    def _do_delete(self, path: str, confirmed: bool) -> None:
        if not confirmed:
            return
        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        strategy = config_service.get_config_value(db, "deletion_strategy", "recycle_bin")
        ok, msg = file_service.delete(nas_root, path, strategy)
        self.app.notify(msg, severity="information" if ok else "error")
        self._refresh_files()

    def action_move(self) -> None:
        path = self._get_selected_path()
        if not path:
            return
        from .dialogs import InputDialog
        self.app.push_screen(
            InputDialog("Destination directory:", initial_value=self._current_path),
            lambda dest: self._do_move(path, dest),
        )

    def _do_move(self, source: str, dest: str | None) -> None:
        if not dest:
            return
        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        ok, msg = file_service.move(nas_root, source, dest)
        self.app.notify(msg, severity="information" if ok else "error")
        self._refresh_files()

    def action_compress(self) -> None:
        path = self._get_selected_path()
        if not path:
            return
        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        full = os.path.join(nas_root, path.lstrip("/")) if path != "/" else nas_root
        if not os.path.isdir(full):
            self.app.notify("Select a folder to compress", severity="warning")
            return
        from ...services.compress_service import compressor
        import asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(compressor.start_compress(full, os.path.basename(full.rstrip("/"))))
        self.app.notify(f"Compressing '{os.path.basename(full.rstrip('/'))}'...", severity="information")

    def action_download_file(self) -> None:
        """Download selected file/folder to local temp."""
        path = self._get_selected_path()
        if not path:
            return
        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        local, name = file_service.download_to_temp(nas_root, path)
        if local:
            self.app.notify(f"Downloaded to: {local}", severity="information")
        else:
            self.app.notify("Download failed", severity="error")

    # ── download bar ───────────────────────────────────────────────────────

    def _start_download(self) -> None:
        url = self.query_one("#url-input", Input).value.strip()
        if not url:
            self.app.notify("Enter a URL first", severity="warning")
            return
        fmt = str(self.query_one("#format-select", Select).value)
        split_val = str(self.query_one("#split-select", Select).value)
        split_mode = None if split_val == "none" else split_val

        # Determine download type
        from ...services.download_service import DownloadTask
        from urllib.parse import urlparse

        parsed = urlparse(url)
        is_yt = "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc
        dl_type = "youtube" if is_yt else "direct"

        db = self.app.get_db()
        nas_root = config_service.get_nas_root(db)
        save_path = os.path.join(nas_root, self._current_path.lstrip("/"))

        title = ""
        if dl_type == "direct":
            title = os.path.basename(parsed.path) or "download"

        from ...db import DownloadHistory
        download = DownloadHistory(
            url=url, title=title, format=fmt, split_mode=split_mode,
            file_path=save_path, status="pending",
        )
        db.add(download)
        db.commit()
        db.refresh(download)

        task = DownloadTask(
            id=int(download.id),
            url=url, title=title, format=fmt,
            split_mode=split_mode, save_path=save_path,
            download_type=dl_type, current_file=title,
        )

        import asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(download_manager.add_download(task))
        self.query_one("#url-input", Input).value = ""
        self.app.notify("Download queued", severity="information")

    # ── download progress ──────────────────────────────────────────────────

    async def _on_download_progress(self, task: DownloadTask) -> None:
        """Listener callback — update the queue display."""
        self.call_after_refresh(self._render_queue)

    def _render_queue(self) -> None:
        area = self.query_one("#queue-area", VerticalScroll)
        active_ids = {f"task-{t.id}" for t in download_manager.get_all_tasks()}
        # Remove stale/finished widgets no longer in the task list
        for w in list(area.children):
            if isinstance(w, Static) and w.id and w.id.startswith("task-") and w.id not in active_ids:
                w.remove()
        for task in download_manager.get_all_tasks():
            status = task.status.value
            icon = {
                "pending": "⏳", "downloading": "⬇", "converting": "🔄",
                "splitting": "✂", "completed": "✅", "failed": "❌",
                "cancelled": "🚫",
            }.get(status, "•")
            bar = "█" * int(task.progress / 10) + "░" * (10 - int(task.progress / 10))
            line = f"{icon} {task.title or task.url[:40]}  [{bar}] {task.progress:.0f}% {task.speed} {task.eta}"
            if task.error:
                line += f"  ERR: {task.error[:30]}"
            wid = f"task-{task.id}"
            try:
                existing = area.query_one(f"#{wid}", Static)
                existing.update(line)
            except Exception:
                area.mount(Static(line, id=wid))