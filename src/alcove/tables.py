import os
import re
import shutil
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
from alcove.partitions import PARTITION_GLOB, is_partition_version
from alcove.paths import ARTIFACT_DIR, DATA_DIR, SNAPSHOT_DIR, TABLE_DIR
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

    return _inputs_unchanged(uri, input_manifest, deps)


def _inputs_unchanged(
    uri: StepURI, input_manifest: Manifest, deps: list[StepURI]
) -> bool:
    """Every recorded input still has the same checksum, the step's script
    has not grown any files since (a step folder may gain a template or module
    that the recorded manifest never saw), and the step still depends on
    exactly the versions it was built from.

    The last check catches a `latest` dependency that now resolves to a newer
    version, and a partition added to or removed from a wildcard dependency:
    in both cases every recorded input is unchanged, yet the step is stale.
    """
    built_from = {
        path for path in input_manifest if Path(path).is_relative_to(DATA_DIR)
    }
    if built_from != {str(_metadata_path(dep)) for dep in deps}:
        return False

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
            _exec_script_command(uri, command)

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
    for group in _argument_groups(dependencies):
        if len(group) > 1:
            cmd.append(_dependency_glob_path(group))
        else:
            cmd.append(_dependency_path(group[0]))

    if dest_path is None:
        dest_path = TABLE_DIR / f"{uri.path}.parquet"
    cmd.append(dest_path)

    return cmd


def _argument_groups(dependencies: list[StepURI]) -> list[list[StepURI]]:
    """The dependencies behind each argument a step's script is given, in order.

    When multiple deps share the same scheme + base_path (1:n wildcard
    expansion), they collapse into a single glob path instead of passing
    each version individually. Artifacts are passed one directory at a
    time: a glob over their parent would also match the .meta.yaml
    sidecars that sit beside each version's directory.
    """
    groups: dict[str, list[StepURI]] = {}
    for dep in dependencies:
        if dep.scheme == "artifact":
            groups[f"{dep}#{len(groups)}"] = [dep]
        else:
            groups.setdefault(f"{dep.scheme}://{dep.base_path}", []).append(dep)

    return list(groups.values())


def _dependency_path(uri: StepURI) -> Path:
    if uri.scheme == "snapshot":
        return Snapshot.load(uri.path).path

    elif uri.scheme == "table":
        return TABLE_DIR / f"{uri.path}.parquet"

    elif uri.scheme == "artifact":
        return ARTIFACT_DIR / uri.path

    else:
        raise ValueError(f"Unknown scheme {uri.scheme}")


def _dependency_glob_path(versions: list[StepURI]) -> Path:
    """A glob over several versions of one dataset, e.g. data/tables/foo/*.parquet,
    data/snapshots/foo/????-??-?? or data/snapshots/foo/????-??-??.csv.

    For date-named snapshot versions it matches their data and never their
    .meta.yaml sidecars. Other version names fall back to `*`, which for
    directory snapshots also matches the sidecars, as it always has."""
    uri = versions[0]
    if uri.scheme == "snapshot":
        # date-named partitions can be matched exactly; other names can't
        stem = (
            PARTITION_GLOB
            if all(is_partition_version(v.version) for v in versions)
            else "*"
        )

        kinds = set()
        for v in versions:
            snapshot = Snapshot.load(v.path)
            kinds.add((snapshot.snapshot_type, snapshot.extension))

        if len(kinds) > 1:
            raise ValueError(
                f"Versions of snapshot://{uri.base_path} mix files and directories "
                f"or file extensions ({sorted(map(str, kinds))}), so one glob "
                "cannot read them all"
            )

        ((snapshot_type, extension),) = kinds
        if snapshot_type == "file":
            return SNAPSHOT_DIR / uri.base_path / f"{stem}{extension}"

        return SNAPSHOT_DIR / uri.base_path / stem

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


def run_nu_step(command: list[Path]) -> None:
    """Run a step's nushell script.

    Nushell reads neither config.nu nor env.nu when it runs a script, so a
    step can't come to depend on commands defined only in someone's shell
    config. Plugins registered with `plugin add`, such as polars, are still
    available.
    """
    nu = shutil.which("nu")
    if nu is None:
        raise FileNotFoundError(
            f"Running {command[0]} needs nushell, but `nu` is not on the PATH"
        )

    subprocess.run([nu] + [str(p.resolve()) for p in command], check=True)


def run_script_step(command: list[Path]) -> None:
    "Run a step's build script with nushell if it is a `.nu` file, else Python."
    if command[0].suffix == ".nu":
        run_nu_step(command)
    else:
        run_python_step(command)


def _exec_script_command(uri: StepURI, command: list[Path]) -> None:
    output_file = command[-1]
    is_update = output_file.exists()

    run_script_step(command)

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
    """Template variable names for a dependency, shortest first: its dataset
    name, then that name prefixed with each enclosing folder in turn (so
    `snapshots_foo` and `tables_foo` tell apart a snapshot and a table both
    called foo), then with its version appended. All-digit names are skipped,
    since str.format reads `{1990}` as a positional index."""
    parts = dep.parts
    names = [parts[-2]]
    for p in reversed(parts[:-2]):
        names.append(f"{p}_{names[-1]}")

    # a glob such as ????-??-??.csv has no version to add
    version = re.sub(r"\W", "", parts[-1].split(".")[0])
    if version:
        names += [f"{parts[-2]}_{version}", f"{names[-1]}_{version}"]

    for name in names:
        if not name.isdigit():
            yield name


def _simplify_dependency_names(deps: list[Path]) -> dict[str, Path]:
    """Give each dependency the shortest candidate name that no other
    dependency shares."""
    candidates = {d: _generate_candidate_names(d) for d in deps}
    current = {d: next(candidates[d]) for d in deps}

    mapping: dict[str, Path] = {}
    while current:
        counts = Counter(current.values())
        for d, name in list(current.items()):
            if counts[name] == 1 and name not in mapping:
                mapping[name] = d
                del current[d]

        for d in current:
            try:
                current[d] = next(candidates[d])
            except StopIteration:
                raise ValueError(
                    f"Cannot give {d} a template variable name distinct from "
                    f"the other dependencies: {deps}"
                )

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
