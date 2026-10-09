import os
import shutil
import threading
import time

import pytest
from alcove import Alcove, plan_and_run, snapshot_to_alcove, snapshots
from alcove.utils import checksum_file


@pytest.fixture
def empty_cache(setup_test_environment, tmp_path, monkeypatch):
    "A home directory of the test's own, so the alcove cache starts empty."
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home / ".cache" / "alcove"


def test_run_downloads_several_files_at_once(
    setup_test_environment, empty_cache, tmp_path, monkeypatch
):
    test_dir = setup_test_environment

    # upload to MinIO for real, rather than only into the cache, so the run
    # has to download everything
    monkeypatch.delenv("TEST_ENVIRONMENT")

    folder = tmp_path / "folder"
    folder.mkdir()
    for i in range(6):
        (folder / f"part{i}.txt").write_text(f"part {i}")
    (folder / "same_a.txt").write_text("shared")
    (folder / "same_b.txt").write_text("shared")
    single = tmp_path / "single.txt"
    single.write_text("shared")

    alcove = Alcove.init()
    snapshot_to_alcove(folder, "example/folder/2024-07-26")
    snapshot_to_alcove(single, "example/single/2024-07-26")

    data_dir = test_dir / "data" / "snapshots" / "example"
    shutil.rmtree(data_dir / "folder" / "2024-07-26")
    (data_dir / "single" / "2024-07-26.txt").unlink()
    assert not empty_cache.exists()

    # the first three downloads only get past the barrier together, so the
    # run fails unless three are in flight at once
    barrier = threading.Barrier(3, timeout=10)
    lock = threading.Lock()
    downloads = []
    download_file = snapshots.download_file

    def download_together(s3_path, dest_path, s3=None, callback=None):
        with lock:
            downloads.append(s3_path)
            n = len(downloads)
        if n <= 3:
            barrier.wait()
        download_file(s3_path, dest_path, s3, callback)

    monkeypatch.setattr(snapshots, "download_file", download_together)

    plan_and_run(alcove, jobs=3)

    for i in range(6):
        part = data_dir / "folder" / "2024-07-26" / f"part{i}.txt"
        assert part.read_text() == f"part {i}"
    assert (data_dir / "folder" / "2024-07-26" / "same_a.txt").read_text() == "shared"
    assert (data_dir / "folder" / "2024-07-26" / "same_b.txt").read_text() == "shared"
    assert (data_dir / "single" / "2024-07-26.txt").read_text() == "shared"

    # "shared" is needed three times but downloaded once
    assert len(downloads) == 7
    assert len(set(downloads)) == 7

    cached = [p for p in empty_cache.rglob("*") if p.is_file()]
    assert len(cached) == 7
    assert not [p for p in cached if p.name.endswith(".partial")]


def _snapshot_six_files(test_dir, tmp_path):
    "Snapshot a folder of six distinct files; it goes only into the cache."
    folder = tmp_path / "folder"
    folder.mkdir()
    for i in range(6):
        (folder / f"part{i}.txt").write_text(f"part {i}")

    alcove = Alcove.init()
    snapshot_to_alcove(folder, "example/folder/2024-07-26")

    data_dir = test_dir / "data" / "snapshots" / "example" / "folder" / "2024-07-26"
    shutil.rmtree(data_dir)
    return alcove, data_dir


def test_a_failed_download_stops_the_others(
    setup_test_environment, empty_cache, tmp_path, monkeypatch
):
    alcove, _ = _snapshot_six_files(setup_test_environment, tmp_path)

    # with the cache gone, every file has to be downloaded
    shutil.rmtree(empty_cache)

    lock = threading.Lock()
    started = []
    second_started = threading.Event()
    abandoned = threading.Event()

    def download(s3_path, dest_path, s3=None, callback=None):
        with lock:
            started.append(s3_path)
            n = len(started)
        if n == 1:
            second_started.wait(10)
            raise RuntimeError("no network")

        # the others report progress for up to 10s, until told to stop
        assert callback is not None
        second_started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                callback(1024)
            except snapshots.FetchStopped:
                abandoned.set()
                raise
            time.sleep(0.01)

    monkeypatch.setattr(snapshots, "download_file", download)

    with pytest.raises(RuntimeError, match="no network"):
        plan_and_run(alcove, jobs=2)

    # the download in progress was abandoned mid-file, and of the four
    # still queued, at most the one the failed worker picked up next started
    assert abandoned.is_set()
    assert len(started) <= 3


def test_a_stopped_fetch_abandons_a_real_download(
    setup_test_environment, empty_cache, tmp_path, monkeypatch
):
    # upload to MinIO for real, so the download goes through s3transfer
    monkeypatch.delenv("TEST_ENVIRONMENT")

    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(2 * 1024 * 1024))
    checksum = checksum_file(big)
    snapshots.add_to_s3(big, checksum)

    session = snapshots.FetchSession()
    session.stopped.set()
    dest_dir = setup_test_environment / "fetched"

    with pytest.raises(snapshots.FetchStopped):
        snapshots.fetch_from_s3(checksum, dest_dir / "big.bin", session)

    # nothing half-downloaded is left behind, here or in the cache
    assert list(dest_dir.iterdir()) == []
    assert not empty_cache.exists()


def test_a_fully_cached_run_needs_no_credentials(
    setup_test_environment, empty_cache, tmp_path, monkeypatch
):
    alcove, data_dir = _snapshot_six_files(setup_test_environment, tmp_path)

    for name in ("S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_ENDPOINT_URL", "S3_BUCKET_NAME"):
        monkeypatch.delenv(name)

    plan_and_run(alcove, jobs=4)

    for i in range(6):
        assert (data_dir / f"part{i}.txt").read_text() == f"part {i}"


def test_fetch_writes_through_a_link_to_another_file_of_the_snapshot(
    setup_test_environment, empty_cache
):
    test_dir = setup_test_environment

    # snapshotted where it sits, so the link is kept, and both names are in
    # the manifest with the same checksum
    data_dir = test_dir / "data" / "snapshots" / "example" / "linked" / "2024-07-26"
    data_dir.mkdir(parents=True)
    (data_dir / "a.txt").write_text("hello")
    (data_dir / "latest.txt").symlink_to("a.txt")

    alcove = Alcove.init()
    snapshot_to_alcove(data_dir, "example/linked/2024-07-26")

    # both destinations are the same file, which must not stop the fetch
    plan_and_run(alcove, force=True, jobs=2)

    assert (data_dir / "a.txt").read_text() == "hello"
    assert (data_dir / "latest.txt").read_text() == "hello"
