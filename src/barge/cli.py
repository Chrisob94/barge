from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__, fmt, sync
from .backends import BackendError
from .backends.ssh import ssh_config_hosts
from .config import ConfigError, load_profiles
from .location import Location, parse_location
from .transfer import Conflict, State, TransferQueue, plan

COMMANDS = {"ls", "cp", "mv", "rm", "mkdir", "sync", "hosts"}


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        return run_tui(argv)

    args = build_parser().parse_args(argv)
    try:
        profiles = load_profiles()
        return args.func(args, profiles)
    except (BackendError, ConfigError) as e:
        print(f"barge: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="barge",
        description="Move files between your disk, S3 and SSH servers. Run with no arguments for the TUI.",
    )
    p.add_argument("--version", action="version", version=f"barge {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("ls", help="list a directory")
    ls.add_argument("location")
    ls.add_argument("-l", "--long", action="store_true", help="show size and modified time")
    ls.set_defaults(func=cmd_ls)

    for name, helptext in (("cp", "copy files or folders"), ("mv", "move files or folders")):
        c = sub.add_parser(name, help=helptext)
        c.add_argument("sources", nargs="+")
        c.add_argument("dest", help="destination folder")
        c.add_argument("-j", "--jobs", type=int, default=4, help="parallel transfers (default 4)")
        c.add_argument("--skip-existing", action="store_true")
        c.add_argument("-q", "--quiet", action="store_true")
        c.set_defaults(func=cmd_copy, move=name == "mv")

    rm = sub.add_parser("rm", help="delete files or folders")
    rm.add_argument("locations", nargs="+")
    rm.add_argument("-r", "--recursive", action="store_true")
    rm.set_defaults(func=cmd_rm)

    mk = sub.add_parser("mkdir", help="create a folder (and parents)")
    mk.add_argument("location")
    mk.set_defaults(func=cmd_mkdir)

    sy = sub.add_parser("sync", help="make DEST match SRC")
    sy.add_argument("src")
    sy.add_argument("dest")
    sy.add_argument("--delete", action="store_true", help="delete files in DEST that aren't in SRC")
    sy.add_argument("--size-only", action="store_true", help="ignore modification times")
    sy.add_argument("-n", "--dry-run", action="store_true")
    sy.add_argument("-j", "--jobs", type=int, default=4)
    sy.set_defaults(func=cmd_sync)

    hosts = sub.add_parser("hosts", help="list profiles and ssh hosts")
    hosts.set_defaults(func=cmd_hosts)
    return p


def run_tui(argv: list[str]) -> int:
    from .ui.app import BargeApp

    if len(argv) > 2:
        print("usage: barge [LEFT] [RIGHT]   (or barge --help for commands)", file=sys.stderr)
        return 2
    try:
        profiles = load_profiles()
        locations = [parse_location(a, profiles) for a in argv]
    except (BackendError, ConfigError) as e:
        print(f"barge: {e}", file=sys.stderr)
        return 1
    BargeApp(profiles, *locations).run()
    return 0


def cmd_ls(args, profiles) -> int:
    loc = parse_location(args.location, profiles)
    with loc.backend:
        for e in loc.backend.list_dir(loc.path):
            name = e.name + ("/" if e.is_dir else "")
            if args.long:
                size = "-" if e.is_dir else fmt.size(e.size)
                print(f"{size:>7}  {fmt.when(e.mtime):>8}  {name}")
            else:
                print(name)
    return 0


def cmd_copy(args, profiles) -> int:
    dest = parse_location(args.dest, profiles)
    sources = [parse_location(s, profiles) for s in args.sources]
    jobs = []
    for src in sources:
        jobs += plan(src.backend, src.path, dest.backend, dest.path, move=args.move)
    if not jobs:
        print("nothing to copy")
        return 0

    def report(job):
        if args.quiet or job.state not in (State.DONE, State.FAILED, State.SKIPPED):
            return
        mark = {"done": "✓", "skipped": "-", "failed": "✗"}[job.state]
        extra = f"  ({job.error})" if job.error else ""
        print(f"{mark} {job.src_path} → {job.dst_path}{extra}")

    queue = TransferQueue(workers=args.jobs, on_change=report)
    conflict = Conflict.SKIP if args.skip_existing else Conflict.OVERWRITE
    queue.submit(jobs, conflict)
    queue.wait()
    queue.shutdown()
    failed = [j for j in jobs if j.state is State.FAILED]
    if failed:
        print(f"{len(failed)} of {len(jobs)} failed", file=sys.stderr)
    _close(dest, *sources)
    return 1 if failed else 0


def cmd_rm(args, profiles) -> int:
    for spec in args.locations:
        loc = parse_location(spec, profiles)
        if loc.backend.stat(loc.path).is_dir:
            if not args.recursive:
                print(f"barge: {spec} is a folder (use -r)", file=sys.stderr)
                return 1
            loc.backend.delete_tree(loc.path)
        else:
            loc.backend.delete(loc.path)
        loc.backend.close()
    return 0


def cmd_mkdir(args, profiles) -> int:
    loc = parse_location(args.location, profiles)
    loc.backend.mkdir(loc.path)
    loc.backend.close()
    return 0


def cmd_sync(args, profiles) -> int:
    src = parse_location(args.src, profiles)
    dst = parse_location(args.dest, profiles)
    src_index = sync.index(src.backend, src.path)
    steps = sync.diff(
        src_index, sync.index(dst.backend, dst.path), delete=args.delete, use_mtime=not args.size_only
    )

    symbols = {"new": "+", "changed": "~", "delete": "-"}
    for s in steps:
        print(f"{symbols[s.action]} {s.rel}" + (f"  ({s.reason})" if s.reason else ""))
    print(f"{len(steps)} changes, {sync.unchanged_count(src_index, steps)} unchanged")
    if args.dry_run or not steps:
        _close(src, dst)
        return 0

    queue = TransferQueue(workers=args.jobs)
    jobs = queue.submit(sync.jobs_for(steps, src.backend, src.path, dst.backend, dst.path))
    queue.wait()
    queue.shutdown()
    failed = [j for j in jobs if j.state is State.FAILED]
    for j in failed:
        print(f"✗ {j.src_path}: {j.error}", file=sys.stderr)
    # only delete once every copy has landed, so a failed run can't leave dest emptier
    errors = [] if failed else sync.apply_deletes(steps, dst.backend, dst.path)
    for err in errors:
        print(f"✗ delete {err}", file=sys.stderr)
    _close(src, dst)
    return 1 if failed or errors else 0


def cmd_hosts(args, profiles) -> int:
    for p in profiles.values():
        print(f"{p.name:<20} {p.type:<6} {p.describe()}")
    for h in ssh_config_hosts():
        if h not in profiles:
            print(f"{h:<20} {'ssh':<6} ~/.ssh/config")
    return 0


def _close(*locations: Location) -> None:
    for loc in locations:
        loc.backend.close()
