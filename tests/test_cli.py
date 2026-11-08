from barge.cli import main


def test_ls_and_cp_local(tmp_path, capsys):
    src = tmp_path / "src"
    (src / "docs").mkdir(parents=True)
    (src / "docs/readme.md").write_text("hi")
    (src / "a.txt").write_text("aaa")

    assert main(["ls", str(src)]) == 0
    assert capsys.readouterr().out.split() == ["docs/", "a.txt"]

    dst = tmp_path / "dst"
    dst.mkdir()
    assert main(["cp", "-q", str(src / "docs"), str(src / "a.txt"), str(dst)]) == 0
    assert (dst / "docs/readme.md").read_text() == "hi"
    assert (dst / "a.txt").read_text() == "aaa"


def test_mv_and_rm(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "out").mkdir()
    assert main(["mv", "-q", str(tmp_path / "a.txt"), str(tmp_path / "out")]) == 0
    assert not (tmp_path / "a.txt").exists()
    assert main(["rm", str(tmp_path / "out")]) == 1  # folder without -r
    assert main(["rm", "-r", str(tmp_path / "out")]) == 0
    assert not (tmp_path / "out").exists()


def test_sync_to_s3_dry_run_then_real(tmp_path, s3, capsys):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>")
    assert main(["sync", "-n", str(site), "s3://test-bucket/www"]) == 0
    assert "+ index.html" in capsys.readouterr().out
    assert not s3.exists("/www/index.html")

    assert main(["sync", str(site), "s3://test-bucket/www"]) == 0
    assert s3.open_read("/www/index.html").read() == b"<h1>"


def test_errors_are_friendly(capsys):
    assert main(["ls", "nosuchprofile:/x"]) == 1
    assert "nosuchprofile" in capsys.readouterr().err
