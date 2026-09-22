import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import duckdb
import jsonschema
import polars as pl

from alcove.exceptions import ValidationError
from alcove.paths import ARTIFACT_DIR, SNAPSHOT_DIR, TABLE_DIR
from alcove.schemas import TABLE_SCHEMA
from alcove.snapshots import Snapshot
from alcove.table_metadata import (
    _get_executable,
    _metadata_path,
    process_table_metadata,
    script_manifest,
)
from alcove.types import Manifest, StepURI
from alcove.utils import checksum_file, load_yaml, print_op, save_yaml


def is_completed(uri: StepURI, deps: list[StepURI]) -> bool:
    """Check if a table is up to date."""
    assert uri.scheme == "table"

    # Check if files exist
    table_path = TABLE_DIR / f"{uri.path}.parquet"
    metadata_path = _metadata_path(uri)
    if not (table_path.exists() and metadata_path.exists()):
        return False

    # Load metadata and check dependencies
    metadata = load_yaml(metadata_path)
    input_manifest = metadata["input_manifest"]

    # Check if metadata config exists and is up to date
    config_path = Path(_get_executable(uri)).with_suffix(".meta.yaml")
    if config_path.exists():
        if str(config_path) not in input_manifest:
            return False
        if checksum_file(config_path) != input_manifest[str(config_path)]:
            return False

    return _inputs_unchanged(uri, input_manifest)


def _inputs_unchanged(uri: StepURI, input_manifest: Manifest) -> bool:
    """Every recorded input still has the same checksum, and the step's script
    has not grown any files since (a step folder may gain a template or module
    that the recorded manifest never saw)."""
    for path, checksum in input_manifest.items():
        if not Path(path).exists() or checksum != checksum_file(path):
            return False

    for path in script_manifest(uri):
        if path not in input_manifest:
            return False

    return True


def build_table(uri: StepURI, dependencies: list[StepURI]) -> None:
    """Build a table and handle its metadata."""
    assert uri.scheme == "table"

    dest_path = _prepare_output_path(uri)
    runtime_info = _execute_table_build(uri, dependencies, dest_path)
    _handle_metadata(uri, dependencies, dest_path, runtime_info)


def _prepare_output_path(uri: StepURI) -> Path:
    """Prepare the output directory and return the destination path."""
    dest_path = TABLE_DIR / f"{uri.path}.parquet"
    if dest_path.exists():
        dest_path.unlink()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    return dest_path


def _execute_table_build(
    uri: StepURI,
    dependencies: list[StepURI],
    dest_path: Path,
) -> dict[str, Any]:
    """Execute the table build and return runtime information."""
    command = _generate_build_command(uri, dependencies)

    def build() -> None:
        if command[0].suffix == ".sql":
            _exec_sql_command(uri, command)
        else:
            _exec_python_command(uri, command)

        if not dest_path.exists():
            raise Exception(
                f"Table step {uri} did not generate the expected {dest_path}"
            )

    return timed_run(build)


def timed_run(fn: Callable[[], object]) -> dict[str, Any]:
    """Run a build, returning the `execution` record shared by every step type."""
    start_time = datetime.now()
    runtime_info: dict[str, Any] = {
        "start_time": start_time.isoformat(),
        "status": "failed",
    }

    try:
        fn()
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

    return runtime_info


def _handle_metadata(
    uri: StepURI, dependencies: list[StepURI], dest_path: Path, runtime_info: dict
) -> None:
    """Process and validate the table metadata."""
    try:
        process_table_metadata(uri, dependencies, dest_path, runtime_info)
    except ValidationError:
        dest_path.unlink()
        raise


def _generate_build_command(
    uri: StepURI, dependencies: list[StepURI], dest_path: Path | None = None
) -> list[Path]:
    """The script to run, followed by each dependency's path, then the output path.

    Tables write to their Parquet file; other schemes pass their own dest_path.
    """
    executable = _get_executable(uri)

    cmd = [executable]

    # When multiple deps share the same scheme + base_path (1:n wildcard
    # expansion), collapse them into a single glob path instead of passing
    # each version individually. Artifacts are passed one directory at a
    # time: a glob over their parent would also match the .meta.yaml
    # sidecars that sit beside each version's directory.
    group_counts = Counter(f"{d.scheme}://{d.base_path}" for d in dependencies)
    added_globs: set[str] = set()
    for dep in dependencies:
        group_key = f"{dep.scheme}://{dep.base_path}"
        if group_counts[group_key] > 1 and dep.scheme != "artifact":
            if group_key not in added_globs:
                cmd.append(_dependency_glob_path(dep))
                added_globs.add(group_key)
        else:
            cmd.append(_dependency_path(dep))

    if dest_path is None:
        dest_path = TABLE_DIR / f"{uri.path}.parquet"
    cmd.append(dest_path)

    return cmd


def _dependency_path(uri: StepURI) -> Path:
    if uri.scheme == "snapshot":
        return Snapshot.load(uri.path).path

    elif uri.scheme == "table":
        return TABLE_DIR / f"{uri.path}.parquet"

    elif uri.scheme == "artifact":
        return ARTIFACT_DIR / uri.path

    else:
        raise ValueError(f"Unknown scheme {uri.scheme}")


def _dependency_glob_path(uri: StepURI) -> Path:
    """Return a glob path for all versions under a base_path, e.g. data/tables/foo/*.parquet."""
    if uri.scheme == "snapshot":
        return SNAPSHOT_DIR / uri.base_path / "*"

    elif uri.scheme == "table":
        return TABLE_DIR / uri.base_path / "*.parquet"

    else:
        # artifacts are never collapsed into a glob, see _generate_build_command
        raise ValueError(f"No glob path for scheme {uri.scheme}")


