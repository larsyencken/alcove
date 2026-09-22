"""A step may be a folder holding a `__main__.py` entrypoint plus any other
files it needs; the whole folder is the step's input."""

import importlib.util
import os
import py_compile
from pathlib import Path

import polars as pl
import pytest
from alcove import plan_and_run, steps
from alcove.artifacts import artifact_path
from alcove.core import Alcove
from alcove.paths import ARTIFACT_SCRIPT_DIR, TABLE_DIR, TABLE_SCRIPT_DIR
from alcove.table_metadata import (
    _get_executable,
    _metadata_path,
    script_manifest,
    step_folder_files,
)
from alcove.types import StepURI
from alcove.utils import load_yaml

TABLE_MAIN = """import sys
import polars as pl
from helpers import ROWS

pl.DataFrame({"dim_key": list(range(ROWS)), "value": [1] * ROWS}).write_parquet(sys.argv[-1])
"""

TABLE_HELPER = "ROWS = 3\n"

# renders a sibling template with the number of rows in its table dependency,
# via a sibling helper module
ARTIFACT_MAIN = """import sys
from pathlib import Path
import polars as pl
from render import render

*deps, out = sys.argv[1:]
template = (Path(__file__).parent / "template.html").read_text()
(Path(out) / "index.html").write_text(render(template, len(pl.read_parquet(deps[0]))))
"""

ARTIFACT_HELPER = """def render(template, n):
    return template.replace("{n}", str(n))
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

    # alcove runs steps with -B, so no bytecode cache appears
    assert not (folder / "__pycache__").exists()
    manifest = load_yaml(_metadata_path(uri))["input_manifest"]
    assert manifest.keys() >= {str(folder / "__main__.py"), str(folder / "helpers.py")}
    assert outstanding(alcove) == set()

    # a cache left behind by running the script by hand is not an input
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "junk.cpython-312.pyc").write_bytes(b"\0")
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
        {
            "__main__.py": ARTIFACT_MAIN,
            "render.py": ARTIFACT_HELPER,
            "template.html": "<h1>{n} rows</h1>",
        },
    )

    alcove = Alcove.init()
    alcove.new_table(table.path, [])
    alcove.new_artifact(report.path, [str(table)])
    plan_and_run(alcove)

    page = artifact_path(report) / "index.html"
    assert page.read_text() == "<h1>3 rows</h1>"
    assert not (folder / "__pycache__").exists()
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


def test_stale_bytecode_cannot_shadow_an_edited_module(setup_test_environment):
    """A __pycache__ left by running the script by hand, whose .pyc records the
    same source size and mtime second as an edited module, must not be used."""
    uri = StepURI.parse("table://numbers/latest")
    folder = make_folder(
        TABLE_SCRIPT_DIR, uri, {"__main__.py": TABLE_MAIN, "helpers.py": TABLE_HELPER}
    )
    helpers = folder / "helpers.py"
    py_compile.compile(
        str(helpers), cfile=importlib.util.cache_from_source(str(helpers))
    )
    assert (folder / "__pycache__").is_dir()

    # same byte length, same mtime: exactly what the .pyc header checks
    st = helpers.stat()
    helpers.write_text("ROWS = 5\n")
    os.utime(helpers, ns=(st.st_atime_ns, st.st_mtime_ns))

    alcove = Alcove.init()
    alcove.new_table(uri.path, [])
    plan_and_run(alcove)

    assert len(pl.read_parquet(TABLE_DIR / "numbers/latest.parquet")) == 5


def test_folder_takes_precedence_over_shared_parent_script(setup_test_environment):
    uri = StepURI.parse("table://numbers/latest")
    folder = make_folder(TABLE_SCRIPT_DIR, uri, {"__main__.py": TABLE_MAIN})
    shared = TABLE_SCRIPT_DIR / "numbers.py"
    shared.write_text("#!/usr/bin/env python3\n")
    shared.chmod(0o755)

    assert _get_executable(uri) == folder


def test_config_sidecar_sits_beside_the_folder(setup_test_environment):
    uri = StepURI.parse("table://numbers/latest")
    folder = make_folder(
        TABLE_SCRIPT_DIR, uri, {"__main__.py": TABLE_MAIN, "helpers.py": TABLE_HELPER}
    )
    (folder.parent / "latest.meta.yaml").write_text("override:\n  name: Numbers\n")

    alcove = Alcove.init()
    alcove.new_table(uri.path, [])
    plan_and_run(alcove)

    metadata = load_yaml(_metadata_path(uri))
    assert metadata["name"] == "Numbers"
    assert str(folder.parent / "latest.meta.yaml") in metadata["input_manifest"]


def test_step_folder_files_skips_debris(tmp_path):
    for name in [
        "__main__.py",
        "helper.py",
        "data/lookup.csv",
        "__pycache__/helper.cpython-312.pyc",
        "data/__pycache__/x.pyc",
        ".__main__.py.swp",
        ".ruff_cache/CACHEDIR.TAG",
        ".DS_Store",
        "helper.py~",
        "#helper.py#",
    ]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    rel = [str(f.relative_to(tmp_path)) for f in step_folder_files(tmp_path)]
    assert rel == ["__main__.py", "data/lookup.csv", "helper.py"]


def test_step_folder_files_follows_symlinked_dirs(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.html").write_text("a")
    folder = tmp_path / "step"
    folder.mkdir()
    (folder / "__main__.py").write_text("x")
    (folder / "tpl").symlink_to(shared, target_is_directory=True)

    rel = [str(f.relative_to(folder)) for f in step_folder_files(folder)]
    assert rel == ["__main__.py", "tpl/a.html"]


def test_step_folder_files_rejects_dangling_symlink_and_cycles(tmp_path):
    folder = tmp_path / "step"
    folder.mkdir()
    (folder / "__main__.py").write_text("x")

    (folder / "missing").symlink_to(tmp_path / "nowhere")
    with pytest.raises(FileNotFoundError, match="Dangling symlink"):
        list(step_folder_files(folder))
    (folder / "missing").unlink()

    (folder / "loop").symlink_to(folder, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink cycle"):
        list(step_folder_files(folder))
