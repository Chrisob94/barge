from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, LoadingIndicator, Static

from .. import fmt, sync
from ..location import Location
from . import names

STYLE = {
    sync.Action.NEW: ("+", "green"),
    sync.Action.CHANGED: ("~", "yellow"),
    sync.Action.DELETE: ("−", "red"),
}
HEADINGS = {sync.Action.NEW: "New", sync.Action.CHANGED: "Changed", sync.Action.DELETE: "Delete on target"}


class SyncPreview(ModalScreen[list[sync.Step] | None]):
    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("enter", "run", "Run"),
        Binding("space", "skip", "Skip item"),
        Binding("d", "toggle_delete", "Delete extras"),
    ]

    def __init__(self, src: Location, dst: Location) -> None:
        super().__init__(classes="dialog-screen")
        self.src = src
        self.dst = dst
        self.delete = False
        self.steps: list[sync.Step] = []
        self.skipped: set[str] = set()
        self._src_index: dict = {}
        self._dst_index: dict = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog sync"):
            yield Static(classes="sync-title")
            yield LoadingIndicator()
            yield DataTable(show_header=False, cursor_type="row")
            yield Static(classes="sync-footer")

    def on_mount(self) -> None:
        dark = self.app.current_theme.dark
        title = Text.assemble(
            ("Sync  ", "bold"),
            (names.where(self.src.backend, self.src.path), names.color(self.src.backend, dark)),
            ("  →  ", "dim"),
            (names.where(self.dst.backend, self.dst.path), names.color(self.dst.backend, dark)),
        )
        self.query_one(".sync-title", Static).update(title)
        table = self.query_one(DataTable)
        table.add_column("row", key="row")
        table.display = False
        self._scan()

    @work(thread=True)
    def _scan(self) -> None:
        try:
            src = sync.index(self.src.backend, self.src.path)
            dst = sync.index(self.dst.backend, self.dst.path)
        except Exception as e:
            self.app.call_from_thread(self.app.notify, str(e), severity="error")
            self.app.call_from_thread(self.dismiss, None)
            return
        self.app.call_from_thread(self._scanned, src, dst)

    def _scanned(self, src: dict, dst: dict) -> None:
        self._src_index, self._dst_index = src, dst
        self.query_one(LoadingIndicator).display = False
        table = self.query_one(DataTable)
        table.display = True
        table.focus()
        self._rebuild()

    def _rebuild(self) -> None:
        self.steps = sync.diff(self._src_index, self._dst_index, delete=self.delete)
        table = self.query_one(DataTable)
        table.clear()
        width = max(table.size.width - 4, 40)
        for action in sync.Action:
            group = [s for s in self.steps if s.action is action]
            if not group:
                continue
            sym, style = STYLE[action]
            table.add_row(
                Text(f"{HEADINGS[action]} · {len(group)}", style=f"bold {style}"), key=f"h:{action}"
            )
            for step in group:
                table.add_row(self._row(step, sym, style, width), key=step.rel)
        if not self.steps:
            table.add_row(Text("Already in sync.", style="green"), key="h:none")
        elif table.cursor_row == 0:
            table.move_cursor(row=1)  # skip the first heading
        self._footer()

    def _row(self, step: sync.Step, sym: str, style: str, width: int) -> Text:
        skipped = step.rel in self.skipped
        detail = "skipped" if skipped else (step.reason or fmt.size(step.size))
        name_w = width - len(detail) - 4
        name = step.rel if len(step.rel) <= name_w else "…" + step.rel[-(name_w - 1) :]
        row = Text(f" {sym} ", style=style)
        row.append(f"{name:<{name_w}}", style="dim strike" if skipped else "")
        row.append(f" {detail}", style="dim")
        return row

    def _footer(self) -> None:
        counts = {a: sum(1 for s in self.chosen() if s.action is a) for a in sync.Action}
        send = sum(s.size for s in self.chosen() if s.action is not sync.Action.DELETE)
        same = sync.unchanged_count(self._src_index, self.steps)
        line = Text()
        line.append(f"{counts[sync.Action.NEW]} new  ", style="green")
        line.append(f"{counts[sync.Action.CHANGED]} changed  ", style="yellow")
        line.append(f"{counts[sync.Action.DELETE]} to delete", style="red")
        line.append(f"  ·  {same} unchanged · {fmt.size(send)} to send", style="dim")
        line.append("\n")
        line.append("enter", style="bold")
        line.append(" run   ", style="dim")
        line.append("space", style="bold")
        line.append(" skip item   ", style="dim")
        line.append("d", style="bold")
        line.append(f" delete extras: {'on' if self.delete else 'off'}   ", style="dim")
        line.append("esc", style="bold")
        line.append(" cancel", style="dim")
        self.query_one(".sync-footer", Static).update(line)

    def chosen(self) -> list[sync.Step]:
        return [s for s in self.steps if s.rel not in self.skipped]

    def action_skip(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value or ""
        if key.startswith("h:"):
            return
        self.skipped ^= {key}
        row = table.cursor_row
        self._rebuild()
        table.move_cursor(row=min(row + 1, table.row_count - 1))

    def action_toggle_delete(self) -> None:
        self.delete = not self.delete
        self._rebuild()

    @on(DataTable.RowSelected)
    def _enter(self) -> None:
        self.action_run()

    def action_run(self) -> None:
        if self._src_index or self._dst_index:
            self.dismiss(self.chosen())
