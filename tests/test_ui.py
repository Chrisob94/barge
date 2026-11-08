import pytest

from barge.backends import LocalBackend
from barge.location import Location
from barge.ui.app import BargeApp
from barge.ui.dialogs import AskConflict, Confirm, Help
from barge.ui.palette import Palette
from barge.ui.syncview import SyncPreview


@pytest.fixture
def dirs(tmp_path):
    (tmp_path / "left/docs").mkdir(parents=True)
    (tmp_path / "left/a.txt").write_text("a")
    (tmp_path / "left/b.txt").write_text("bb")
    (tmp_path / "left/docs/c.md").write_text("c")
    (tmp_path / "right").mkdir()
    return tmp_path


def make_app(root, left=None, right=None):
    # separate backend objects so copies go through the transfer queue
    return BargeApp(
        {}, Location(left or LocalBackend(root), "/left"), Location(right or LocalBackend(root), "/right")
    )


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def wait_for(pilot, check, tries=100):
    # for things that finish while a worker is still waiting on a dialog
    for _ in range(tries):
        if check():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out")


async def test_copy_file_with_f5(dirs):
    app = make_app(dirs)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await pilot.press("down", "down")  # .., docs, a.txt
        await pilot.press("f5")
        await settle(app, pilot)
        assert app.queue.wait(5)
        assert (dirs / "right/a.txt").read_text() == "a"


async def test_copy_folder_and_conflict_skip(dirs):
    (dirs / "right/a.txt").write_text("keep me")
    app = make_app(dirs)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await pilot.press("down", "space", "space")  # mark docs and a.txt
        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, AskConflict)
        await pilot.press("s")
        await settle(app, pilot)
        assert app.queue.wait(5)
        assert (dirs / "right/docs/c.md").read_text() == "c"
        assert (dirs / "right/a.txt").read_text() == "keep me"


async def test_delete_needs_confirmation(dirs):
    app = make_app(dirs)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await pilot.press("down", "down", "f8")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        await pilot.press("escape")
        await settle(app, pilot)
        assert (dirs / "left/a.txt").exists()

        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert not (dirs / "left/a.txt").exists()


async def test_mkdir_and_rename(dirs):
    app = make_app(dirs)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await pilot.press("f7")
        await pilot.pause()
        await pilot.press(*"photos", "enter")
        await settle(app, pilot)
        assert (dirs / "left/photos").is_dir()

        pane = app.session.left
        assert pane.current().name == "photos"
        await pilot.press("f2")
        await pilot.pause()
        await pilot.press(*"pics", "enter")  # the dialog pre-selects the old name
        await settle(app, pilot)
        assert (dirs / "left/pics").is_dir()


async def test_move_on_same_backend_is_a_rename(dirs):
    shared = LocalBackend(dirs)
    app = make_app(dirs, shared, shared)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await pilot.press("down", "down", "f6")
        await settle(app, pilot)
        assert not (dirs / "left/a.txt").exists()
        assert (dirs / "right/a.txt").read_text() == "a"
        assert app.queue.jobs == []


async def test_filter_and_navigation(dirs):
    app = make_app(dirs)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        pane = app.session.left
        await pilot.press("slash", "b")
        await pilot.pause()
        assert [e.name for e in pane.visible()] == ["b.txt"]
        await pilot.press("escape")
        await pilot.pause()
        assert len(pane.visible()) == 3

        await pilot.press("down", "enter")  # into docs
        await settle(app, pilot)
        assert pane.path == "/left/docs"
        await pilot.press("backspace")
        await settle(app, pilot)
        assert pane.path == "/left"
        assert pane.current().name == "docs"


async def test_palette_runs_commands(dirs):
    app = make_app(dirs)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert isinstance(app.screen, Palette)
        await pilot.press(*"keyboard", "enter")
        await pilot.pause()
        assert isinstance(app.screen, Help)


async def test_sync_preview_and_run(dirs):
    app = make_app(dirs)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await pilot.press("s")
        await pilot.pause()
        preview = app.screen
        assert isinstance(preview, SyncPreview)
        await wait_for(pilot, lambda: preview.steps)
        assert {s.rel for s in preview.steps} == {"a.txt", "b.txt", "docs/c.md"}
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.queue.wait(5)
        assert (dirs / "right/docs/c.md").exists()


async def test_tabs(dirs):
    app = make_app(dirs)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+t")
        await settle(app, pilot)
        assert len(app.sessions) == 2
        await pilot.press("ctrl+w")
        await settle(app, pilot)
        assert len(app.sessions) == 1
