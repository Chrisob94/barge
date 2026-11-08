from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..transfer import Conflict


class Confirm(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape,n", "dismiss(False)", "Cancel"),
        Binding("y", "dismiss(True)", "Yes"),
    ]

    def __init__(self, title: str, body: str = "", yes: str = "OK", danger: bool = False) -> None:
        super().__init__(classes="dialog-screen")
        self.title_text = title
        self.body = body
        self.yes = yes
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            if self.body:
                yield Static(self.body, classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="no")
                yield Button(self.yes, id="yes", variant="error" if self.danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#yes" if not self.danger else "#no", Button).focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class Prompt(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, value: str = "", placeholder: str = "", hint: str = "") -> None:
        super().__init__(classes="dialog-screen")
        self.title_text = title
        self.value = value
        self.placeholder = placeholder
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Input(self.value, placeholder=self.placeholder)
            if self.hint:
                yield Static(self.hint, classes="dialog-hint")

    def on_mount(self) -> None:
        box = self.query_one(Input)
        box.focus()
        # select the name but not the extension, like most file managers
        stem = self.value.rsplit(".", 1)[0] if "." in self.value[1:] else self.value
        box.selection = (0, len(stem))  # type: ignore[assignment]

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)


class AskConflict(ModalScreen[Conflict | None]):
    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("o", "pick('overwrite')", "Overwrite"),
        Binding("s", "pick('skip')", "Skip"),
        Binding("k", "pick('newer')", "Keep newer"),
    ]

    def __init__(self, names: list[str], where: str) -> None:
        super().__init__(classes="dialog-screen")
        self.names = names
        self.where = where

    def compose(self) -> ComposeResult:
        n = len(self.names)
        shown = ", ".join(self.names[:3]) + (f" and {n - 3} more" if n > 3 else "")
        with Vertical(classes="dialog"):
            yield Label(
                f"{n} item{'s' if n != 1 else ''} already exist in {self.where}", classes="dialog-title"
            )
            yield Static(shown, classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Skip", id="skip")
                yield Button("Keep newer", id="newer")
                yield Button("Overwrite", id="overwrite", variant="warning")
            yield Static("applies to everything in this copy · esc cancels", classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#skip", Button).focus()

    def action_pick(self, value: str) -> None:
        self.dismiss(Conflict(value))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(Conflict(event.button.id))


KEYS = [
    (
        "Navigate",
        [
            ("↑ ↓", "move"),
            ("enter", "open folder / preview file"),
            ("backspace", "up a level"),
            ("tab", "other pane"),
            ("/", "filter"),
            (".", "show hidden files"),
        ],
    ),
    ("Select", [("space", "mark"), ("ctrl+a", "mark all"), ("esc", "clear marks / filter")]),
    (
        "Files",
        [
            ("F5", "copy to other pane"),
            ("F6", "move to other pane"),
            ("F7", "new folder"),
            ("F8", "delete"),
            ("F2", "rename"),
            ("p", "preview"),
            ("e", "edit in $EDITOR"),
        ],
    ),
    (
        "Barge",
        [
            ("ctrl+k", "commands and connections"),
            ("g", "go to a location"),
            ("s", "sync to other pane"),
            ("t", "show / hide transfers"),
            ("ctrl+t", "new tab"),
            ("ctrl+w", "close tab"),
            ("ctrl+r", "refresh"),
            ("q", "quit"),
        ],
    ),
]


class Help(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark,q", "dismiss(None)", "Close")]

    def compose(self) -> ComposeResult:
        lines = []
        for section, keys in KEYS:
            lines.append(f"[b]{section}[/b]")
            lines += [f"  [$accent]{k:<11}[/] {what}" for k, what in keys]
            lines.append("")
        with Vertical(classes="dialog help"):
            yield Static("\n".join(lines).rstrip())
