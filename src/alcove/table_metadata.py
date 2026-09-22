# table_metadata.py

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List

import jsonschema
import polars as pl
from rich.console import Console

from alcove.exceptions import ValidationError
from alcove.paths import (
    ARTIFACT_DIR,
    ARTIFACT_SCRIPT_DIR,
    SNAPSHOT_DIR,
    TABLE_DIR,
    TABLE_SCRIPT_DIR,
)
from alcove.schemas import TABLE_CONFIG_SCHEMA
from alcove.types import Manifest, StepURI
from alcove.utils import checksum_file, load_yaml, save_yaml

console = Console()


@dataclass
class ValidationResult:
    passed: bool
    errors: List[str]

    def __bool__(self):
        return self.passed


class TableMetadata:
    def __init__(self, uri: StepURI):
        self.uri = uri
        self.config = self._load_config()
        self.inherited: Dict[str, Any] = {}
        self.runtime: Dict[str, Any] = {}

    def _load_config(self) -> dict:
        """Load and validate the table configuration file if it exists."""
        config_path = self._get_config_path()
        if not config_path.exists():
            return {}

        config = load_yaml(config_path)
        try:
            jsonschema.validate(config, TABLE_CONFIG_SCHEMA)
        except jsonschema.ValidationError as e:
            raise ValidationError(f"Invalid table configuration: {e}")

        return config

    def _get_config_path(self) -> Path:
        """Get the path to the table's metadata configuration file."""
        executable = _get_executable(self.uri, check=False)
        return Path(executable).with_suffix(".meta.yaml")

    def resolve_inheritance(self, dependencies: List[StepURI]) -> None:
        """Resolve and validate inherited metadata from dependencies."""
        if not self.config and len(dependencies) == 1:
            # default to inheriting all fields from the single dependency
            inherit = {
                str(dependencies[0]): {
                    "fields": [
                        "name",
                        "description",
                        "source_name",
                        "source_url",
                        "access_notes",
                        "license",
                        "license_url",
                    ]
                }
            }
        else:
            # otherwise, use the specified inheritance
            inherit = self.config.get("inherit")

        if not inherit:
            return

        for dep_uri, settings in inherit.items():
            dep = StepURI.parse(dep_uri)
            if dep not in dependencies:
                raise ValidationError(
                    f"Cannot inherit from {dep_uri} as it is not a dependency"
                )

            dep_metadata = load_yaml(_metadata_path(dep))
            self.inherited.update(
                {
                    field: dep_metadata[field]
                    for field in settings["fields"]
                    if field in dep_metadata
                }
            )

    def validate_schema(self, df: pl.DataFrame) -> ValidationResult:
        """Validate the dataframe against schema specifications."""
        errors = []

        # Check schema if specified
        if schema_spec := self.config.get("schema"):
            df_schema = {col: str(dtype) for col, dtype in df.schema.items()}
            for col, dtype in schema_spec.items():
                if col not in df_schema:
                    errors.append(f"Missing column: {col}")
                elif df_schema[col] != dtype:
                    errors.append(
                        f"Type mismatch for {col}: expected {dtype}, got {df_schema[col]}"
                    )

        # Check validation rules
        if validation := self.config.get("validation"):
            # Check required columns
            for col in validation.get("required_columns", []):
                if col not in df.columns:
                    errors.append(f"Required column missing: {col}")

            # Check unique columns
            for col in validation.get("unique_columns", []):
                if col in df.columns and df[col].n_unique() != len(df):
                    errors.append(f"Column not unique: {col}")

            # Check for null values
            for col in validation.get("not_null", []):
                if col in df.columns and df[col].null_count() > 0:
                    errors.append(f"Column contains null values: {col}")

        return ValidationResult(not errors, errors)

    def generate(self, output_path: Path, dependencies: List[StepURI]) -> dict:
        """Generate the final metadata for the table."""
        # Start with inherited metadata
        metadata = self.inherited.copy()

        # Apply overrides
        overrides = self.config.get("override", {})
        metadata.update(overrides)

        # Add schema information
        df = pl.read_parquet(output_path)
        metadata["schema"] = {col: str(dtype) for col, dtype in df.schema.items()}

        # Add execution information
        metadata["execution"] = self.runtime

        # Add standard fields
        metadata.update(
            {
                "uri": str(self.uri),
                "version": 1,
                "checksum": checksum_file(output_path),
                "input_manifest": self._generate_input_manifest(dependencies),
            }
        )

        return metadata

    def _generate_input_manifest(self, dependencies: List[StepURI]) -> Dict[str, str]:
        """Generate the input manifest including script and dependency metadata."""
        manifest = {}

        # Add the script (or step folder) we used to generate the table
        manifest.update(script_manifest(self.uri))

        # Add the metadata config if it exists
        config_path = self._get_config_path()
        if config_path.exists():
            manifest[str(config_path)] = checksum_file(config_path)

        # add every dependency's metadata file; that file includes a checksum of its data,
        # so we cover both data and metadata this way
        for dep in dependencies:
            dep_metadata_file = _metadata_path(dep)
            manifest[str(dep_metadata_file)] = checksum_file(dep_metadata_file)

        return manifest


