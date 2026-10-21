import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from alcove import (
    CaptureFailed,
    capture_to_alcove,
    init_alcove,
    main,
    snapshot_to_alcove,
)
from alcove.core import Alcove
from alcove.provenance import REDACTED, STDERR_TAIL_CHARS, redact, redact_text
from alcove.snapshots import Snapshot
from alcove.storage import StorageConfig
from alcove.types import StepURI
from alcove.utils import load_yaml, save_yaml


@pytest.fixture
def project(offline):
    init_alcove(StorageConfig("local"))
    return offline


def produced_by(uri: StepURI) -> dict:
    return Snapshot.load(uri.path).produced_by or {}


def run_cli(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["alcove", *args])
    try:
        main()
    except SystemExit as e:
        return int(e.code or 0)
    return 0


# --- alcove capture ----------------------------------------------------------


def test_capture_snapshots_stdout_and_records_how(project):
    snapshot = capture_to_alcove(
        "node01/kubelet",
        ["sh", "-c", "echo hello; echo careful >&2"],
        note="pulled through the kubelet proxy",
    )

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{4}", snapshot.uri.version)
    assert snapshot.path.read_text() == "hello\n"
    assert snapshot.extension == ".txt"

    record = produced_by(snapshot.uri)
    assert record["command"] == ["sh", "-c", "echo hello; echo careful >&2"]
    assert record["exit_code"] == 0
    assert record["stderr"] == "careful"
    assert record["note"] == "pulled through the kubelet proxy"
    assert record["started_at"] <= record["finished_at"]
    for key in ("snapshotted_at", "host", "alcove_version"):
        assert record[key]


def test_capture_takes_a_version_and_extension(project):
    snapshot = capture_to_alcove(
        "api/thing", ["echo", "{}"], version="2026-10-07-1443", extension=".json"
    )
    assert str(snapshot.uri) == "snapshot://api/thing/2026-10-07-1443"
    assert snapshot.path.name == "2026-10-07-1443.json"


def test_capture_dir_snapshots_the_files_written(project):
    snapshot = capture_to_alcove(
        "node01/sysstat",
        ["sh", "-c", "echo a > a.txt; mkdir sub; echo b > sub/b.txt"],
        directory=True,
    )
    assert snapshot.snapshot_type == "directory"
    assert set(snapshot.manifest or {}) == {"a.txt", "sub/b.txt"}


def test_capture_dir_refuses_a_command_that_wrote_nothing(project):
    with pytest.raises(ValueError, match="wrote no files"):
        capture_to_alcove("node01/empty", ["true"], directory=True)
    assert not Alcove().steps


def test_a_failed_capture_snapshots_nothing_by_default(project):
    with pytest.raises(CaptureFailed) as e:
        capture_to_alcove("node01/fail", ["sh", "-c", "echo partial; exit 3"])
    assert e.value.returncode == 3
    assert e.value.snapshot is None
    assert not Alcove().steps


def test_a_failed_capture_can_be_kept(project):
    with pytest.raises(CaptureFailed) as e:
        capture_to_alcove(
            "node01/fail", ["sh", "-c", "echo partial; exit 3"], keep_failed=True
        )
    snapshot = e.value.snapshot
    assert snapshot is not None
    assert snapshot.path.read_text() == "partial\n"
    assert produced_by(snapshot.uri)["exit_code"] == 3


def test_capture_checks_the_name_before_running_anything(project):
    marker = project / "ran"
    command = ["touch", str(marker)]

    with pytest.raises(ValueError, match="invalid step name"):
        capture_to_alcove("bad-name/kubelet", command)
    assert not marker.exists()

    capture_to_alcove("node01/kubelet", ["echo"], version="2026-10-07-1443")
    with pytest.raises(ValueError, match="already exists"):
        capture_to_alcove("node01/kubelet", command, version="2026-10-07-1443")
    assert not marker.exists()

    # a version alcove would extend with today's date, making another dataset
    with pytest.raises(ValueError, match="invalid version 'v2'"):
        capture_to_alcove("node01/kubelet", command, version="v2")
    assert not marker.exists()

    # a version with no dataset name
    with pytest.raises(ValueError, match="invalid dataset name"):
        capture_to_alcove("2026-10-07", command)
    assert not marker.exists()


