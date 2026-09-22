from pathlib import Path

import jsonschema
import polars as pl
import pytest
from alcove import plan_and_run, steps
from alcove.artifacts import artifact_path, build_artifact, is_completed
from alcove.core import Alcove
from alcove.db import _get_tables
from alcove.paths import ARTIFACT_DIR, ARTIFACT_SCRIPT_DIR, TABLE_DIR, TABLE_SCRIPT_DIR
from alcove.schemas import ALCOVE_SCHEMA
from alcove.table_metadata import _metadata_path
from alcove.types import StepURI
from alcove.utils import load_yaml

# A script that writes one file per dependency plus an index, so the test can
# see which paths alcove handed it.
WRITE_INDEX = """#!/usr/bin/env python3
import sys
from pathlib import Path

*deps, out = sys.argv[1:]
out = Path(out)
for i, dep in enumerate(deps):
    (out / f"dep{i}.txt").write_text(dep)
(out / "index.html").write_text("<h1>hello</h1>")
"""

WRITE_NOTHING = """#!/usr/bin/env python3
"""

WRITE_TABLE = """#!/usr/bin/env python3
import sys
import polars as pl

pl.DataFrame({"dim_key": ["a", "b"], "value": [1, 2]}).write_parquet(sys.argv[-1])
"""

# Reads the artifact directory it depends on and turns it into a table
TABLE_FROM_ARTIFACT = """#!/usr/bin/env python3
import sys
from pathlib import Path
import polars as pl

artifact_dir, out = Path(sys.argv[1]), sys.argv[2]
files = sorted(p.name for p in artifact_dir.iterdir())
pl.DataFrame({"dim_file": files}).write_parquet(out)
"""


def write_script(script_dir, uri: StepURI, body: str) -> Path:
    path = (script_dir / uri.path).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_step_uri_artifact():
    uri = StepURI.parse("artifact://reports/health/latest")
    assert uri.scheme == "artifact"
    assert uri.full_path == ARTIFACT_DIR / "reports/health/latest"
    assert str(uri) == "artifact://reports/health/latest"


def test_alcove_schema_accepts_artifact_steps():
    config = {
        "version": 1,
        "steps": {
            "snapshot://raw/2024-09-04": [],
            "table://clean/latest": ["snapshot://raw/2024-09-04"],
            "artifact://report/latest": ["table://clean/latest"],
            "artifact://report/2025-01-01": ["table://clean/latest"],
            "table://from_report/latest": [
                "artifact://report/latest",
                "artifact://report/2025-01-01",
            ],
        },
    }
    jsonschema.validate(config, ALCOVE_SCHEMA)


