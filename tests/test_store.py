import pytest

from edaport.snapshot.store import Store, StoreError


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s")


def tree(tmp_path, files):
    d = tmp_path / "d"
    for k, v in files.items():
        (d / k).parent.mkdir(parents=True, exist_ok=True)
        (d / k).write_text(v)
    return d


def test_content_addressed_and_deduped(store, tmp_path):
    d = tree(tmp_path, {"rtl/a.v": "module a; endmodule", "rtl/b.v": "module a; endmodule"})
    r1, r2 = store.import_dir(d), store.import_dir(d)
    assert r1 == r2 and r1.startswith("sha256:")
    m = store.manifest(r1)
    assert m["files"]["rtl/a.v"] == m["files"]["rtl/b.v"]  # same bytes, one blob


def test_write_branch_restore_diff(store, tmp_path):
    base = store.import_dir(tree(tmp_path, {"rtl/a.v": "x"}))
    store.branch(base, "main")
    new = store.write_file("main", "rtl/a.v", b"y")
    assert new != base and store.read_file(base, "rtl/a.v") == b"x"  # old ref untouched
    assert store.branch(new, "main") == base  # previous head returned -> undo possible
    assert store.resolve("main") == new
    assert store.diff(base, new)["files_changed"]["modified"] == ["rtl/a.v"]
    gone = store.write_file(new, "rtl/a.v", None)
    assert store.diff(new, gone)["files_changed"]["removed"] == ["rtl/a.v"]


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../b", "", "a\x00b"])
def test_path_traversal_rejected(store, tmp_path, bad):
    base = store.import_dir(tree(tmp_path, {"a": "1"}))
    with pytest.raises(StoreError):
        store.write_file(base, bad, b"x")


@pytest.mark.parametrize("bad", ["../x", "a/../b", "-x", ""])
def test_bad_labels(store, tmp_path, bad):
    base = store.import_dir(tree(tmp_path, {"a": "1"}))
    with pytest.raises(StoreError):
        store.branch(base, bad)


def test_capture_write_scope_and_metric_deltas(store, tmp_path):
    base = store.import_dir(tree(tmp_path, {"rtl/a.v": "x", "build/old.txt": "stale"}))
    wd = tmp_path / "wd"
    store.materialize(base, wd)
    (wd / "build/new.txt").write_text("n")
    (wd / "build/old.txt").unlink()
    (wd / "rtl/a.v").write_text("tampered")  # outside the write scope: must not be captured
    out = store.capture(base, wd, ["build"], {"metrics": {"synth": {"area": 10}}})
    files = store.manifest(out)["files"]
    assert set(files) == {"rtl/a.v", "build/new.txt"}
    assert store.read_file(out, "rtl/a.v") == b"x"
    out2 = store.capture(out, wd, ["build"], {"metrics": {"synth": {"area": 12}}})
    assert store.diff(out, out2)["metric_deltas"] == {"synth.area": {"a": 10, "b": 12, "delta": 2}}


def test_gc_keeps_reachable(store, tmp_path):
    keep = store.import_dir(tree(tmp_path, {"a": "1"}))
    store.branch(keep, "main")
    drop = store.write_file(keep, "b", b"garbage")
    removed = store.gc(set())
    assert removed == 2  # dropped manifest + its unique blob
    assert store.has(keep) and not store.has(drop)
    assert store.read_file("main", "a") == b"1"
