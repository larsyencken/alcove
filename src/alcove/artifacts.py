"""Artifact steps: build outputs that are not tables.

A table step must produce a single Parquet file that alcove can load, validate
and serve through DuckDB. An artifact step is for everything else that is
still a pure function of its dependencies: a rendered HTML dashboard, a fitted
model, a generated report, a bundle of images.

An artifact is built by an executable Python script at
``src/steps/artifacts/<path>.py``. It is called with the path of each
dependency followed by an empty output directory, and must write at least one
file into that directory. Alcove then checksums every file it produced and
records the manifest, along with the checksums of the script and dependency
metadata, in ``data/artifacts/<path>.meta.yaml``. The step is rebuilt whenever
any of those inputs change.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema

from alcove.paths import ARTIFACT_DIR
from alcove.schemas import ARTIFACT_SCHEMA
from alcove.table_metadata import _metadata_path
from alcove.tables import _generate_build_command, _generate_input_manifest, timed_run
from alcove.types import Manifest, StepURI
from alcove.utils import (
    checksum_file,
    checksum_folder,
    checksum_manifest,
    ensure_data_gitignore,
    load_yaml,
    print_op,
    save_yaml,
)


def artifact_path(uri: StepURI) -> Path:
    "The directory an artifact step writes its files into."
    assert uri.scheme == "artifact"
    return ARTIFACT_DIR / uri.path


def is_completed(uri: StepURI) -> bool:
    "An artifact is up to date when its output exists and no input has changed."
    assert uri.scheme == "artifact"

    dest_path = artifact_path(uri)
    metadata_path = _metadata_path(uri)
    if not (dest_path.is_dir() and metadata_path.exists()):
        return False

    metadata = load_yaml(metadata_path)

    # every file the artifact produced must still be there
    for rel_path in metadata["manifest"]:
        if not (dest_path / rel_path).exists():
            return False

    # and every input it was built from must be unchanged
    for path, checksum in metadata["input_manifest"].items():
        if not Path(path).exists() or checksum != checksum_file(path):
            return False

    return True


def build_artifact(uri: StepURI, dependencies: list[StepURI]) -> None:
    """Build into a scratch directory, then swap it in.

    The previous build stays in place until the new one has succeeded and been
    checksummed, so a missing script or a failing build never destroys the
    artifact that is currently being served.
    """
    assert uri.scheme == "artifact"

    dest_path = artifact_path(uri)
    build_path = dest_path.with_name(f"{dest_path.name}.building")

    # resolve the script before touching anything on disk
    command = _generate_build_command(uri, dependencies, build_path)

    _reset_dir(build_path)
    try:
        runtime_info = _execute_artifact_build(command)
        manifest = _checksum_output(uri, build_path)
    except Exception:
        shutil.rmtree(build_path, ignore_errors=True)
        raise

    if dest_path.exists():
        shutil.rmtree(dest_path)
        print_op("UPDATE", dest_path)
    else:
        print_op("CREATE", dest_path)
    build_path.rename(dest_path)

    metadata = {
        "uri": str(uri),
        "version": 1,
        "checksum": checksum_manifest(manifest),
        "manifest": manifest,
        "input_manifest": _generate_input_manifest(uri, dependencies),
        "execution": runtime_info,
    }
    jsonschema.validate(metadata, ARTIFACT_SCHEMA)
    save_yaml(metadata, _metadata_path(uri))

    # artifacts are build products, like tables; keep them out of git
    ensure_data_gitignore()


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def _execute_artifact_build(command: list[Path]) -> dict[str, Any]:
    command_s = [sys.executable] + [str(p.resolve()) for p in command]
    return timed_run(lambda: subprocess.run(command_s, check=True))


def _checksum_output(uri: StepURI, build_path: Path) -> Manifest:
    if not any(p.is_file() for p in build_path.rglob("*")):
        raise ValueError(
            f"Artifact step {uri} did not write any files into {build_path}"
        )

    return checksum_folder(build_path)
