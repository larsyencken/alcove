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
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

from alcove.paths import ARTIFACT_DIR, DATA_DIR
from alcove.schemas import ARTIFACT_SCHEMA
from alcove.table_metadata import _get_executable, _metadata_path
from alcove.tables import _generate_build_command
from alcove.types import Manifest, StepURI
from alcove.utils import (
    add_entry_to_file,
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
    "Run the artifact's script into a fresh directory and record what it made."
    assert uri.scheme == "artifact"

    dest_path = _prepare_output_dir(uri)
    runtime_info = _execute_artifact_build(uri, dependencies, dest_path)
    manifest = _checksum_output(uri, dest_path)

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
    add_entry_to_file(DATA_DIR / ".gitignore", "artifacts/")


def _prepare_output_dir(uri: StepURI) -> Path:
    "Start from an empty directory so stale files from a previous build cannot linger."
    dest_path = artifact_path(uri)
    if dest_path.exists():
        shutil.rmtree(dest_path)
    dest_path.mkdir(parents=True)
    return dest_path


def _execute_artifact_build(
    uri: StepURI, dependencies: list[StepURI], dest_path: Path
) -> dict[str, Any]:
    command = _generate_build_command(uri, dependencies, dest_path)
    if command[0].suffix != ".py":
        raise ValueError(f"Artifact {uri} must be built by a Python script")

    start_time = datetime.now()
    runtime_info: dict[str, Any] = {
        "start_time": start_time.isoformat(),
        "status": "failed",
    }

    try:
        command_s = [sys.executable] + [str(p.resolve()) for p in command]
        subprocess.run(command_s, check=True)
        runtime_info["status"] = "success"

    except Exception as e:
        runtime_info["error"] = str(e)
        raise

    finally:
        end_time = datetime.now()
        runtime_info["end_time"] = end_time.isoformat()
        runtime_info["duration_seconds"] = round(
            (end_time - start_time).total_seconds(), 2
        )

    print_op("CREATE", dest_path)
    return runtime_info


def _checksum_output(uri: StepURI, dest_path: Path) -> Manifest:
    try:
        return checksum_folder(dest_path)
    except Exception:
        shutil.rmtree(dest_path, ignore_errors=True)
        raise ValueError(
            f"Artifact step {uri} did not write any files into {dest_path}"
        )


def _generate_input_manifest(uri: StepURI, dependencies: list[StepURI]) -> Manifest:
    manifest = {}

    # the script we used to generate the artifact
    executable = _get_executable(uri)
    manifest[str(executable)] = checksum_file(executable)

    # every dependency's metadata file; it includes a checksum of the data,
    # so this covers both data and metadata
    for dep in dependencies:
        dep_metadata_file = _metadata_path(dep)
        manifest[str(dep_metadata_file)] = checksum_file(dep_metadata_file)

    return manifest
