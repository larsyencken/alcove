# table_metadata.py

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import polars as pl
from rich.console import Console

from alcove.exceptions import ValidationError
from alcove.paths import SNAPSHOT_DIR, TABLE_DIR, TABLE_SCRIPT_DIR
from alcove.schemas import TABLE_CONFIG_SCHEMA
from alcove.types import StepURI
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

        # Add the script we used to generate the table
        executable = _get_executable(self.uri)
        manifest[str(executable)] = checksum_file(executable)

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

    # Generate and save final metadata
    final_metadata = metadata.generate(output_path, dependencies)
    save_yaml(final_metadata, _metadata_path(uri))


def _get_executable(uri: StepURI, check: bool = True) -> Path:
    base = TABLE_SCRIPT_DIR / uri.path

    for exec_base in [base, base.parent]:
        py_script = exec_base.with_suffix(".py")
        sql_script = exec_base.with_suffix(".sql")

        if py_script.exists():
            if check and not _is_valid_script(py_script):
                raise Exception(f"Missing execute permissions on {py_script}")

            return py_script

        elif sql_script.exists():
            return sql_script

    else:
        raise FileNotFoundError(f"Could not find script for {uri}")


def _is_valid_script(script: Path) -> bool:
    return script.is_file() and os.access(script, os.X_OK)


def _metadata_path(uri: StepURI) -> Path:
    if uri.scheme == "snapshot":
        return (SNAPSHOT_DIR / uri.path).with_suffix(".meta.yaml")

    elif uri.scheme == "table":
        return (TABLE_DIR / f"{uri.path}.parquet").with_suffix(".meta.yaml")

    else:
        raise ValueError(f"Unknown scheme {uri.scheme}")
