from __future__ import annotations

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import LoadingIndicator, Static

from .. import fmt
from ..backends import Backend, Entry

MAX_BYTES = 256 * 1024


class Preview(ModalScreen[None]):
    BINDINGS = [Binding("escape,p,q", "dismiss(None)", "Close")]

    def __init__(self, backend: Backend, entry: Entry) -> None:
        super().__init__(classes="dialog-screen")
        self.backend = backend
        self.entry = entry

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog preview"):
            yield Static(classes="preview-title")
            yield LoadingIndicator()
            with VerticalScroll():
                yield Static(id="body")

    def on_mount(self) -> None:
        e = self.entry
        title = Text.assemble((e.name, "bold"), (f"   {fmt.size(e.size)} · {fmt.when(e.mtime)}", "dim"))
        self.query_one(".preview-title", Static).update(title)
        self._read()

    @work(thread=True)
    def _read(self) -> None:
        try:
            with self.backend.open_read(self.entry.path) as f:
                data = f.read(MAX_BYTES)
        except Exception as e:
            self.app.call_from_thread(self._show, Text(str(e), style="red"))
            return
        self.app.call_from_thread(self._show, render(self.entry.name, data, self.entry.size))

    def _show(self, renderable) -> None:
        self.query_one(LoadingIndicator).display = False
        self.query_one("#body", Static).update(renderable)


def render(name: str, data: bytes, size: int):
    if b"\x00" in data[:4096]:
        return hexdump(data[:512])
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return hexdump(data[:512])
    lexer = Syntax.guess_lexer(name, code=text[:2000])
    out = Syntax(text, lexer, line_numbers=True, word_wrap=False, theme="ansi_dark")
    if size > len(data):
        return Group(Text(f"first {fmt.size(len(data))} of {fmt.size(size)}\n", style="dim"), out)
    return out


def hexdump(data: bytes) -> Text:
    out = Text()
    out.append("binary file\n\n", style="dim")
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        hexes = " ".join(f"{b:02x}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off:08x}  ", style="dim")
        out.append(f"{hexes:<48}  ")
        out.append(f"{ascii_}\n", style="cyan")
    return out