def test_capture_into_a_partitioned_dataset_uses_the_date(project):
    alcove = Alcove()
    alcove.steps[StepURI.parse("snapshot://gpu/usage/*")] = []
    alcove.save()

    snapshot = capture_to_alcove("gpu/usage", ["echo", "1"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", snapshot.uri.version)


def test_capture_into_a_partitioned_dataset_refuses_a_time_first(project):
    alcove = Alcove()
    alcove.steps[StepURI.parse("snapshot://gpu/usage/*")] = []
    alcove.save()
    marker = project / "ran"

    with pytest.raises(ValueError, match="must be an ISO date"):
        capture_to_alcove(
            "gpu/usage", ["touch", str(marker)], version="2026-10-07-1443"
        )
    assert not marker.exists()


def test_a_failed_capture_into_an_empty_dir_exits_as_the_command_did(project):
    with pytest.raises(CaptureFailed) as e:
        capture_to_alcove("a/b", ["false"], directory=True, keep_failed=True)
    assert e.value.returncode == 1
    assert e.value.snapshot is None


def test_capture_redacts_its_stderr(project, monkeypatch):
    monkeypatch.setenv("HOME", "/Users/someone")
    snapshot = capture_to_alcove(
        "api/thing",
        [
            "sh",
            "-c",
            "echo '> Authorization: Bearer abc' >&2; "
            "echo 'File \"/Users/someone/x.py\", line 1' >&2",
        ],
    )
    stderr = produced_by(snapshot.uri)["stderr"]
    assert "abc" not in stderr
    assert "/Users/someone" not in stderr
    assert '"~/x.py"' in stderr


def test_extra_produced_by_values_must_be_plain_data(project):
    Path("x.txt").write_text("x")
    with pytest.raises(TypeError, match="plain data"):
        snapshot_to_alcove(Path("x.txt"), "a/b/latest", produced_by={"p": Path(".")})
    assert not Alcove().steps

    # a tuple is saved as a list, which reloads, not as a Python-only YAML tag
    snapshot = snapshot_to_alcove(
        Path("x.txt"), "a/b/latest", produced_by={"range": ("2026-10-01", "2026-10-07")}
    )
    assert produced_by(snapshot.uri)["range"] == ["2026-10-01", "2026-10-07"]


def test_capture_doesnt_wait_for_a_background_process_holding_stderr(project):
    started = time.monotonic()
    snapshot = capture_to_alcove(
        "a/b", ["sh", "-c", "sleep 30 >&2 & echo hi"], version="2026-10-07-1443"
    )
    assert time.monotonic() - started < 10
    assert snapshot.path.read_text() == "hi\n"


def test_redaction_is_fast_on_a_long_argument():
    started = time.monotonic()
    redact(["curl", "--data-binary", "QUJD" * 250_000])
    assert time.monotonic() - started < 2


def test_redact_text_leaves_an_email_in_json_alone():
    text = '{"url":"https://h","email":"a@b.com"}'
    assert redact_text(text) == text


def test_a_long_stderr_line_keeps_its_tail(project):
    snapshot = capture_to_alcove(
        "a/b", ["sh", "-c", "printf '%05000d' 7 >&2"], version="2026-10-07-1443"
    )
    stderr = produced_by(snapshot.uri)["stderr"]
    assert len(stderr) == STDERR_TAIL_CHARS
    assert stderr.endswith("7")


def test_progress_bar_stderr_keeps_its_tail(project):
    # 12000 bytes of \r-separated updates and no \n, as from tqdm or curl
    script = "for i in $(seq 1000 2000); do printf '%s%%  |#####|\\r' $i >&2; done"
    snapshot = capture_to_alcove("a/b", ["sh", "-c", script], version="2026-10-07-1443")
    stderr = produced_by(snapshot.uri)["stderr"]
    assert stderr
    assert "2000%" in stderr


def test_a_secret_where_stderr_is_cut_is_still_hidden(project):
    # the header straddles where the kept tail of stderr begins
    pad = "x" * (STDERR_TAIL_CHARS - 10)
    snapshot = capture_to_alcove(
        "api/thing",
        ["sh", "-c", f"echo '> Authorization: Bearer abc' >&2; printf '{pad}' >&2"],
    )
    assert "abc" not in produced_by(snapshot.uri)["stderr"]


def test_capture_redacts_secrets_in_the_command(project):
    snapshot = capture_to_alcove(
        "api/thing", ["echo", "--token", "abc", "--api-key=xyz", "ok"]
    )
    assert produced_by(snapshot.uri)["command"] == [
        "echo",
        "--token",
        REDACTED,
        f"--api-key={REDACTED}",
        "ok",
    ]


def test_capture_cli_exits_as_the_command_did(project, monkeypatch, capsys):
    assert run_cli(monkeypatch, "capture", "a/b", "--", "sh", "-c", "exit 3") == 3
    assert "nothing was snapshotted" in capsys.readouterr().err

    assert run_cli(monkeypatch, "capture", "a/b", "--", "no-such-command-xyz") == 127
    assert run_cli(monkeypatch, "capture", "a/b", "--", "sh", "-c", "kill -9 $$") == 137
    assert not Alcove().steps

    assert (
        run_cli(monkeypatch, "capture", "a/b", "--note", "n", "--", "echo", "-n", "x")
        == 0
    )
    [uri] = Alcove().steps
    assert produced_by(uri)["command"] == ["echo", "-n", "x"]
    assert produced_by(uri)["note"] == "n"


# --- produced_by on every snapshot --------------------------------------------


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


SCRIPT = """\
import sys, tempfile
from pathlib import Path
import alcove

with tempfile.TemporaryDirectory() as tmp:
    data = Path(tmp) / "thing.json"
    data.write_text("{}")
    alcove.snapshot_to_alcove(data, f"api/thing/{sys.argv[1]}", produced_by={"query": "a=1"})
"""


def test_a_snapshot_script_records_itself_and_its_commit(project):
    script = Path("src/steps/snapshots/thing.py")
    script.parent.mkdir(parents=True)
    script.write_text(SCRIPT)
    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "add script")
    commit = git("rev-parse", "HEAD")

    subprocess.run([sys.executable, str(script), "2026-10-01"], check=True)
    record = produced_by(StepURI.parse("snapshot://api/thing/2026-10-01"))
    assert record["script"] == "src/steps/snapshots/thing.py"
    assert record["command"] == ["src/steps/snapshots/thing.py", "2026-10-01"]
    assert record["git_commit"] == commit
    assert record["script_changed"] is False
    assert record["query"] == "a=1"

    script.write_text(SCRIPT + "# changed\n")
    subprocess.run([sys.executable, str(script), "2026-10-02"], check=True)
    record = produced_by(StepURI.parse("snapshot://api/thing/2026-10-02"))
    assert record["script_changed"] is True


def test_an_ignored_script_counts_as_changed(project):
    script = Path("scratch/thing.py")
    script.parent.mkdir()
    script.write_text(SCRIPT)
    Path(".gitignore").write_text("scratch/\n")
    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "init")

    subprocess.run([sys.executable, str(script), "2026-10-01"], check=True)
    record = produced_by(StepURI.parse("snapshot://api/thing/2026-10-01"))
    assert record["script_changed"] is True