def process_table_metadata(
    uri: StepURI,
    dependencies: List[StepURI],
    output_path: Path,
    runtime_info: Dict[str, Any],
) -> None:
    """Main function to handle table metadata processing."""
    metadata = TableMetadata(uri)

    # Pre-execution
    metadata.resolve_inheritance(dependencies)

    # Use the runtime info from table execution
    metadata.runtime = runtime_info

    # Read and validate the generated table
    df = pl.read_parquet(output_path)
    validation_result = metadata.validate_schema(df)
    if not validation_result:
        error_msg = "\n".join(validation_result.errors)
        raise ValidationError(f"Table validation failed for {uri}:\n{error_msg}")

    # Enforce uniqueness on dim_ columns
    dim_cols = [col for col in df.columns if col.startswith("dim_")]
    if dim_cols:
        n_rows = len(df)
        n_unique = df.select(dim_cols).n_unique()
        if n_unique != n_rows:
            n_dupes = n_rows - n_unique
            raise ValidationError(
                f"Table {uri} has {n_dupes} duplicate rows "
                f"for dim columns {dim_cols} "
                f"({n_rows} rows, {n_unique} unique)"
            )

    # Generate and save final metadata
    final_metadata = metadata.generate(output_path, dependencies)
    save_yaml(final_metadata, _metadata_path(uri))


def _script_dir(uri: StepURI) -> Path:
    if uri.scheme == "table":
        return TABLE_SCRIPT_DIR

    elif uri.scheme == "artifact":
        return ARTIFACT_SCRIPT_DIR

    raise ValueError(f"Scheme {uri.scheme} has no build scripts")


def _is_step_debris(name: str) -> bool:
    """Files and directories inside a step folder that are never part of the
    step: bytecode caches, dotfiles and dot-directories (editor swap files,
    tool caches such as .ruff_cache, .DS_Store), and editor backups."""
    return (
        name == "__pycache__"
        or name.startswith(".")
        or name.endswith("~")
        or (name.startswith("#") and name.endswith("#"))
    )


def step_folder_files(folder: Path) -> Iterator[Path]:
    """Every file that makes up a step folder, in a stable order.

    Symlinked directories are followed, so a template shared between steps via
    a symlink still counts as an input; a dangling symlink or a symlink cycle
    is an error rather than a silent gap in the manifest.
    """
    seen: set[Path] = set()

    def walk(directory: Path) -> Iterator[Path]:
        real = directory.resolve()
        if real in seen:
            raise ValueError(f"Step folder {folder} has a symlink cycle at {directory}")
        seen.add(real)

        for entry in sorted(directory.iterdir()):
            if _is_step_debris(entry.name):
                continue
            if not entry.exists():
                raise FileNotFoundError(f"Dangling symlink in step folder: {entry}")
            if entry.is_dir():
                yield from walk(entry)
            elif entry.is_file():
                yield entry

    yield from walk(folder)


def _get_executable(uri: StepURI, check: bool = True) -> Path:
    """Find what builds a step.

    In order of precedence: a `<path>.py` or `<path>.sql` script, a step
    folder `<path>/` holding a `__main__.py` entrypoint (Python runs the
    folder), or a `<parent>.py` / `<parent>.sql` script shared across every
    version of the step. A step folder can carry other files beside the
    entrypoint, such as templates or lookup data, and the whole folder counts
    as the step's input.
    """
    base = _script_dir(uri) / uri.path

    # artifacts are built by Python only; SQL steps always produce a table
    suffixes = [".py", ".sql"] if uri.scheme == "table" else [".py"]

    for exec_base in [base, base.parent]:
        for suffix in suffixes:
            script = exec_base.with_suffix(suffix)
            if not script.exists():
                continue

            if suffix == ".py" and check and not _is_valid_script(script):
                raise Exception(f"Missing execute permissions on {script}")

            return script

        if exec_base == base and (base / "__main__.py").is_file():
            return base

    hint = (
        " (artifacts must be built by a Python script)"
        if uri.scheme == "artifact"
        else ""
    )
    raise FileNotFoundError(f"Could not find script for {uri}{hint}")


def script_manifest(uri: StepURI) -> Manifest:
    """Checksums of the files that make up a step's build script: the single
    script file, or every file in a step folder."""
    executable = _get_executable(uri)
    if executable.is_dir():
        return {str(f): checksum_file(f) for f in step_folder_files(executable)}

    return {str(executable): checksum_file(executable)}


def _is_valid_script(script: Path) -> bool:
    return script.is_file() and os.access(script, os.X_OK)


def _metadata_path(uri: StepURI) -> Path:
    if uri.scheme == "snapshot":
        return (SNAPSHOT_DIR / uri.path).with_suffix(".meta.yaml")

    elif uri.scheme == "table":
        return (TABLE_DIR / f"{uri.path}.parquet").with_suffix(".meta.yaml")

    elif uri.scheme == "artifact":
        return ARTIFACT_DIR / f"{uri.path}.meta.yaml"

    else:
        raise ValueError(f"Unknown scheme {uri.scheme}")
