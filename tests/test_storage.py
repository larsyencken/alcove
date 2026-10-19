import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from alcove import Alcove, init_alcove, plan_and_run, snapshot_to_alcove, storage
from alcove.storage import GIT_REF, GitStore, StorageConfig, StorageError
from alcove.utils import checksum_file

S3_VARS = ("S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_ENDPOINT_URL", "S3_BUCKET_NAME")


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """An empty project directory with no S3 credentials, and a home directory
    of its own, so we can tell that nothing lands in the alcove cache."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in (*S3_VARS, "TEST_ENVIRONMENT", storage.LOCAL_STORE_ENV):
        monkeypatch.delenv(name, raising=False)

    # git must not read the developer's own config
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")

    project = tmp_path / "project"
    project.mkdir()
    # an earlier test's fixture may have deleted the directory we're in, which
    # monkeypatch.chdir needs to note down to return to
    os.chdir(tmp_path)
    monkeypatch.chdir(project)

    yield project

    # alcove never fell back to its S3 cache
    assert not (home / ".cache" / "alcove").exists()


def git(*args, cwd="."):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_inputs(tmp_path: Path) -> tuple[Path, Path]:
    single = tmp_path / "single.txt"
    single.write_text("shared")

    folder = tmp_path / "folder"
    folder.mkdir()
    for i in range(4):
        (folder / f"part{i}.txt").write_text(f"part {i}")
    (folder / "same.txt").write_text("shared")
    return single, folder


SINGLE = Path("data/snapshots/example/single/2024-07-26.txt")
FOLDER = Path("data/snapshots/example/folder/2024-07-26")


def snapshot_and_delete(tmp_path: Path) -> Alcove:
    "Snapshot a file and a folder, then delete the working copies."
    single, folder = make_inputs(tmp_path)
    snapshot_to_alcove(single, "example/single/2024-07-26")
    snapshot_to_alcove(folder, "example/folder/2024-07-26")

    SINGLE.unlink()
    shutil.rmtree(FOLDER)
    return Alcove()


def assert_restored() -> None:
    assert SINGLE.read_text() == "shared"
    for i in range(4):
        assert (FOLDER / f"part{i}.txt").read_text() == f"part {i}"
    assert (FOLDER / "same.txt").read_text() == "shared"


def config() -> dict:
    return yaml.safe_load(Path("alcove.yaml").read_text())


# --- local -------------------------------------------------------------------


def test_local_store_round_trips_without_s3(offline, tmp_path):
    init_alcove(StorageConfig("local"))
    assert config()["storage"] == "local"
    assert "/.alcove/" in Path(".gitignore").read_text().split()

    alcove = snapshot_and_delete(tmp_path)

    # 5 distinct files; "shared" is stored once
    blobs = [p for p in (offline / ".alcove" / "blobs").rglob("*") if p.is_file()]
    assert len(blobs) == 5
    checksum = checksum_file(tmp_path / "single.txt")
    assert (
        offline / ".alcove/blobs" / checksum[:2] / checksum[2:4] / checksum
    ).exists()

    plan_and_run(alcove, jobs=4)
    assert_restored()


def test_local_store_keeps_its_setting_through_a_snapshot(offline, tmp_path):
    "alcove.yaml is rewritten by each snapshot; storage must survive that."
    init_alcove(StorageConfig("local", path="blobs"))
    snapshot_and_delete(tmp_path)

    assert config()["storage"] == {"backend": "local", "path": "blobs"}
    assert config()["data_dir"] == "data"
    assert (offline / "blobs").is_dir()


def test_local_store_path_can_be_overridden_per_machine(offline, tmp_path, monkeypatch):
    elsewhere = tmp_path / "volume"
    monkeypatch.setenv(storage.LOCAL_STORE_ENV, str(elsewhere))

    init_alcove(StorageConfig("local"))
    alcove = snapshot_and_delete(tmp_path)

    assert not (offline / ".alcove").exists()
    assert len([p for p in elsewhere.rglob("*") if p.is_file()]) == 5

    plan_and_run(alcove)
    assert_restored()


def test_local_store_reports_a_missing_blob(offline, tmp_path):
    init_alcove(StorageConfig("local"))
    alcove = snapshot_and_delete(tmp_path)
    shutil.rmtree(offline / ".alcove")

    with pytest.raises(StorageError, match="not in the local store"):
        plan_and_run(alcove)


# --- git ---------------------------------------------------------------------


def test_git_store_round_trips_without_s3(offline, tmp_path):
    git("init", "-q")
    init_alcove(StorageConfig("git"))
    assert config()["storage"] == "git"

    alcove = snapshot_and_delete(tmp_path)

    listed = git("ls-tree", "-r", "--name-only", GIT_REF).split()
    assert len(listed) == 5
    checksum = checksum_file(tmp_path / "single.txt")
    assert f"{checksum[:2]}/{checksum[2:4]}/{checksum}" in listed

    plan_and_run(alcove, jobs=4)
    assert_restored()


def test_git_store_survives_gc_and_leaves_branches_alone(offline, tmp_path):
    git("init", "-q", "-b", "main")
    init_alcove(StorageConfig("git"))
    alcove = snapshot_and_delete(tmp_path)

    # no branch was created or moved, and nothing was staged
    assert git("for-each-ref", "--format=%(refname)") == GIT_REF
    assert git("status", "--porcelain", "--untracked-files=no") == ""

    git("gc", "-q", "--prune=now")
    plan_and_run(alcove)
    assert_restored()


def test_git_store_adds_to_the_existing_tree(offline, tmp_path):
    git("init", "-q")
    init_alcove(StorageConfig("git"))
    single, folder = make_inputs(tmp_path)

    snapshot_to_alcove(single, "example/single/2024-07-26")
    first = git("rev-parse", GIT_REF)
    snapshot_to_alcove(folder, "example/folder/2024-07-26")

    # a second commit on top of the first, holding both snapshots' blobs
    assert git("rev-parse", f"{GIT_REF}^") == first
    assert len(git("ls-tree", "-r", "--name-only", GIT_REF).split()) == 5

    # snapshotting the same data again adds no commit
    head = git("rev-parse", GIT_REF)
    snapshot_to_alcove(single, "example/single/2024-07-26", force=True)
    assert git("rev-parse", GIT_REF) == head


def test_git_store_travels_with_an_explicit_fetch(offline, tmp_path):
    git("init", "-q", "-b", "main")
    init_alcove(StorageConfig("git"))
    snapshot_and_delete(tmp_path)
    git("add", "-A")
    git("commit", "-q", "-m", "analysis")

    clone = tmp_path / "clone"
    git("clone", "-q", f"file://{offline}", str(clone))
    os.chdir(clone)

    # a plain clone doesn't bring the data along...
    with pytest.raises(StorageError, match="git fetch origin"):
        plan_and_run(Alcove())

    # ...until it's asked for
    git("fetch", "-q", "origin", "refs/alcove/*:refs/alcove/*")
    plan_and_run(Alcove())
    assert_restored()


def test_git_store_works_from_a_subdirectory_alcove(offline, tmp_path):
    git("init", "-q")
    (offline / "data_project").mkdir()
    os.chdir(offline / "data_project")

    init_alcove(StorageConfig("git"))
    alcove = snapshot_and_delete(tmp_path)
    plan_and_run(alcove)
    assert_restored()


def test_git_store_retries_when_another_process_moves_the_ref(
    offline, tmp_path, monkeypatch
):
    git("init", "-q")
    init_alcove(StorageConfig("git"))
    single, folder = make_inputs(tmp_path)
    snapshot_to_alcove(single, "example/single/2024-07-26")

    # another process adds a blob between our reading the ref and moving it
    other = tmp_path / "other.txt"
    other.write_text("from another process")
    other_checksum = checksum_file(other)
    store = GitStore()
    original_head = store._head
    calls = []

    def head_then_race():
        head = original_head()
        if not calls:
            calls.append(1)
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from pathlib import Path; "
                    "from alcove.storage import GitStore; "
                    "GitStore().put([(Path(sys.argv[1]), sys.argv[2])])",
                    str(other),
                    other_checksum,
                ],
                check=True,
            )
        return head

    monkeypatch.setattr(store, "_head", head_then_race)
    store.put([(folder / "part0.txt", checksum_file(folder / "part0.txt"))])

    # both writers' blobs are in the tree; neither overwrote the other
    listed = git("ls-tree", "-r", "--name-only", GIT_REF)
    assert other_checksum in listed
    assert checksum_file(folder / "part0.txt") in listed
    assert checksum_file(single) in listed


def test_git_store_commits_without_a_configured_identity(
    offline, tmp_path, monkeypatch
):
    git("init", "-q")
    git("config", "user.useConfigOnly", "true")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.delenv(f"GIT_{role}_NAME")
        monkeypatch.delenv(f"GIT_{role}_EMAIL")

    init_alcove(StorageConfig("git"))
    alcove = snapshot_and_delete(tmp_path)

    assert git("log", "-1", "--format=%an", GIT_REF) == "alcove"
    plan_and_run(alcove)
    assert_restored()


def test_git_storage_needs_a_git_repo(offline):
    with pytest.raises(StorageError, match="git init"):
        init_alcove(StorageConfig("git"))
    assert not Path("alcove.yaml").exists()


# --- config ------------------------------------------------------------------


def test_a_missing_storage_key_means_s3(offline):
    init_alcove()
    assert "storage" not in config()
    assert Alcove().storage == StorageConfig("s3")

    with pytest.raises(ValueError, match="Missing S3 credentials"):
        snapshot_to_alcove(Path("alcove.yaml"), "example/single/2024-07-26")


def test_init_refuses_to_change_storage(offline):
    init_alcove(StorageConfig("local"))
    with pytest.raises(ValueError, match="already keeps its data in local"):
        init_alcove(StorageConfig("s3"))


def test_git_store_keeps_bytes_exactly_despite_attributes(offline, tmp_path):
    "Windows logs keep their CRLFs, whatever the repo's attributes say."
    git("init", "-q")
    git("config", "core.autocrlf", "true")
    Path(".gitattributes").write_text("* text=auto eol=lf\n*.log filter=shout\n")
    git("config", "filter.shout.clean", "tr a-z A-Z")
    init_alcove(StorageConfig("git"))

    log = tmp_path / "events.log"
    log.write_bytes(b"logon failed\r\nlogon ok\r\n")
    snapshot_to_alcove(log, "example/events/2024-07-26")
    restored = Path("data/snapshots/example/events/2024-07-26.log")
    restored.unlink()

    plan_and_run(Alcove())
    assert restored.read_bytes() == b"logon failed\r\nlogon ok\r\n"


def test_an_unknown_backend_is_rejected():
    with pytest.raises(StorageError, match="unknown storage backend 'gti'"):
        StorageConfig.parse("gti")


def test_a_folder_left_with_only_empty_subfolders_is_fetched_again(offline, tmp_path):
    "A failed fetch can leave subfolders behind but no files."
    init_alcove(StorageConfig("local"))
    folder = tmp_path / "folder"
    (folder / "nested").mkdir(parents=True)
    (folder / "nested" / "a.txt").write_text("a")
    snapshot_to_alcove(folder, "example/nested/2024-07-26")

    restored = Path("data/snapshots/example/nested/2024-07-26")
    (restored / "nested" / "a.txt").unlink()
    assert (restored / "nested").is_dir()

    plan_and_run(Alcove())
    assert (restored / "nested" / "a.txt").read_text() == "a"


def test_git_store_takes_a_file_name_with_a_newline(offline, tmp_path):
    git("init", "-q")
    init_alcove(StorageConfig("git"))
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "x\ny.txt").write_text("odd name")
    (folder / "x").write_text("decoy")
    snapshot_to_alcove(folder, "example/odd/2024-07-26")

    restored = Path("data/snapshots/example/odd/2024-07-26")
    shutil.rmtree(restored)
    plan_and_run(Alcove())
    assert (restored / "x\ny.txt").read_text() == "odd name"
    assert (restored / "x").read_text() == "decoy"


def test_a_storage_path_only_applies_to_local():
    with pytest.raises(StorageError, match="only applies to local"):
        StorageConfig.parse({"backend": "git", "path": "blobs"})


def test_the_cli_reads_env_from_the_alcove_not_from_where_it_is_installed(
    offline, tmp_path
):
    "Where the local store goes must not depend on how alcove was installed."
    elsewhere = tmp_path / "volume"
    Path(".env").write_text(f"{storage.LOCAL_STORE_ENV}={elsewhere}\n")
    single, _ = make_inputs(tmp_path)

    # the installed script, run from the alcove, as a user would
    alcove = Path(sys.executable).parent / "alcove"
    subprocess.run([alcove, "init", "--storage", "local"], check=True)
    subprocess.run(
        [alcove, "snapshot", single, "example/single/2024-07-26"], check=True
    )

    assert not Path(".alcove").exists()
    assert len([p for p in elsewhere.rglob("*") if p.is_file()]) == 1


def test_init_explains_why_it_cannot_continue(offline):
    alcove = Path(sys.executable).parent / "alcove"
    result = subprocess.run(
        [alcove, "init", "--storage", "git"], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert (
        result.stderr
        == "alcove: error: git storage needs a git repo; run `git init` first\n"
    )


def test_fetch_prunes_stray_files_in_subfolders(offline, tmp_path):
    init_alcove(StorageConfig("local"))
    folder = tmp_path / "folder"
    (folder / "nested").mkdir(parents=True)
    (folder / "nested" / "a.txt").write_text("a")
    snapshot_to_alcove(folder, "example/nested/2024-07-26")

    restored = Path("data/snapshots/example/nested/2024-07-26")
    (restored / "nested" / "a.txt").write_text("changed")
    (restored / "nested" / "stray.txt").write_text("stray")
    (restored / "newdir" / "deeper").mkdir(parents=True)
    (restored / "newdir" / "deeper" / "s.txt").write_text("stray")

    plan_and_run(Alcove())
    assert (restored / "nested" / "a.txt").read_text() == "a"
    assert not (restored / "nested" / "stray.txt").exists()
    assert not (restored / "newdir" / "deeper" / "s.txt").exists()


def test_save_writes_a_changed_storage_setting(offline):
    init_alcove()
    alcove = Alcove()
    alcove.storage = StorageConfig("local", path="blobs")
    alcove.save()
    assert config()["storage"] == {"backend": "local", "path": "blobs"}

    alcove = Alcove()
    alcove.storage = StorageConfig()
    alcove.save()
    assert config()["storage"] == "s3"