def test_a_launcher_in_the_python_environment_is_not_the_script(project, monkeypatch):
    launcher = Path(sys.prefix) / "lib" / "site-packages" / "ipykernel_launcher.py"
    monkeypatch.setattr(sys, "argv", [str(launcher), "-f", "kernel.json"])
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    from alcove.provenance import describe_caller

    record = describe_caller()
    assert "script" not in record
    assert record["command"] == ["ipykernel_launcher.py", "-f", "kernel.json"]


def test_a_script_outside_git_records_no_commit(project):
    script = Path("thing.py")
    script.write_text(SCRIPT)
    subprocess.run([sys.executable, str(script), "2026-10-01"], check=True)
    record = produced_by(StepURI.parse("snapshot://api/thing/2026-10-01"))
    assert record["script"] == "thing.py"
    assert "git_commit" not in record


def test_alcove_snapshot_records_its_command(project, monkeypatch):
    Path("countries.csv").write_text("a\n1\n")
    monkeypatch.setattr(
        sys,
        "argv",
        ["/venv/bin/alcove", "snapshot", "countries.csv", "countries/latest"],
    )
    snapshot = snapshot_to_alcove(Path("countries.csv"), "countries/latest")
    record = produced_by(snapshot.uri)
    assert record["command"] == [
        "alcove",
        "snapshot",
        "countries.csv",
        "countries/latest",
    ]
    assert "script" not in record


