"""A step may be a folder holding a `__main__.py` entrypoint plus any other
files it needs; the whole folder is the step's input."""

from pathlib import Path

import polars as pl
from alcove import plan_and_run, steps
from alcove.artifacts import artifact_path
from alcove.core import Alcove
from alcove.paths import ARTIFACT_SCRIPT_DIR, TABLE_DIR, TABLE_SCRIPT_DIR
from alcove.table_metadata import _get_executable, _metadata_path, script_manifest
from alcove.types import StepURI
from alcove.utils import checksum_folder, load_yaml

TABLE_MAIN = """import sys
import polars as pl
from helpers import ROWS

pl.DataFrame({"dim_key": list(range(ROWS)), "value": [1] * ROWS}).write_parquet(sys.argv[-1])
"""

TABLE_HELPER = "ROWS = 3\n"

# renders a sibling template with the number of rows in its table dependency
ARTIFACT_MAIN = """import sys
from pathlib import Path
import polars as pl

*deps, out = sys.argv[1:]
template = (Path(__file__).parent / "template.html").read_text()
n = len(pl.read_parquet(deps[0]))
(Path(out) / "index.html").write_text(template.replace("{n}", str(n)))
"""


def make_folder(script_dir: Path, uri: StepURI, files: dict[str, str]) -> Path:
    folder = script_dir / uri.path
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (folder / name).write_text(body)
    return folder


def outstanding(alcove: Alcove) -> set[StepURI]:
    alcove.refresh()
    return set(steps.prune_completed(dict(alcove.steps)))


def test_table_folder_step(setup_test_environment):
    uri = StepURI.parse("table://numbers/latest")
    folder = make_folder(
        TABLE_SCRIPT_DIR, uri, {"__main__.py": TABLE_MAIN, "helpers.py": TABLE_HELPER}
    )

    alcove = Alcove.init()
    alcove.new_table(uri.path, [])
    plan_and_run(alcove)

    df = pl.read_parquet(TABLE_DIR / "numbers/latest.parquet")
    assert len(df) == 3

    # every file in the folder is an input, the bytecode cache is not
    assert (folder / "__pycache__").is_dir()
    manifest = load_yaml(_metadata_path(uri))["input_manifest"]
    assert str(folder / "__main__.py") in manifest
    assert str(folder / "helpers.py") in manifest
    assert not any("__pycache__" in path for path in manifest)
    assert outstanding(alcove) == set()

    # editing a helper, not the entrypoint, dirties the step
    (folder / "helpers.py").write_text("ROWS = 5\n")
    assert outstanding(alcove) == {uri}
    plan_and_run(alcove)
    assert len(pl.read_parquet(TABLE_DIR / "numbers/latest.parquet")) == 5


def test_artifact_folder_step_tracks_its_template(setup_test_environment):
    table = StepURI.parse("table://numbers/latest")
    report = StepURI.parse("artifact://report/latest")
    make_folder(
        TABLE_SCRIPT_DIR, table, {"__main__.py": TABLE_MAIN, "helpers.py": TABLE_HELPER}
    )
    folder = make_folder(
        ARTIFACT_SCRIPT_DIR,
        report,
        {"__main__.py": ARTIFACT_MAIN, "template.html": "<h1>{n} rows</h1>"},
    )

    alcove = Alcove.init()
    alcove.new_table(table.path, [])
    alcove.new_artifact(report.path, [str(table)])
    plan_and_run(alcove)

    page = artifact_path(report) / "index.html"
    assert page.read_text() == "<h1>3 rows</h1>"
    assert outstanding(alcove) == set()

    # a template-only edit rebuilds the artifact and nothing upstream
    (folder / "template.html").write_text("<h2>{n} rows</h2>")
    assert outstanding(alcove) == {report}
    plan_and_run(alcove)
    assert page.read_text() == "<h2>3 rows</h2>"

    # so does a file the recorded manifest has never seen
    (folder / "styles.css").write_text("h2 { color: teal }")
    assert outstanding(alcove) == {report}
    plan_and_run(alcove)
    assert outstanding(alcove) == set()

    # and removing one
    (folder / "styles.css").unlink()
    assert outstanding(alcove) == {report}


def test_single_script_takes_precedence_over_folder(setup_test_environment):
    uri = StepURI.parse("table://numbers/latest")
    make_folder(TABLE_SCRIPT_DIR, uri, {"__main__.py": TABLE_MAIN})
    script = (TABLE_SCRIPT_DIR / uri.path).with_suffix(".py")
    script.write_text("#!/usr/bin/env python3\n")
    script.chmod(0o755)

    assert _get_executable(uri) == script
    assert script_manifest(uri).keys() == {str(script)}


def test_folder_without_main_is_not_a_step(setup_test_environment):
    uri = StepURI.parse("table://numbers/latest")
    make_folder(TABLE_SCRIPT_DIR, uri, {"helpers.py": TABLE_HELPER})

    try:
        _get_executable(uri)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("a folder with no __main__.py should not resolve")


def test_checksum_folder_ignores_named_dirs(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.pyc").write_text("x")
    (tmp_path / "sub" / "__pycache__").mkdir(parents=True)
    (tmp_path / "sub" / "__pycache__" / "b.pyc").write_text("y")
    (tmp_path / "sub" / "b.txt").write_text("b")

    assert set(checksum_folder(tmp_path)) == {
        "a.txt",
        "__pycache__/a.pyc",
        "sub/__pycache__/b.pyc",
        "sub/b.txt",
    }
    assert set(checksum_folder(tmp_path, ignore_dirs=frozenset({"__pycache__"}))) == {
        "a.txt",
        "sub/b.txt",
    }
