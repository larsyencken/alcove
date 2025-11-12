import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import duckdb
import jsonschema
import polars as pl

from alcove.exceptions import ValidationError
from alcove.paths import TABLE_DIR, TABLE_SCRIPT_DIR
from alcove.schemas import TABLE_SCHEMA
from alcove.snapshots import Snapshot
from alcove.table_metadata import _get_executable, _metadata_path, process_table_metadata
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

    # Check script and dependency checksums
    for path, checksum in input_manifest.items():
        if not Path(path).exists() or checksum != checksum_file(path):
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
    uri: StepURI, dependencies: list[StepURI], dest_path: Path
) -> dict[str, Any]:
    """Execute the table build and return runtime information."""
    command = _generate_build_command(uri, dependencies)
    start_time = datetime.now()
    runtime_info: dict[str, Any] = {
        "start_time": start_time.isoformat(),
        "status": "failed",
    }

    try:
        if command[0].suffix == ".sql":
            _exec_sql_command(uri, command)
        else:
            _exec_python_command(uri, command)

        if not dest_path.exists():
            raise Exception(
                f"Table step {uri} did not generate the expected {dest_path}"
            )

        runtime_info["status"] = "success"

    except Exception as e:
        runtime_info["error"] = str(e)
        raise

    finally:
        end_time = datetime.now()
        runtime_info["end_time"] = end_time.isoformat()
        runtime_info["duration_seconds"] = round(
            (end_time - start_time).total_seconds(),
            2,  # type: ignore
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


def _generate_build_command(uri: StepURI, dependencies: list[StepURI]) -> list[Path]:
    executable = _get_executable(uri)

    cmd = [executable]
    for dep in dependencies:
        cmd.append(_dependency_path(dep))

    dest_path = TABLE_DIR / f"{uri.path}.parquet"
    cmd.append(dest_path)

    return cmd


def _dependency_path(uri: StepURI) -> Path:
    if uri.scheme == "snapshot":
        return Snapshot.load(uri.path).path

    elif uri.scheme == "table":
        return TABLE_DIR / f"{uri.path}.parquet"
    else:
        raise ValueError(f"Unknown scheme {uri.scheme}")


def _exec_python_command(uri: StepURI, command: list[Path]) -> None:
    output_file = command[-1]
    is_update = output_file.exists()

    command_s = [sys.executable] + [str(p.resolve()) for p in command]
    subprocess.run(command_s, check=True)

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

    # add the script we used to generate the table
    executable = _get_executable(uri)
    manifest[str(executable)] = checksum_file(executable)

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


def add_placeholder_script(uri: StepURI, dependencies: list[StepURI]) -> Path:
    """Create a placeholder script for a new table.

    Args:
        uri: The table URI
        dependencies: List of dependency URIs

    Returns:
        Path to the created script

    Raises:
        ValueError: If script already exists
    """
    # Determine script type based on URI path
    is_sql = uri.path.endswith(".sql")

    # Get the script path based on the type
    if is_sql:
        # For .sql files, remove the .sql from the path
        script_path = TABLE_SCRIPT_DIR / f"{uri.path}"
    else:
        # For Python files, add .py extension
        script_path = (TABLE_SCRIPT_DIR / uri.path).with_suffix(".py")

    if script_path.exists():
        # Don't raise an error, just return the existing path
        return script_path

    script_path.parent.mkdir(parents=True, exist_ok=True)

    if is_sql:
        content = _generate_sql_template(dependencies)
    else:
        content = _generate_python_template(dependencies)

    script_path.write_text(content)
    if not is_sql:
        script_path.chmod(0o755)

    return script_path


def _generate_python_template(dependencies: list[StepURI]) -> str:
    """Generate a Python template with optional dependency loading."""
    template = """#!/usr/bin/env python3
import sys
import polars as pl

"""

    if dependencies:
        template += "# Load dependencies\n"
        for i, dep in enumerate(dependencies, start=1):
            dep_name = _get_dependency_name(dep)
            template += f"# {dep_name}_df = pl.read_parquet(sys.argv[{i}])\n"
        template += "\n"

    template += """# IMPORTANT: At least one column must be prefixed with 'dim_'
# Example: dim_country, dim_date, dim_id

data = {
    "dim_id": [1, 2, 3],
    "value": ["a", "b", "c"]
}

df = pl.DataFrame(data)

output_file = sys.argv[-1]
df.write_parquet(output_file)
"""

    return template


def _get_dependency_name(dep: StepURI) -> str:
    """Extract a reasonable name from a dependency URI.

    For paths like 'countries/2024-01-15', extract 'countries'.
    For paths like 'countries/latest', extract 'countries'.
    """
    parts = dep.path.split("/")
    # Get the second-to-last part, or last part if only one part
    if len(parts) >= 2:
        # Return the dataset name (before the version)
        return parts[-2].replace("-", "_")
    else:
        # Fallback to the last part
        return parts[-1].replace("-", "_")


def _generate_sql_template(dependencies: list[StepURI]) -> str:
    """Generate a SQL template with optional dependency references."""
    template = "-- SQL table definition\n"

    if dependencies:
        template += "-- Dependencies available as template variables:\n"
        for dep in dependencies:
            dep_name = _get_dependency_name(dep)
            template += f"--   {{{dep_name}}} -> {dep}\n"
        template += "--\n"

    template += "-- IMPORTANT: At least one column must be prefixed with 'dim_'\n"
    template += "-- Example: dim_country, dim_date, dim_id\n\n"

    if dependencies:
        # Create a template that uses the first dependency
        first_dep_name = _get_dependency_name(dependencies[0])
        template += f"""SELECT
    column1 AS dim_id,
    column2 AS value
FROM {{{first_dep_name}}}
WHERE condition = true
"""
    else:
        # Create a simple standalone table
        template += """SELECT
    1 AS dim_id,
    'example' AS value
UNION ALL
SELECT
    2 AS dim_id,
    'another_example' AS value
"""

    return template


def add_placeholder_metadata(uri: StepURI, dependencies: list[StepURI]) -> Path:
    """Create a placeholder .meta.yaml file for a new table.

    Args:
        uri: The table URI
        dependencies: List of dependency URIs

    Returns:
        Path to the created metadata file
    """
    # Get the metadata path based on the script path
    is_sql = uri.path.endswith(".sql")
    if is_sql:
        meta_path = (TABLE_SCRIPT_DIR / uri.path).with_suffix(".meta.yaml")
    else:
        meta_path = (TABLE_SCRIPT_DIR / uri.path).with_suffix(".py.meta.yaml")

    if meta_path.exists():
        # Don't overwrite existing metadata
        return meta_path

    meta_path.parent.mkdir(parents=True, exist_ok=True)

    content = _generate_metadata_template(dependencies)
    meta_path.write_text(content)

    return meta_path


def _generate_metadata_template(dependencies: list[StepURI]) -> str:
    """Generate a metadata template with inheritance configuration."""
    template = "# Metadata configuration for this table\n"
    template += "# See: https://docs.claude.com/alcove for full documentation\n\n"

    if len(dependencies) == 1:
        dep = dependencies[0]
        template += f"""# Inherit metadata from dependency
inherit:
  {dep}:
    fields:
      - name
      - description
      - source_name
      - source_url
      - license
      - license_url

# Override specific fields
override:
  description: "Derived table from {dep.path}"

"""
    elif len(dependencies) > 1:
        template += "# Inherit metadata from dependencies\n"
        template += "# Uncomment and customize as needed:\n"
        template += "# inherit:\n"
        for dep in dependencies:
            template += f"#   {dep}:\n"
            template += "#     fields:\n"
            template += "#       - name\n"
            template += "#       - source_url\n"
        template += "#\n"
        template += "# override:\n"
        template += "#   description: \"Custom description\"\n\n"
    else:
        template += "# No dependencies - add metadata directly\n"
        template += "# override:\n"
        template += "#   name: \"Table Name\"\n"
        template += "#   description: \"Table description\"\n\n"

    template += """# Validation rules (optional)
# validation:
#   required_columns:
#     - dim_id
#   unique_columns:
#     - dim_id
#   not_null:
#     - dim_id
"""

    return template
