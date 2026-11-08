from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import fuzzy

KIND_STYLES = {
    "sftp": "bold #CECBF6 on #3C3489",
    "ssh": "bold #CECBF6 on #3C3489",
    "scp": "bold #CECBF6 on #3C3489",
    "s3": "bold #9FE1CB on #085041",
    "local": "bold #B5D4F4 on #0C447C",
}


@dataclass(frozen=True)
class Item:
    group: str  # "Connections", "Recent", "Commands"
    title: str
    detail: str = ""
    kind: str = ""  # badge text for connections
    action: str = ""  # "open" or a command name
    value: str = ""  # location spec for "open"


class Palette(ModalScreen[Item | None]):
    BINDINGS = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("down", "move(1)", show=False),
        Binding("up", "move(-1)", show=False),
    ]

    def __init__(self, items: list[Item]) -> None:
        super().__init__(classes="dialog-screen palette-screen")
        self.items = items
        self.shown: list[Item] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="palette"):
            yield Input(placeholder="connection, recent place, command, or s3://… sftp://…")
            yield OptionList()
            yield Static("enter open here · esc close", classes="palette-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._refresh("")

    @on(Input.Changed)
    def _changed(self, event: Input.Changed) -> None:
        self._refresh(event.value)

    @on(Input.Submitted)
    def _submitted(self) -> None:
        options = self.query_one(OptionList)
        if options.highlighted is not None and self.shown:
            self.dismiss(self.shown[options.highlighted])

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.shown[event.option_index])

    def action_move(self, delta: int) -> None:
        options = self.query_one(OptionList)
        if options.option_count:
            current = options.highlighted or 0
            options.highlighted = (current + delta) % options.option_count

    def _refresh(self, query: str) -> None:
        query = query.strip()
        candidates = list(self.items)
        if query and ("://" in query or query.startswith(("/", "~", "."))):
            candidates.insert(0, Item("Go to", query, action="open", value=query))

        if query:
            by_title = {i.title: i for i in candidates}
            ranked = fuzzy.rank(query, [i.title for i in candidates])
            matches = [(by_title[t], pos) for t, pos in ranked]
        else:
            matches = [(i, []) for i in candidates]

        options = self.query_one(OptionList)
        options.clear_options()
        self.shown = []
        group = None
        rows: list[Option] = []
        for item, pos in matches[:40]:
            # group headings only make sense when we aren't sorting by score
            if not query and item.group != group:
                group = item.group
                rows.append(Option(Text(item.group, style="dim italic"), disabled=True))
                self.shown.append(item)  # placeholder keeps indexes lined up
            rows.append(Option(self._line(item, pos)))
            self.shown.append(item)
        options.add_options(rows)
        first = next((n for n, r in enumerate(rows) if not r.disabled), None)
        options.highlighted = first

    def _line(self, item: Item, pos: list[int]) -> Text:
        line = Text()
        if item.kind:
            line.append(f" {item.kind:^5} ", style=KIND_STYLES.get(item.kind, "reverse"))
            line.append(" ")
        elif item.group == "Recent":
            line.append("  ↺    ", style="dim")
        else:
            line.append("  ›    ", style="dim")
        title = Text(item.title)
        for p in pos:
            title.stylize("bold underline #FAC775", p, p + 1)
        line.append_text(title)
        if item.detail:
            line.append(f"  {item.detail}", style="dim")
        return line
