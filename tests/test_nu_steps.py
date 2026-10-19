"""A step may be a nushell script, `<path>.nu`, called with the same arguments
as a Python step: each dependency's path, then the output path."""

import os
import shutil
import subprocess
from pathlib import Path

import polars as pl
import pytest
from alcove import plan_and_run, steps
from alcove.artifacts import artifact_path
from alcove.core import Alcove
from alcove.paths import ARTIFACT_SCRIPT_DIR, TABLE_DIR, TABLE_SCRIPT_DIR
from alcove.table_metadata import _get_executable, _metadata_path
from alcove.types import StepURI
from alcove.utils import load_yaml

# CI installs nushell, so these tests only skip locally
needs_nu = pytest.mark.skipif(
    shutil.which("nu") is None and os.environ.get("GITHUB_ACTIONS") != "true",
    reason="needs nushell",
)

WRITE_TABLE = """#!/usr/bin/env python3
import sys
import polars as pl

pl.DataFrame({"dim_key": ["a", "b"], "value": [1, 2]}).write_parquet(sys.argv[-1])
"""

# Plain nushell can't write Parquet without the polars plugin, so this table
# step copies its table dependency
COPY_TABLE = """#!/usr/bin/env nu
def main [...args: path] {
    let dest = $args | last
    let deps = $args | drop 1
    cp ($deps | first) $dest
}
"""

# records each path alcove handed it, so the test can check them
WRITE_INDEX = """#!/usr/bin/env nu
def main [...args: path] {
    let dest = $args | last
    let deps = $args | drop 1
    $deps | enumerate | each {|d| $d.item | save ($dest | path join $"dep($d.index).txt") }
    "<h1>hello</h1>" | save ($dest | path join "index.html")
}
"""

FAIL = """#!/usr/bin/env nu
def main [...args: path] {
    error make {msg: "boom"}
}
"""


def write_script(script_dir: Path, uri: StepURI, body: str, suffix: str) -> Path:
    path = (script_dir / uri.path).with_suffix(suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def outstanding(alcove: Alcove) -> set[StepURI]:
    alcove.refresh()
    return set(steps.prune_completed(dict(alcove.steps)))


@needs_nu
def test_nu_table_step(setup_test_environment):
    source = StepURI.parse("table://source/latest")
    copy = StepURI.parse("table://copy/latest")
    write_script(TABLE_SCRIPT_DIR, source, WRITE_TABLE, ".py")
    script = write_script(TABLE_SCRIPT_DIR, copy, COPY_TABLE, ".nu")

    alcove = Alcove.init()
    alcove.new_table(source.path, [])
    alcove.new_table(copy.path, [str(source)])
    plan_and_run(alcove)

    df = pl.read_parquet(TABLE_DIR / "copy/latest.parquet")
    assert df["dim_key"].to_list() == ["a", "b"]

    manifest = load_yaml(_metadata_path(copy))["input_manifest"]
    assert str(script) in manifest
    assert outstanding(alcove) == set()

    # editing the script makes the step stale
    script.write_text(COPY_TABLE + "\n# changed\n")
    assert outstanding(alcove) == {copy}


@needs_nu
def test_nu_artifact_step(setup_test_environment):
    source = StepURI.parse("table://source/latest")
    report = StepURI.parse("artifact://report/latest")
    write_script(TABLE_SCRIPT_DIR, source, WRITE_TABLE, ".py")
    script = write_script(ARTIFACT_SCRIPT_DIR, report, WRITE_INDEX, ".nu")

    alcove = Alcove.init()
    alcove.new_table(source.path, [])
    alcove.new_artifact(report.path, [str(source)])
    plan_and_run(alcove)

    out = artifact_path(report)
    assert (out / "index.html").read_text() == "<h1>hello</h1>"
    assert (out / "dep0.txt").read_text() == str(
        (TABLE_DIR / "source/latest.parquet").resolve()
    )

    metadata = load_yaml(_metadata_path(report))
    assert set(metadata["manifest"]) == {"index.html", "dep0.txt"}
    assert str(script) in metadata["input_manifest"]
    assert outstanding(alcove) == set()


@needs_nu
def test_failing_nu_step_fails_the_build(setup_test_environment):
    uri = StepURI.parse("artifact://report/latest")
    write_script(ARTIFACT_SCRIPT_DIR, uri, FAIL, ".nu")

    alcove = Alcove.init()
    alcove.new_artifact(uri.path, [])
    with pytest.raises(subprocess.CalledProcessError):
        plan_and_run(alcove)

    assert not artifact_path(uri).exists()


def test_nu_step_without_nushell(setup_test_environment, monkeypatch):
    uri = StepURI.parse("artifact://report/latest")
    write_script(ARTIFACT_SCRIPT_DIR, uri, WRITE_INDEX, ".nu")
    monkeypatch.setattr(shutil, "which", lambda _: None)

    alcove = Alcove.init()
    alcove.new_artifact(uri.path, [])
    with pytest.raises(FileNotFoundError, match="needs nushell"):
        plan_and_run(alcove)


def test_python_script_takes_precedence_over_nu(setup_test_environment):
    uri = StepURI.parse("table://both/latest")
    py = write_script(TABLE_SCRIPT_DIR, uri, WRITE_TABLE, ".py")
    write_script(TABLE_SCRIPT_DIR, uri, COPY_TABLE, ".nu")

    assert _get_executable(uri) == py


def test_nu_script_shared_across_versions(setup_test_environment):
    uri = StepURI.parse("artifact://report/2024-01-01")
    script = (ARTIFACT_SCRIPT_DIR / "report").with_suffix(".nu")
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(WRITE_INDEX)

    assert _get_executable(uri) == script


def test_sql_script_takes_precedence_over_nu(setup_test_environment):
    uri = StepURI.parse("table://both/latest")
    sql = (TABLE_SCRIPT_DIR / uri.path).with_suffix(".sql")
    sql.parent.mkdir(parents=True, exist_ok=True)
    sql.write_text("SELECT 1 AS dim_key")
    write_script(TABLE_SCRIPT_DIR, uri, COPY_TABLE, ".nu")

    assert _get_executable(uri) == sql