def test_produced_by_is_not_carried_over(project):
    capture_to_alcove(
        "node01/kubelet", ["echo", "a"], version="2026-10-07-1443", note="first pull"
    )
    uri = StepURI.parse("snapshot://node01/kubelet/2026-10-07-1443")
    snapshot = Snapshot.load(uri.path)
    snapshot.access_notes = "needs the Prime kubeconfig"
    snapshot.save()

    Path("again.txt").write_text("b\n")
    snapshot_to_alcove(Path("again.txt"), uri.path, force=True)

    replaced = Snapshot.load(uri.path)
    # what describes the dataset carries over; how the old version was made doesn't
    assert replaced.access_notes == "needs the Prime kubeconfig"
    assert replaced.produced_by is not None
    assert "note" not in replaced.produced_by
    assert "exit_code" not in replaced.produced_by


def test_produced_by_takes_unquoted_timestamps_and_extra_keys(project):
    snapshot = capture_to_alcove("a/b", ["echo"], version="2026-10-07-1443")
    text = snapshot.metadata_path.read_text()
    text = re.sub(r"started_at: '([^']+)'", r"started_at: \1", text)
    text += "  kube_context: h100-prime-001\n"
    snapshot.metadata_path.write_text(text)
    assert "started_at: 20" in snapshot.metadata_path.read_text()

    record = produced_by(snapshot.uri)
    assert isinstance(record["started_at"], str)
    assert record["kube_context"] == "h100-prime-001"


def test_snapshots_without_produced_by_still_load(project):
    snapshot = capture_to_alcove("a/b", ["echo"], version="2026-10-07-1443")
    metadata = load_yaml(snapshot.metadata_path)
    del metadata["produced_by"]
    save_yaml(metadata, snapshot.metadata_path)
    assert Snapshot.load(snapshot.uri.path).produced_by is None


