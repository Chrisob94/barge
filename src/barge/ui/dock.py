from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from .. import fmt
from ..transfer import Job, State, TransferQueue
from . import names

ROWS = 4
BAR_W = 22


def bar(fraction: float, width: int = BAR_W) -> Text:
    filled = round(fraction * width)
    return Text.assemble(("━" * filled, "#1D9E75"), ("━" * (width - filled), "#2a2c31"))


class TransferDock(Static):
    """Bottom panel showing the transfer queue. Redrawn on a timer, not per progress tick."""

    def __init__(self, queue: TransferQueue) -> None:
        super().__init__(classes="dock")
        self.queue = queue
        self.pinned_open = False

    def on_mount(self) -> None:
        self.set_interval(0.25, self.redraw)
        self.redraw()

    def redraw(self) -> None:
        jobs = list(self.queue.jobs)
        active = [j for j in jobs if j.state in (State.RUNNING, State.QUEUED)]
        failed = [j for j in jobs if j.state is State.FAILED]
        self.display = bool(active or failed) or self.pinned_open
        if not self.display:
            return

        s = self.queue.stats()
        head = Text()
        head.append("Transfers", style="bold")
        head.append(
            f"  {s['running']:.0f} active · {s['queued']:.0f} queued · {s['done']:.0f} done", style="dim"
        )
        if failed:
            head.append(f" · {len(failed)} failed", style="bold red")
        right = Text(style="dim")
        if s["speed"]:
            right.append(f"{fmt.rate(s['speed'])} · {fmt.duration(s['eta'])} left · ")
        right.append("t", style="bold")
        right.append(" hide  x cancel  c clear")

        top = Table.grid(expand=True)
        top.add_column()
        top.add_column(justify="right")
        top.add_row(head, right)

        rows = Table.grid(expand=True, padding=(0, 1))
        rows.add_column(width=2)
        rows.add_column(ratio=1, no_wrap=True)
        rows.add_column(width=BAR_W)
        rows.add_column(width=10, justify="right")
        # failures first so they don't scroll out of view, then whatever's moving
        order = (
            failed
            + [j for j in active if j.state is State.RUNNING]
            + [j for j in active if j.state is State.QUEUED]
        )
        for job in order[:ROWS]:
            rows.add_row(*self._row(job))
        if len(order) > ROWS:
            rows.add_row("", Text(f"+ {len(order) - ROWS} more", style="dim"), "", "")
        if not order:
            rows.add_row("", Text("Nothing running", style="dim"), "", "")

        outer = Table.grid(expand=True)
        outer.add_row(top)
        outer.add_row(rows)
        self.update(outer)

    def _row(self, job: Job) -> tuple:
        arrow = {State.RUNNING: ("↑", "#5DCAA5"), State.QUEUED: ("·", "dim"), State.FAILED: ("✗", "bold red")}
        icon, style = arrow.get(job.state, ("✓", "green"))
        name = Text(job.name)
        name.append(f"  → {names.where(job.dst, job.dst_path)}", style="dim")
        if job.state is State.FAILED:
            return Text(icon, style=style), name, Text(job.error or "failed", style="red"), Text("")
        pct = "queued" if job.state is State.QUEUED else f"{job.fraction:.0%}"
        return Text(icon, style=style), name, bar(job.fraction), Text(pct, style="dim")
