from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widgets import ContentSwitcher, Static

from .. import paths, recent, sync
from ..backends import Backend, BackendError, Entry, LocalBackend
from ..backends.ssh import ssh_config_hosts
from ..config import ConfigError, Profile
from ..location import Location, parse_location
from ..transfer import Conflict, Job, State, TransferQueue, plan
from .dialogs import AskConflict, Confirm, Help, Prompt
from .dock import TransferDock
from .palette import Item, Palette
from .pane import FilePane
from .preview import Preview
from .syncview import SyncPreview

DARK = Theme(
    name="barge-dark",
    primary="#5DCAA5",
    secondary="#AFA9EC",
    accent="#5DCAA5",
    warning="#EF9F27",
    error="#F09595",
    success="#97C459",
    foreground="#d9d8d2",
    background="#141518",
    surface="#18191d",
    panel="#1f2125",
    dark=True,
)
LIGHT = Theme(
    name="barge-light",
    primary="#0F6E56",
    secondary="#534AB7",
    accent="#0F6E56",
    warning="#854F0B",
    error="#A32D2D",
    success="#3B6D11",
    foreground="#2a2925",
    background="#fbfaf7",
    surface="#f3f1ec",
    panel="#e9e6df",
    dark=False,
)

KEY_HINTS = [
    ("space", "mark"),
    ("F5", "copy"),
    ("F6", "move"),
    ("F7", "new folder"),
    ("F8", "delete"),
    ("/", "filter"),
    ("s", "sync"),
    ("p", "preview"),
    ("e", "edit"),
]

COMMANDS = [
    ("New tab", "new_tab", "ctrl+t"),
    ("Close tab", "close_tab", "ctrl+w"),
    ("Sync to other pane", "sync", "s"),
    ("Swap panes", "swap", ""),
    ("Show hidden files", "toggle_hidden", "."),
    ("Show transfers", "toggle_dock", "t"),
    ("Cancel all transfers", "cancel_transfers", "x"),
    ("Light / dark theme", "toggle_theme", ""),
    ("Keyboard shortcuts", "help", "?"),
    ("Quit", "quit", "q"),
]


class Session(Horizontal):
    def __init__(self, left: Location, right: Location, id: str) -> None:
        super().__init__(id=id, classes="session")
        self._start = (left, right)

    def compose(self) -> ComposeResult:
        yield FilePane(self._start[0], id="left")
        yield FilePane(self._start[1], id="right")

    @property
    def left(self) -> FilePane:
        return self.query_one("#left", FilePane)

    @property
    def right(self) -> FilePane:
        return self.query_one("#right", FilePane)

    @property
    def title(self) -> str:
        try:
            return f"{self.left.short_name} ↔ {self.right.short_name}"
        except Exception:
            return "…"