def test_alcove_schema_rejects_hyphenated_names():
    config = {
        "version": 1,
        "steps": {"table://clean/latest": ["snapshot://raw-data/latest"]},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(config, ALCOVE_SCHEMA)


def test_build_artifact_without_deps(setup_test_environment):
    uri = StepURI.parse("artifact://report/latest")
    write_script(ARTIFACT_SCRIPT_DIR, uri, WRITE_INDEX)

    build_artifact(uri, [])

    out = artifact_path(uri)
    assert (out / "index.html").read_text() == "<h1>hello</h1>"

    metadata = load_yaml(_metadata_path(uri))
    assert metadata["uri"] == "artifact://report/latest"
    assert set(metadata["manifest"]) == {"index.html"}
    assert metadata["execution"]["status"] == "success"
    assert is_completed(uri)


def test_build_artifact_that_writes_nothing_fails(setup_test_environment):
    uri = StepURI.parse("artifact://empty/latest")
    write_script(ARTIFACT_SCRIPT_DIR, uri, WRITE_NOTHING)

    with pytest.raises(ValueError, match="did not write any files"):
        build_artifact(uri, [])

    assert not artifact_path(uri).exists()
    assert not list(ARTIFACT_DIR.rglob("*.building"))
    assert not is_completed(uri)


def test_failed_rebuild_keeps_previous_artifact(setup_test_environment):
    uri = StepURI.parse("artifact://report/latest")
    write_script(ARTIFACT_SCRIPT_DIR, uri, WRITE_INDEX)
    build_artifact(uri, [])

    # the script now crashes after writing a partial output
    write_script(ARTIFACT_SCRIPT_DIR, uri, WRITE_INDEX + "\nraise SystemExit(1)\n")
    with pytest.raises(Exception):
        build_artifact(uri, [])

    assert (artifact_path(uri) / "index.html").read_text() == "<h1>hello</h1>"
    assert not list(ARTIFACT_DIR.rglob("*.building"))
    # but it is reported stale, since the script changed
    assert not is_completed(uri)


def test_missing_script_keeps_previous_artifact(setup_test_environment):
    uri = StepURI.parse("artifact://report/latest")
    script = write_script(ARTIFACT_SCRIPT_DIR, uri, WRITE_INDEX)
    build_artifact(uri, [])

    script.unlink()
    with pytest.raises(FileNotFoundError, match="Python script"):
        build_artifact(uri, [])

    assert (artifact_path(uri) / "index.html").exists()


def test_artifact_is_gitignored(setup_test_environment):
    test_dir = setup_test_environment
    uri = StepURI.parse("artifact://report/latest")
    write_script(ARTIFACT_SCRIPT_DIR, uri, WRITE_INDEX)

    build_artifact(uri, [])

    entries = (test_dir / "data/.gitignore").read_text().split()
    assert "artifacts/" in entries


def test_artifact_rebuilds_when_table_dep_changes(setup_test_environment):
    table = StepURI.parse("table://numbers/latest")
    report = StepURI.parse("artifact://report/latest")

    alcove = Alcove.init()
    alcove.new_table(table.path, [])
    alcove.new_artifact(report.path, [str(table)])
    write_script(TABLE_SCRIPT_DIR, table, WRITE_TABLE)
    write_script(ARTIFACT_SCRIPT_DIR, report, WRITE_INDEX)

    plan_and_run(alcove)

    # the artifact was handed the table's parquet path
    dep_path = (artifact_path(report) / "dep0.txt").read_text()
    assert dep_path.endswith("numbers/latest.parquet")

    # nothing is outstanding now
    alcove.refresh()
    assert steps.prune_completed(dict(alcove.steps)) == {}

    # editing the table's script dirties the table and the artifact downstream
    write_script(TABLE_SCRIPT_DIR, table, WRITE_TABLE + "\n# changed\n")
    outstanding = steps.prune_completed(dict(alcove.steps))
    assert set(outstanding) == {table, report}

    plan_and_run(alcove)
    assert steps.prune_completed(dict(alcove.steps)) == {}


def test_table_can_depend_on_artifact(setup_test_environment):
    report = StepURI.parse("artifact://report/latest")
    listing = StepURI.parse("table://listing/latest")

    alcove = Alcove.init()
    alcove.new_artifact(report.path, [])
    alcove.new_table(listing.path, [str(report)])
    write_script(ARTIFACT_SCRIPT_DIR, report, WRITE_INDEX)
    write_script(TABLE_SCRIPT_DIR, listing, TABLE_FROM_ARTIFACT)

    plan_and_run(alcove)

    df = pl.read_parquet(TABLE_DIR / "listing/latest.parquet")
    assert df["dim_file"].to_list() == ["index.html"]


def test_artifact_versions_are_passed_individually(setup_test_environment):
    """Two versions of one artifact are not collapsed into a glob, since the
    glob would also match their .meta.yaml sidecars."""
    v1 = StepURI.parse("artifact://report/2025-01-01")
    v2 = StepURI.parse("artifact://report/2025-02-01")
    both = StepURI.parse("artifact://combined/latest")

    alcove = Alcove.init()
    alcove.new_artifact(v1.path, [])
    alcove.new_artifact(v2.path, [])
    alcove.new_artifact(both.path, [str(v1), str(v2)])
    for uri in (v1, v2, both):
        write_script(ARTIFACT_SCRIPT_DIR, uri, WRITE_INDEX)

    plan_and_run(alcove)

    out = artifact_path(both)
    assert (out / "dep0.txt").read_text().endswith("report/2025-01-01")
    assert (out / "dep1.txt").read_text().endswith("report/2025-02-01")


def test_artifacts_are_not_tables(setup_test_environment):
    alcove = Alcove.init()
    alcove.new_table("numbers/latest", [])
    alcove.new_artifact("report/latest", [])

    assert _get_tables(alcove) == ["numbers/latest"]


def test_artifact_requires_python_script(setup_test_environment):
    uri = StepURI.parse("artifact://report/latest")
    sql = (ARTIFACT_SCRIPT_DIR / uri.path).with_suffix(".sql")
    sql.parent.mkdir(parents=True, exist_ok=True)
    sql.write_text("SELECT 1")

    with pytest.raises(FileNotFoundError):
        build_artifact(uri, [])