# --- redaction ----------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--token", "abc"], ["--token", REDACTED]),
        (["--api-key=abc"], [f"--api-key={REDACTED}"]),
        (["--db-password", "abc"], ["--db-password", REDACTED]),
        (["env", "GITHUB_TOKEN=abc", "gh"], ["env", f"GITHUB_TOKEN={REDACTED}", "gh"]),
        (
            ["curl", "-H", "Authorization: Bearer abc"],
            ["curl", "-H", f"Authorization: {REDACTED}"],
        ),
        (["--secrets", "abc"], ["--secrets", REDACTED]),
        (
            ["https://user:hunter2@example.com/x"],
            [f"https://{REDACTED}@example.com/x"],
        ),
        (
            ["https://ghp_abc@github.com/o/r.git"],
            [f"https://{REDACTED}@github.com/o/r.git"],
        ),
        (
            ["https://example.com/x?a=1&token=abc&b=2"],
            [f"https://example.com/x?a=1&token={REDACTED}&b=2"],
        ),
        (
            ["https://b.s3.amazonaws.com/k?X-Amz-Credential=c&X-Amz-Signature=s"],
            [
                f"https://b.s3.amazonaws.com/k?X-Amz-Credential={REDACTED}"
                f"&X-Amz-Signature={REDACTED}"
            ],
        ),
        (["-H", "X-Auth-Token: abc"], ["-H", f"X-Auth-Token: {REDACTED}"]),
        (["-H", "Private-Token: abc"], ["-H", f"Private-Token: {REDACTED}"]),
        # inside a shell script
        (
            ["sh", "-c", "mycli --token abc | jq ."],
            ["sh", "-c", f"mycli --token {REDACTED} | jq ."],
        ),
        (
            ["sh", "-c", "GITHUB_TOKEN=abc gh api /user"],
            ["sh", "-c", f"GITHUB_TOKEN={REDACTED} gh api /user"],
        ),
        (["https://h/x?auth=abc"], [f"https://h/x?auth={REDACTED}"]),
        (
            ["https://h/x?access_key=AKIA1&sort_key=d"],
            [f"https://h/x?access_key={REDACTED}&sort_key={REDACTED}"],
        ),
        # not secrets
        (["git", "log", "--author", "lars"], ["git", "log", "--author", "lars"]),
        (["kubectl", "--context", "prime"], ["kubectl", "--context", "prime"]),
        (
            ["docker", "login", "--password-stdin", "registry.example.com"],
            ["docker", "login", "--password-stdin", "registry.example.com"],
        ),
        (["curl", "--no-auth", "https://h/x"], ["curl", "--no-auth", "https://h/x"]),
        (["mycli", "--auth", "--verbose"], ["mycli", "--auth", "--verbose"]),
        # a script with nothing to hide is kept exactly as written
        (["sh", "-c", "echo 'a  b' | wc"], ["sh", "-c", "echo 'a  b' | wc"]),
        # a whole header or assignment, spaces and all
        (
            ["curl", "-H", "Cookie: session=abc; token=xyz"],
            ["curl", "-H", f"Cookie: {REDACTED}"],
        ),
        (
            ["env", "DB_PASSWORD=correct horse battery", "cmd"],
            ["env", f"DB_PASSWORD={REDACTED}", "cmd"],
        ),
        # inside a script, only the secret changes
        (
            ["sh", "-c", "cd /srv && GITHUB_TOKEN='a b' gh api > out.json; echo $X"],
            [
                "sh",
                "-c",
                f"cd /srv && GITHUB_TOKEN={REDACTED} gh api > out.json; echo $X",
            ],
        ),
        (
            ["sh", "-c", "curl -H 'Authorization: Bearer abc' https://h/x | jq ."],
            ["sh", "-c", f"curl -H 'Authorization: {REDACTED}' https://h/x | jq ."],
        ),
        # whole words only
        (
            ["https://api.github.com/search?q=x&author=lars&keyword=d&monkey=1"],
            ["https://api.github.com/search?q=x&author=lars&keyword=d&monkey=1"],
        ),
        (
            ["train.py", "max_tokens=512", "tokenizer=gpt2"],
            ["train.py", "max_tokens=512", "tokenizer=gpt2"],
        ),
        # an @ in the query is not credentials
        (
            ["https://api.example.com/u?email=me@x.com"],
            ["https://api.example.com/u?email=me@x.com"],
        ),
    ],
)
def test_redact(argv, expected):
    assert redact(argv) == expected


def test_redact_writes_the_home_directory_as_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/someone")
    assert redact(
        [
            "/Users/someone/Downloads/x.csv",
            "--out=/Users/someone/o",
            "/Users/someoneelse/y",
        ]
    ) == ["~/Downloads/x.csv", "--out=~/o", "/Users/someoneelse/y"]
