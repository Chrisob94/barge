from __future__ import annotations

import time

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable, Input, Static

from .. import fmt, paths
from ..backends import Backend, BackendError, Entry
from ..location import Location
from . import names

SIZE_W = 8
WHEN_W = 8

# a rough colour per file type, so a long listing is easier to scan
EXT_STYLES = {
    "code": ({"py", "js", "ts", "go", "rs", "rb", "java", "c", "h", "sh", "css", "html"}, "yellow"),
    "data": ({"json", "yaml", "yml", "toml", "csv", "xml", "sql", "md", "txt"}, "cyan"),
    "media": ({"png", "jpg", "jpeg", "gif", "webp", "svg", "mp4", "mov", "mp3", "wav"}, "magenta"),
    "archive": ({"zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar"}, "red"),
}


def ext_style(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for exts, style in EXT_STYLES.values():
        if ext in exts:
            return style
    return "dim"


class FilePane(Vertical):
    BINDINGS = [
        Binding("backspace", "go_up", "Up", show=False),
        Binding("space", "toggle_mark", "Mark", show=False),
        Binding("ctrl+a", "mark_all", "Mark all", show=False),
        Binding("slash", "filter", "Filter", show=False),
        Binding("escape", "clear", "Clear", show=False),
        Binding("full_stop", "toggle_hidden", "Hidden files", show=False),
    ]

    class Activated(Message):
        def __init__(self, pane: FilePane) -> None:
            super().__init__()
            self.pane = pane

    class Opened(Message):
        """A file (not a folder) was opened with enter."""

        def __init__(self, pane: FilePane, entry: Entry) -> None:
            super().__init__()
            self.pane = pane
            self.entry = entry

    path: reactive[str] = reactive("/", init=False)

    def __init__(self, location: Location, *, id: str | None = None) -> None:
        super().__init__(id=id, classes="pane")
        self.backend: Backend = location.backend
        self.path = location.path
        self.entries: list[Entry] = []
        self.marked: set[str] = set()
        self.filter_text = ""
        self.show_hidden = False
        self.status = "connecting"
        self.latency_ms: int | None = None
        self._restore_cursor: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="crumbs", classes="pane-header")
        yield Input(placeholder="filter", id="filter", classes="hidden")
        yield Static(id="colhead", classes="pane-colhead")
        table: DataTable = DataTable(show_header=False, cursor_type="row", zebra_stripes=False)
        table.add_column("row", key="row")
        yield table
        yield Static(id="info", classes="pane-info")

    def on_mount(self) -> None:
        self.refresh_listing()

    # helpers other code uses

    @property
    def table(self) -> DataTable:
        return self.query_one(DataTable)

    @property
    def location(self) -> Location:
        return Location(self.backend, self.path)

    @property
    def short_name(self) -> str:
        return names.short_name(self.backend)

    def visible(self) -> list[Entry]:
        items = self.entries
        if not self.show_hidden:
            items = [e for e in items if not e.name.startswith(".")]
        if self.filter_text:
            needle = self.filter_text.lower()
            items = [e for e in items if needle in e.name.lower()]
        return items

    def current(self) -> Entry | None:
        table = self.table
        if table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        if key == "..":
            return None
        return next((e for e in self.entries if e.path == key), None)

    def selection(self) -> list[Entry]:
        if self.marked:
            return [e for e in self.entries if e.path in self.marked]
        entry = self.current()
        return [entry] if entry else []

    def names(self) -> set[str]:
        return {e.name for e in self.entries}

    def set_location(self, location: Location) -> None:
        if location.backend is not self.backend:
            self.backend.close()
        self.backend = location.backend
        self.status = "connecting"
        self.cd(location.path)

    def cd(self, path: str, focus_name: str | None = None) -> None:
        self.path = paths.normalize(path)
        self.marked.clear()
        self._clear_filter()
        self._restore_cursor = focus_name
        self.refresh_listing()

    def refresh_listing(self, keep_cursor: bool = True) -> None:
        if keep_cursor and self._restore_cursor is None and (cur := self.current()):
            self._restore_cursor = cur.name
        self._render_header()
        self._load(self.path)

    # loading

    @work(thread=True, exclusive=True, group="listing")
    def _load(self, path: str) -> None:
        started = time.monotonic()
        try:
            entries = self.backend.list_dir(path)
        except BackendError as e:
            self.app.call_from_thread(self._failed, path, str(e))
            return
        ms = int((time.monotonic() - started) * 1000)
        self.app.call_from_thread(self._loaded, path, entries, ms)

    def _loaded(self, path: str, entries: list[Entry], ms: int) -> None:
        if path != self.path:
            return  # user moved on while we were listing
        self.entries = entries
        self.marked &= {e.path for e in entries}
        self.status = "ok"
        self.latency_ms = ms
        self._render_rows()
        self._render_header()

    def _failed(self, path: str, error: str) -> None:
        self.status = "error"
        self.entries = []
        self._render_rows()
        self._render_header()
        self.app.notify(error, title=self.short_name, severity="error")

    # rendering

    def _row_width(self) -> int:
        return max(self.table.size.width - 2, 30)

    def _render_header(self) -> None:
        crumbs = Text()
        crumbs.append(self.short_name, style=f"bold {self._scheme_style()}")
        crumbs.append("  ")
        parts = [p for p in self.path.split("/") if p]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            crumbs.append(part, style="bold" if last else "dim")
            if not last:
                crumbs.append(" / ", style="dim")
        if not parts:
            crumbs.append("/", style="bold")

        dot = {"ok": "green", "error": "red"}.get(self.status, "yellow")
        status = Text("● ", style=dot)
        if self.status == "ok" and self.latency_ms is not None and self.backend.scheme != "local":
            status.append(f"{self.latency_ms}ms", style="dim")

        width = max(self.size.width - 4, 20)
        pad = max(width - crumbs.cell_len - status.cell_len, 1)
        self.query_one("#crumbs", Static).update(Text.assemble(crumbs, " " * pad, status))

        name_w = self._row_width() - 4 - SIZE_W - WHEN_W - 2
        head = Text(f"    {'Name':<{name_w}} {'Size':>{SIZE_W}} {'Modified':>{WHEN_W}}", style="dim")
        self.query_one("#colhead", Static).update(head)

    def _scheme_style(self) -> str:
        return names.color(self.backend, self.app.current_theme.dark)

    def _render_rows(self) -> None:
        table = self.table
        width = self._row_width()
        table.columns["row"].width = width  # type: ignore[index]
        table.clear()

        name_w = width - 4 - SIZE_W - WHEN_W - 2
        if self.path != "/":
            table.add_row(Text(f"    {'..':<{name_w}}", style="bold blue"), key="..")
        for e in self.visible():
            table.add_row(self._row(e, name_w), key=e.path)

        if self._restore_cursor:
            for i, e in enumerate(self.visible()):
                if e.name == self._restore_cursor:
                    table.move_cursor(row=i + (self.path != "/"))
                    break
            self._restore_cursor = None
        self._render_info()

    def _row(self, e: Entry, name_w: int) -> Text:
        marked = e.path in self.marked
        name = e.name if len(e.name) <= name_w else e.name[: name_w - 1] + "…"
        row = Text()
        row.append("● " if marked else "  ", style="bold yellow")
        if e.is_dir:
            row.append("▸ ", style="blue")
            row.append(f"{name:<{name_w}}", style="bold yellow" if marked else "bold blue")
            row.append(f" {'':>{SIZE_W}}")
        else:
            row.append("◇ ", style=ext_style(e.name))
            style = "yellow" if marked else ("dim" if e.name.startswith(".") else "")
            row.append(f"{name:<{name_w}}", style=style)
            row.append(f" {fmt.size(e.size):>{SIZE_W}}")
        row.append(f" {fmt.when(e.mtime):>{WHEN_W}}", style="dim")
        return row

    def _render_info(self) -> None:
        left = Text()
        if self.marked:
            total = sum(e.size for e in self.entries if e.path in self.marked)
            left.append(f"{len(self.marked)} marked", style="bold yellow")
            left.append(f" · {fmt.size(total)}", style="dim")
        else:
            shown = self.visible()
            left.append(f"{len(shown)} items", style="dim")
            if self.filter_text:
                left.append(f"  filter: {self.filter_text}", style="yellow")

        right = Text(style="dim")
        if cur := self.current():
            right.append(cur.name)
            if not cur.is_dir:
                right.append(f" · {cur.size:,} B")
        width = max(self.size.width - 4, 20)
        pad = max(width - left.cell_len - right.cell_len, 1)
        self.query_one("#info", Static).update(Text.assemble(left, " " * pad, right))

    def on_resize(self) -> None:
        self._render_header()
        if self.entries or self.status != "connecting":
            self._render_rows()

    # events

    def on_descendant_focus(self) -> None:
        self.post_message(self.Activated(self))

    @on(DataTable.RowHighlighted)
    def _highlighted(self) -> None:
        self._render_info()

    @on(DataTable.RowSelected)
    def _selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key == "..":
            self.action_go_up()
            return
        entry = next((e for e in self.entries if e.path == key), None)
        if entry is None:
            return
        if entry.is_dir:
            self.cd(entry.path)
        else:
            self.post_message(self.Opened(self, entry))

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self._render_rows()

    @on(Input.Submitted, "#filter")
    def _filter_done(self) -> None:
        self.table.focus()

    # actions

    def action_go_up(self) -> None:
        if self.path != "/":
            self.cd(paths.parent(self.path), focus_name=paths.basename(self.path))

    def action_toggle_mark(self) -> None:
        entry = self.current()
        if entry is None:
            return
        self.marked ^= {entry.path}
        row = self.table.cursor_row
        self._render_rows()
        self.table.move_cursor(row=min(row + 1, self.table.row_count - 1))

    def action_mark_all(self) -> None:
        visible = {e.path for e in self.visible()}
        self.marked = set() if visible <= self.marked else self.marked | visible
        row = self.table.cursor_row
        self._render_rows()
        self.table.move_cursor(row=row)

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.remove_class("hidden")
        box.focus()

    def action_clear(self) -> None:
        if self.filter_text or not self.query_one("#filter").has_class("hidden"):
            self._clear_filter()
            self._render_rows()
            self.table.focus()
        elif self.marked:
            self.marked.clear()
            self._render_rows()

    def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self._render_rows()

    def _clear_filter(self) -> None:
        self.filter_text = ""
        if self.is_mounted:
            box = self.query_one("#filter", Input)
            box.value = ""
            box.add_class("hidden")