def run_python_step(command: list[Path]) -> None:
    """Run a step's script (or step folder) with the alcove interpreter.

    A step runs once per build, so bytecode caching buys nothing: -B stops
    Python writing it, and PYTHONPYCACHEPREFIX points reads at a scratch tree
    so a __pycache__ left inside a step folder by running the script by hand
    can never shadow an edited module.
    """
    command_s = [sys.executable, "-B"] + [str(p.resolve()) for p in command]
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = str(Path(tempfile.gettempdir()) / "alcove-pycache")
    subprocess.run(command_s, check=True, env=env)


def _exec_python_command(uri: StepURI, command: list[Path]) -> None:
    output_file = command[-1]
    is_update = output_file.exists()

    run_python_step(command)

    if is_update:
        print_op("UPDATE", output_file)
    else:
        print_op("CREATE", output_file)


def _exec_sql_command(uri: StepURI, command: list[Path]) -> None:
    sql_file = command[0]
    output_file = command[-1]

    template_vars = _simplify_dependency_names(command[1:-1])

    with open(sql_file, "r") as f:
        sql = f.read().format(**template_vars)

    con = duckdb.connect(database=":memory:")
    sql = f"CREATE TEMPORARY TABLE data AS ({sql})"
    try:
        con.execute(sql)

    except duckdb.ParserException as e:
        raise ValueError(f"Error executing the following SQL\n\n{sql}\n\n{e}")

    except duckdb.BinderException as e:
        raise ValueError(f"Error executing the following SQL\n\n{sql}\n\n{e}")

    is_update = output_file.exists()

    con.execute(f"COPY data TO '{output_file}' (FORMAT 'parquet')")
    if is_update:
        print_op("UPDATE", output_file)
    else:
        print_op("CREATE", output_file)


def _generate_candidate_names(dep: Path) -> Iterator[str]:
    parts = dep.parts
    name = parts[-2]
    yield name

    candidates = [name]
    for p in reversed(parts[:-2]):
        name = f"{p}_{name}"
        yield name

    version = parts[-1].replace("-", "")
    candidates.append(f"{name}_{version}")
    while True:
        yield name


def _simplify_dependency_names(deps: list[Path]) -> dict[str, Path]:
    mapping = {}

    to_map = {d: iter(_generate_candidate_names(d)) for d in deps}

    frontier = {d: next(to_map[d]) for d in deps}
    duplicates = {k for k, v in Counter(frontier.values()).items() if v >= 2}

    for d, name in list(frontier.items()):
        if d not in duplicates:
            mapping[name] = d
            del frontier[d]

    last_duplicates = duplicates
    while duplicates:
        frontier = {d: next(to_map[d]) for d in list(frontier)}
        duplicates = {k for k, v in Counter(frontier.values()).items() if v >= 2}
        if duplicates == last_duplicates:
            raise Exception(f"infinite loop resolving dependencies: {deps}")

        for d, name in list(frontier.items()):
            if d not in duplicates:
                mapping[name] = d
                del frontier[d]

    return mapping


def _gen_metadata(uri: StepURI, dependencies: list[StepURI]) -> None:
    dest_path = _metadata_path(uri)
    metadata = {
        "uri": str(uri),
        "version": 1,
        "checksum": checksum_file(TABLE_DIR / f"{uri.path}.parquet"),
        "input_manifest": _generate_input_manifest(uri, dependencies),
    }

    if len(dependencies) == 1:
        # inherit metadata from the dependency
        dep_metadata_path = _metadata_path(dependencies[0])
        dep_metadata = load_yaml(dep_metadata_path)
        for field in [
            "name",
            "source_name",
            "source_url",
            "date_accessed",
            "access_notes",
        ]:
            if field in dep_metadata:
                metadata[field] = str(dep_metadata[field])

    metadata["schema"] = _infer_schema(uri)

    jsonschema.validate(metadata, TABLE_SCHEMA)

    columns = list(metadata["schema"].keys())
    if not any(col.startswith("dim_") for col in columns):
        # we have not yet written this metadata, so the step is not yet complete
        raise ValueError(
            f"Table {uri} does not have any dimension columns prefixed with dim_, found: {columns}"
        )

    save_yaml(metadata, dest_path)


def _generate_input_manifest(uri: StepURI, dependencies: list[StepURI]) -> Manifest:
    manifest = {}

    # add the script (or step folder) we used to generate the table
    manifest.update(script_manifest(uri))

    # add every dependency's metadata file; that file includes a checksum of its data,
    # so we cover both data and metadata this way
    for dep in dependencies:
        dep_metadata_file = _metadata_path(dep)
        manifest[str(dep_metadata_file)] = checksum_file(dep_metadata_file)

    return manifest


def _infer_schema(uri: StepURI) -> dict[str, str]:
    data_path = TABLE_DIR / f"{uri.path}.parquet"
    df = pl.read_parquet(data_path)
    return {col: str(dtype) for col, dtype in df.schema.items()}


def add_placeholder_script(uri: StepURI) -> Path:
    script_path = _get_executable(uri, check=False)
    if script_path.exists():
        raise ValueError(f"Script already exists: {script_path}")

    script_path.parent.mkdir(parents=True, exist_ok=True)
    content = """#!/usr/bin/env python3
import sys
import polars as pl

data = {
    "a": [1, 1, 3],
    "b": [2, 3, 5],
    "c": [3, 4, 6]
}

df = pl.DataFrame(data)

output_file = sys.argv[-1]
df.write_parquet(output_file)
"""

    script_path.write_text(content)
    script_path.chmod(0o755)

    return script_path