class BargeApp(App[None]):
    CSS_PATH = "barge.tcss"
    TITLE = "barge"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("f5", "copy", "Copy"),
        Binding("f6", "move", "Move"),
        Binding("f7", "mkdir", "New folder"),
        Binding("f8,delete", "delete", "Delete"),
        Binding("f2", "rename", "Rename"),
        Binding("p", "preview", "Preview"),
        Binding("e", "edit", "Edit"),
        Binding("s", "sync", "Sync"),
        Binding("g", "goto", "Go to"),
        Binding("t", "toggle_dock", "Transfers"),
        Binding("x", "cancel_transfers", "Cancel transfers"),
        Binding("c", "clear_finished", "Clear finished"),
        Binding("ctrl+k", "palette", "Commands"),
        Binding("ctrl+t", "new_tab", "New tab"),
        Binding("ctrl+w", "close_tab", "Close tab"),
        Binding("ctrl+pagedown,alt+right", "next_tab", "Next tab", show=False),
        Binding("ctrl+pageup,alt+left", "prev_tab", "Previous tab", show=False),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Keys"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, profiles: dict[str, Profile] | None = None, *locations: Location) -> None:
        super().__init__()
        self.profiles = profiles or {}
        self.queue = TransferQueue(workers=3)
        self.active: FilePane | None = None
        self._initial = self._initial_locations(list(locations))
        self._session_ids = 0
        self._was_busy = False
        # folders to tidy up / deletes to apply once the queue has finished the jobs they depend on
        self._after_moves: list[tuple[Backend, str, list[Job]]] = []
        self._after_sync: list[tuple[list[sync.Step], Backend, str, list[Job]]] = []
        self._tab_titles: list[str] = []

    @staticmethod
    def _initial_locations(given: list[Location]) -> tuple[Location, Location]:
        local = LocalBackend("/")
        cwd = Location(local, local.to_backend_path(os.getcwd()))
        home = Location(local, local.to_backend_path(Path.home()))
        if len(given) == 2:
            return given[0], given[1]
        if len(given) == 1:
            return cwd, given[0]
        return cwd, home

    def compose(self) -> ComposeResult:
        yield Static(id="tabbar")
        with ContentSwitcher(id="sessions"):
            yield self._new_session(*self._initial)
        yield TransferDock(self.queue)
        yield Static(id="keys")

    def on_mount(self) -> None:
        self.register_theme(DARK)
        self.register_theme(LIGHT)
        self.theme = "barge-dark"
        switcher = self.query_one(ContentSwitcher)
        switcher.current = self.sessions[0].id
        self._render_keys()
        self.set_interval(0.3, self._tick)
        self.call_after_refresh(lambda: self.session.left.table.focus())

    def on_unmount(self) -> None:
        self.queue.shutdown()
        for s in self.sessions:
            for pane in (s.left, s.right):
                pane.backend.close()

    # sessions / tabs

    def _new_session(self, left: Location, right: Location) -> Session:
        self._session_ids += 1
        return Session(left, right, id=f"session-{self._session_ids}")

    @property
    def sessions(self) -> list[Session]:
        return list(self.query(Session))

    @property
    def session(self) -> Session:
        current = self.query_one(ContentSwitcher).current
        return self.query_one(f"#{current}", Session)

    @property
    def other(self) -> FilePane:
        s = self.session
        return s.right if self.active is s.left else s.left

    def _pane(self) -> FilePane:
        if self.active is None or self.active not in (self.session.left, self.session.right):
            self.active = self.session.left
        return self.active

    @on(FilePane.Activated)
    def _activated(self, event: FilePane.Activated) -> None:
        self.active = event.pane
        for s in self.sessions:
            for pane in (s.left, s.right):
                pane.set_class(pane is event.pane, "-active")

    def _render_tabs(self) -> None:
        titles = [s.title for s in self.sessions]
        current = self.query_one(ContentSwitcher).current
        bar = Text()
        bar.append(" barge ", style="bold #5DCAA5")
        bar.append("  ")
        for s, title in zip(self.sessions, titles, strict=True):
            on_tab = s.id == current
            bar.append(f" {title} ", style="bold reverse" if on_tab else "dim")
            bar.append(" ")
        bar.append(" + ", style="dim")
        hint = Text("ctrl+k commands   ? keys ", style="dim")
        width = self.size.width - 2
        pad = max(width - bar.cell_len - hint.cell_len, 1)
        self.query_one("#tabbar", Static).update(Text.assemble(bar, " " * pad, hint))
        self._tab_titles = titles

    def _render_keys(self) -> None:
        line = Text()
        for key, what in KEY_HINTS:
            line.append(key, style="bold")
            line.append(f" {what}   ", style="dim")
        self.query_one("#keys", Static).update(line)

    def _tick(self) -> None:
        if [s.title for s in self.sessions] != self._tab_titles:
            self._render_tabs()
        busy = any(j.state in (State.QUEUED, State.RUNNING) for j in self.queue.jobs)
        if self._was_busy and not busy:
            self._queue_drained()
        self._was_busy = busy

    @work(group="cleanup")
    async def _queue_drained(self) -> None:
        moves, self._after_moves = self._after_moves, []
        syncs, self._after_sync = self._after_sync, []
        for backend, folder, jobs in moves:
            # only remove the source folder if every file in it actually moved
            if all(j.state is State.DONE for j in jobs):
                try:
                    await asyncio.to_thread(backend.delete_tree, folder)
                except BackendError as e:
                    self.notify(f"Moved the files but couldn't remove {folder}: {e}", severity="warning")
        for steps, backend, root, jobs in syncs:
            if all(j.finished_ok for j in jobs):
                errors = await asyncio.to_thread(sync.apply_deletes, steps, backend, root)
                for err in errors:
                    self.notify(err, title="Sync", severity="error")

        failed = sum(j.state is State.FAILED for j in self.queue.jobs)
        if failed:
            self.notify(f"{failed} transfer(s) failed. Press t to see why.", severity="warning")
        else:
            self.notify("Transfers finished")
        s = self.session
        s.left.refresh_listing()
        s.right.refresh_listing()

    # file operations

    def action_copy(self) -> None:
        self._transfer(move=False)

    def action_move(self) -> None:
        self._transfer(move=True)

    @work(group="ops")
    async def _transfer(self, move: bool) -> None:
        src, dst = self._pane(), self.other
        items = src.selection()
        if not items:
            return

        verb = "Move" if move else "Copy"
        same_place = src.backend is dst.backend and src.path == dst.path
        if same_place:
            self.notify(f"Both panes show the same folder. Nothing to {verb.lower()}.", severity="warning")
            return

        conflict = Conflict.OVERWRITE
        clashes = sorted(e.name for e in items if e.name in dst.names())
        if clashes:
            choice = await self.push_screen_wait(AskConflict(clashes, dst.short_name))
            if choice is None:
                return
            conflict = choice

        if move and src.backend is dst.backend:
            # same backend: a rename is instant, no need to copy bytes around
            await self._rename_across(items, src, dst, conflict)
            return

        try:
            batches = await asyncio.to_thread(
                lambda: [(e, plan(src.backend, e.path, dst.backend, dst.path, move=move)) for e in items]
            )
        except BackendError as e:
            self.notify(str(e), severity="error")
            return

        all_jobs = [j for _, jobs in batches for j in jobs]
        if not all_jobs:
            self.notify("Nothing to copy (empty folders only)")
            return
        self.queue.submit(all_jobs, conflict)
        if move:
            self._after_moves += [(src.backend, e.path, jobs) for e, jobs in batches if e.is_dir]
        src.marked.clear()
        src.refresh_listing()
        noun = "file" if len(all_jobs) == 1 else "files"
        doing = "Moving" if move else "Copying"
        self.notify(f"{doing} {len(all_jobs)} {noun} to {dst.short_name}")

    async def _rename_across(
        self, items: list[Entry], src: FilePane, dst: FilePane, conflict: Conflict
    ) -> None:
        backend = src.backend

        def go() -> list[str]:
            errors = []
            for e in items:
                target = paths.join(dst.path, e.name)
                try:
                    existing = backend.stat(target) if backend.exists(target) else None
                    if existing:
                        if conflict is Conflict.SKIP:
                            continue
                        if (
                            conflict is Conflict.NEWER
                            and e.mtime
                            and existing.mtime
                            and e.mtime <= existing.mtime
                        ):
                            continue
                        if existing.is_dir:
                            backend.delete_tree(target)
                        else:
                            backend.delete(target)
                    backend.rename(e.path, target)
                except BackendError as err:
                    errors.append(f"{e.name}: {err}")
            return errors

        errors = await asyncio.to_thread(go)
        for err in errors:
            self.notify(err, severity="error")
        src.marked.clear()
        src.refresh_listing()
        dst.refresh_listing()

    @work(group="ops")
    async def action_delete(self) -> None:
        pane = self._pane()
        items = pane.selection()
        if not items:
            return
        what = f'"{items[0].name}"' if len(items) == 1 else f"{len(items)} items"
        body = ", ".join(e.name + ("/" if e.is_dir else "") for e in items[:6])
        if len(items) > 6:
            body += f" and {len(items) - 6} more"
        body += f"\n\nfrom {pane.short_name}. This can't be undone."
        if not await self.push_screen_wait(Confirm(f"Delete {what}?", body, yes="Delete", danger=True)):
            return

        def go() -> list[str]:
            errors = []
            for e in items:
                try:
                    if e.is_dir:
                        pane.backend.delete_tree(e.path)
                    else:
                        pane.backend.delete(e.path)
                except BackendError as err:
                    errors.append(f"{e.name}: {err}")
            return errors

        for err in await asyncio.to_thread(go):
            self.notify(err, severity="error")
        pane.marked.clear()
        pane.refresh_listing(keep_cursor=False)

    @work(group="ops")
    async def action_mkdir(self) -> None:
        pane = self._pane()
        name = await self.push_screen_wait(Prompt("New folder", placeholder="folder name"))
        if not name:
            return
        if "/" in name:
            self.notify("Folder names can't contain /", severity="error")
            return
        try:
            await asyncio.to_thread(pane.backend.mkdir, paths.join(pane.path, name))
        except BackendError as e:
            self.notify(str(e), severity="error")
            return
        pane._restore_cursor = name
        pane.refresh_listing(keep_cursor=False)

    @work(group="ops")
    async def action_rename(self) -> None:
        pane = self._pane()
        entry = pane.current()
        if entry is None:
            return
        name = await self.push_screen_wait(Prompt("Rename", value=entry.name))
        if not name or name == entry.name:
            return
        try:
            await asyncio.to_thread(pane.backend.rename, entry.path, paths.join(pane.path, name))
        except BackendError as e:
            self.notify(str(e), severity="error")
            return
        pane._restore_cursor = name
        pane.refresh_listing(keep_cursor=False)

    def action_preview(self) -> None:
        pane = self._pane()
        entry = pane.current()
        if entry and not entry.is_dir:
            self.push_screen(Preview(pane.backend, entry))

    @on(FilePane.Opened)
    def _opened(self, event: FilePane.Opened) -> None:
        self.push_screen(Preview(event.pane.backend, event.entry))

    @work(group="ops", exclusive=True)
    async def action_edit(self) -> None:
        pane = self._pane()
        entry = pane.current()
        if entry is None or entry.is_dir:
            return
        backend = pane.backend
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"

        with tempfile.TemporaryDirectory(prefix="barge-") as tmp:
            local = Path(tmp) / entry.name

            def download() -> str:
                with backend.open_read(entry.path) as f:
                    data = f.read()
                local.write_bytes(data)
                return hashlib.sha256(data).hexdigest()

            try:
                before = await asyncio.to_thread(download)
            except BackendError as e:
                self.notify(str(e), severity="error")
                return

            with self.suspend():
                subprocess.call([*shlex.split(editor), str(local)])

            data = local.read_bytes()
            if hashlib.sha256(data).hexdigest() == before:
                self.notify("No changes")
                return

            def upload() -> None:
                with open(local, "rb") as f:
                    backend.write(entry.path, f, size=len(data))

            try:
                await asyncio.to_thread(upload)
            except BackendError as e:
                self.notify(f"Upload failed: {e}", severity="error")
                return
        self.notify(f"Saved {entry.name} to {pane.short_name}")
        pane.refresh_listing()

    @work(group="ops")
    async def action_sync(self) -> None:
        src, dst = self._pane(), self.other
        steps = await self.push_screen_wait(SyncPreview(src.location, dst.location))
        if not steps:
            return
        jobs = sync.jobs_for(steps, src.backend, src.path, dst.backend, dst.path)
        self.queue.submit(jobs)
        deletes = [s for s in steps if s.action is sync.Action.DELETE]
        if deletes and jobs:
            self._after_sync.append((deletes, dst.backend, dst.path, jobs))
        elif deletes:
            errors = await asyncio.to_thread(sync.apply_deletes, deletes, dst.backend, dst.path)
            for err in errors:
                self.notify(err, severity="error")
            dst.refresh_listing()

    # navigation

    @work(group="nav")
    async def action_goto(self) -> None:
        pane = self._pane()
        spec = await self.push_screen_wait(
            Prompt(
                "Go to",
                value=str(pane.location) if pane.backend.scheme != "local" else pane.path,
                hint="photos:/2024 · web-01:/var/www · s3://bucket/key · sftp://user@host/path · ~/Downloads",
            )
        )
        if spec:
            await self._open(spec, pane)

    async def _open(self, spec: str, pane: FilePane) -> None:
        try:
            loc = await asyncio.to_thread(parse_location, spec, self.profiles)
        except (BackendError, ConfigError) as e:
            self.notify(str(e), severity="error")
            return
        pane.set_location(loc)
        recent.remember(spec)

    @work(group="nav")
    async def action_palette(self) -> None:
        item = await self.push_screen_wait(Palette(self._palette_items()))
        if item is None:
            return
        if item.action == "open":
            await self._open(item.value, self._pane())
        else:
            await self.run_action(item.action)

    def _palette_items(self) -> list[Item]:
        items = [Item("Connections", "Home", str(Path.home()), "local", "open", str(Path.home()))]
        for p in self.profiles.values():
            kind = {"s3": "s3", "local": "local"}.get(p.type) or (
                "scp" if p.options.get("mode") == "scp" else "sftp"
            )
            items.append(Item("Connections", p.name, p.describe(), kind, "open", f"{p.name}:/"))
        for host in ssh_config_hosts():
            if host not in self.profiles:
                items.append(Item("Connections", host, "~/.ssh/config", "ssh", "open", f"{host}:/"))
        for spec in recent.load()[:8]:
            items.append(Item("Recent", spec, action="open", value=spec))
        for title, action, key in COMMANDS:
            items.append(Item("Commands", title, key, action=action))
        return items

    def action_refresh(self) -> None:
        self._pane().refresh_listing()
        self.other.refresh_listing()

    def action_toggle_hidden(self) -> None:
        self._pane().action_toggle_hidden()

    def action_swap(self) -> None:
        s = self.session
        left, right = s.left.location, s.right.location
        s.left.backend, s.right.backend = right.backend, left.backend
        s.left.cd(right.path)
        s.right.cd(left.path)

    def action_new_tab(self) -> None:
        session = self._new_session(*self._initial_locations([]))
        switcher = self.query_one(ContentSwitcher)
        switcher.mount(session)
        switcher.current = session.id
        self.call_after_refresh(lambda: session.left.table.focus())
        self._render_tabs()

    def action_close_tab(self) -> None:
        sessions = self.sessions
        if len(sessions) == 1:
            self.notify("That's the last tab. Press q to quit.")
            return
        current = self.session
        idx = sessions.index(current)
        nxt = sessions[idx - 1] if idx else sessions[1]
        self.query_one(ContentSwitcher).current = nxt.id
        for pane in (current.left, current.right):
            pane.backend.close()
        current.remove()
        self.call_after_refresh(lambda: nxt.left.table.focus())
        self.call_after_refresh(self._render_tabs)

    def _cycle_tab(self, step: int) -> None:
        sessions = self.sessions
        idx = sessions.index(self.session)
        target = sessions[(idx + step) % len(sessions)]
        self.query_one(ContentSwitcher).current = target.id
        target.left.table.focus()
        self._render_tabs()

    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    def action_toggle_dock(self) -> None:
        dock = self.query_one(TransferDock)
        dock.pinned_open = not dock.display
        dock.redraw()

    def action_cancel_transfers(self) -> None:
        if any(j.state in (State.QUEUED, State.RUNNING) for j in self.queue.jobs):
            self.queue.cancel()
            self.notify("Cancelling transfers")

    def action_clear_finished(self) -> None:
        self.queue.clear_finished()
        self.query_one(TransferDock).redraw()

    def action_toggle_theme(self) -> None:
        self.theme = "barge-light" if self.theme == "barge-dark" else "barge-dark"
        for s in self.sessions:
            for pane in (s.left, s.right):
                pane._render_header()

    def action_help(self) -> None:
        self.push_screen(Help(classes="dialog-screen"))

    def on_resize(self) -> None:
        self._render_tabs()
