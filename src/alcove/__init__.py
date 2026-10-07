import argparse
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

import duckdb
from dotenv import load_dotenv

from alcove import steps
from alcove.core import Alcove
from alcove.wildcards import expand_wildcards
from alcove.db import (
    AlcoveDB as AlcoveDB,
    connect as connect,
    _get_tables,
    _path_to_snake,
    _table_aliases,
    _better_alias,
)
from alcove.exceptions import StepDefinitionError
from alcove.partitions import (
    ORPHANED_DIR,
    check_contiguous,
    is_partition_version,
    partition_gitignore_entry,
    tidy_orphans,
)
from alcove.snapshots import Snapshot
from alcove.types import StepURI
from alcove.utils import DATA_IGNORES, checksum_manifest, console

load_dotenv()


BLACKLIST = [".DS_Store"]


def main():
    parser = argparse.ArgumentParser(
        description="Add a data file or directory in a content-addressable way to the S3-compatible store."
    )
    subparsers = parser.add_subparsers(dest="command")

    snapshot_parser = subparsers.add_parser(
        "snapshot", help="Add a data file or directory to the content store"
    )
    snapshot_parser.add_argument(
        "file_path", type=str, help="Path to the data file or directory"
    )
    snapshot_parser.add_argument(
        "dataset_name",
        type=str,
        help="Dataset name as a relative path of arbitrary size",
    )
    snapshot_parser.add_argument(
        "--edit",
        action="store_true",
        help="Edit the metadata file in an interactive editor.",
    )
    snapshot_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite an existing snapshot with the same name",
    )

    run_parser = subparsers.add_parser(
        "run", help="Execute any outstanding steps in the DAG"
    )
    run_parser.add_argument(
        "path",
        type=str,
        nargs="?",
        help="Optional regex to match against step names",
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-build of steps",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't execute, just print the steps that would be executed",
    )

    list_parser = subparsers.add_parser(
        "list", help="List all datasets in alphabetical order"
    )
    list_parser.add_argument(
        "regex",
        type=str,
        nargs="?",
        help="Optional regex to filter dataset names",
    )
    list_parser.add_argument(
        "--paths",
        action="store_true",
        help="Return relative paths instead of URIs",
    )

    subparsers.add_parser(
        "init", help="Initialize the alcove with the necessary directories"
    )

    audit_parser = subparsers.add_parser(
        "audit",
        help="Audit the alcove metadata and validate the metadata of every step",
    )
    audit_parser.add_argument(
        "--fix",
        action="store_true",
        help="Fix the overall checksum for snapshot steps with snapshot_type of directory if it is wrong",
    )

    export_parser = subparsers.add_parser(
        "export-duckdb", help="Export tables to a DuckDB file"
    )
    export_parser.add_argument(
        "db_file", type=str, help="Path to the DuckDB file to export tables to"
    )
    export_parser.add_argument(
        "--short", action="store_true", help="Use minimal aliases for table names"
    )

    new_table_parser = subparsers.add_parser(
        "new-table", help="Create a new table with optional dependencies"
    )
    new_table_parser.add_argument("table_path", type=str, help="Path to the new table")
    new_table_parser.add_argument(
        "dependencies", type=str, nargs="*", help="Optional dependencies for the table"
    )
    new_table_parser.add_argument(
        "--edit",
        action="store_true",
        help="Edit the metadata file in an interactive editor.",
    )

    new_artifact_parser = subparsers.add_parser(
        "new-artifact",
        help="Create a new artifact step, for outputs that are not tables",
    )
    new_artifact_parser.add_argument(
        "artifact_path", type=str, help="Path to the new artifact"
    )
    new_artifact_parser.add_argument(
        "dependencies",
        type=str,
        nargs="*",
        help="Optional dependencies for the artifact",
    )

    db_parser = subparsers.add_parser(
        "db", help="Enter a DuckDB shell or execute a query"
    )
    db_parser.add_argument(
        "query",
        nargs="?",
        help="SQL query to execute (if not provided, enters interactive shell)",
    )
    db_parser.add_argument(
        "--names",
        action="store",
        default="both",
        help="What kind of names to use for tables (short|full|[both])",
    )
    db_parser.add_argument(
        "--csv",
        action="store_true",
        help="Output results in CSV format instead of JSON",
    )

    args = parser.parse_args()

    if args.command == "init":
        return init_alcove()

    alcove = Alcove()

    if args.command == "snapshot":
        snapshot_to_alcove(
            Path(args.file_path), args.dataset_name, edit=args.edit, force=args.force
        )
        return

    elif args.command == "list":
        return list_steps_cmd(alcove, args.regex, args.paths)

    elif args.command == "run":
        return plan_and_run(alcove, args.path, args.force, args.dry_run)

    elif args.command == "audit":
        return audit_alcove(alcove, args.fix)

    elif args.command == "export-duckdb":
        return export_duckdb(alcove, args.db_file, args.short)

    elif args.command == "db":
        if args.query:
            return execute_query(alcove, args.query, names=args.names, csv=args.csv)
        return duckdb_shell(alcove, names=args.names)

    elif args.command == "new-table":
        return new_table(alcove, args.table_path, args.dependencies, args.edit)

    elif args.command == "new-artifact":
        return alcove.new_artifact(args.artifact_path, args.dependencies)

    parser.print_help()


def init_alcove_config() -> None:
    """
    Initialize the alcove configuration file (alcove.yaml).
    """
    print("Initializing alcove")
    Alcove.init()


def init_data_files() -> None:
    """
    Initialize data/.gitignore to handle data files that should not be tracked.
    """
    from alcove.utils import ensure_data_gitignore

    ensure_data_gitignore()


def init_alcove() -> None:
    """
    Initialize alcove with the necessary files and directories.
    Creates the alcove.yaml file and data/.gitignore for ignoring data files.
    """
    # Initialize configuration
    init_alcove_config()

    # Initialize data files setup
    init_data_files()


def snapshot_to_alcove(
    file_path: Path, dataset_name: str, edit: bool = False, force: bool = False
) -> Snapshot:
    _check_s3_credentials()

    alcove = Alcove()

    # a partition must be named by its date before any default version is
    # appended, or `foo/2026-1-5` would become a new dataset `foo/2026-1-5/<today>`
    dataset_name = dataset_name.strip("/")
    if alcove.is_partitioned(StepURI("snapshot", f"{dataset_name}/*")):
        raise ValueError(
            f"snapshot://{dataset_name}/* is partitioned by date, so name the "
            f"partition this data covers, e.g. {dataset_name}/YYYY-MM-DD"
        )

    # versions listed in alcove.yaml (e.g. from before the dataset was
    # declared partitioned) are not partitions, and keep their old behaviour
    listed = set(alcove.steps) - alcove.discovered

    candidate = StepURI("snapshot", dataset_name)
    if (
        alcove.is_partitioned(candidate)
        and candidate not in listed
        and not is_partition_version(candidate.version)
    ):
        raise ValueError(
            f"{candidate} is a partition of snapshot://{candidate.base_path}/*, "
            "so its version must be an ISO date (YYYY-MM-DD). To snapshot a "
            "dataset nested below it, give that dataset's version too, e.g. "
            f"{dataset_name}/YYYY-MM-DD"
        )

    # ensure we are tagging a version on everything
    dataset_name = _maybe_add_version(dataset_name)

    # sanity check that it does not exist
    proposed_uri = StepURI("snapshot", dataset_name)
    partitioned = alcove.is_partitioned(proposed_uri) and proposed_uri not in listed

    if proposed_uri in alcove.steps and not force:
        raise ValueError(f"Dataset already exists in alcove: {proposed_uri}")

    # keep the descriptive metadata of the snapshot we are replacing or, for a
    # new partition, of the dataset's most recent partition
    metadata_source = None
    if proposed_uri in alcove.steps:
        metadata_source = proposed_uri
    elif partitioned and alcove.versions(proposed_uri):
        metadata_source = proposed_uri.with_version(alcove.versions(proposed_uri)[-1])

    existing_metadata = {}
    if metadata_source:
        for k, v in Snapshot.load(metadata_source.path).get_metadata().items():
            if k not in ["checksum", "manifest", "date_accessed"]:
                existing_metadata[k] = v

    if partitioned:
        # a partition is named by the date its data covers, so record
        # separately when it was fetched
        existing_metadata["date_accessed"] = datetime.today().strftime("%Y-%m-%d")

    # create and add to s3
    print(f"Creating {proposed_uri}")
    snapshot = Snapshot.create(file_path, dataset_name, existing_metadata)

    # ensure that the data itself does not enter git (if not already ignored)
    from alcove.utils import add_pattern_to_data_gitignore, add_to_data_gitignore

    if partitioned:
        # one pattern covers every partition of the dataset
        extension = snapshot.extension if snapshot.snapshot_type == "file" else None
        add_pattern_to_data_gitignore(
            partition_gitignore_entry(proposed_uri.base_path, extension)
        )
    else:
        add_to_data_gitignore(snapshot.path)

    if edit:
        subprocess.run(["vim", snapshot.metadata_path])

    if not partitioned:
        # partitions are discovered from their metadata, not listed in alcove.yaml
        alcove.steps[proposed_uri] = []
        alcove.save()

    return snapshot


def list_steps_cmd(
    alcove: Alcove, regex: str | None = None, paths: bool = False
) -> None:
    for step in list_steps(alcove, regex, paths):
        print(step)


def list_steps(
    alcove: Alcove, regex: str | None = None, paths: bool = False
) -> list[Path] | list[StepURI]:
    steps = sorted(s for s in alcove.steps if not s.is_wildcard)

    if regex:
        steps = [s for s in steps if re.search(regex, str(s))]

    if paths:
        steps = [s.rel_path for s in steps]

    return steps


def plan_and_run(
    alcove: Alcove,
    regex: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    # to help unit testing
    alcove.refresh()

    # XXX in the future, we could create a Plan object that explains why each step has
    #     been selected to be run, even down to the level of which checksums are out of
    #     date or which files are missing
    dag = dict(alcove.steps)

    for step, dependencies in dag.items():
        dag[step] = resolve_latest(dependencies, alcove)

    dag, _ = expand_wildcards(dag)
    expanded = dag

    if regex:
        dag = steps.prune_with_regex(dag, regex)

    # a day missing from the middle of a dataset would otherwise go
    # unnoticed; stop before anything on disk is touched
    check_contiguous(alcove.steps, scope=dag if regex else None)

    # files left by a dropped version would otherwise still match their
    # dataset's glob, here and on every other clone
    tidy_orphans(alcove.steps, expanded, scope=dag if regex else None, dry_run=dry_run)

    if not force:
        dag = steps.prune_completed(dag)

    if not dag:
        print("Already up to date!")
        return

    steps.execute_dag(dag, dry_run=dry_run)


def resolve_latest(dependencies: list[StepURI], alcove: Alcove) -> list[StepURI]:
    resolved = []
    for dep in dependencies:
        if dep.path.endswith("latest"):
            latest_version = alcove.get_latest_version(dep)
            resolved.append(latest_version)
        else:
            resolved.append(dep)

    return resolved


def export_duckdb(alcove: Alcove, db_file: str, short: bool = False) -> None:
    # Ensure all tables are built
    plan_and_run(alcove)

    # Connect to DuckDB
    conn = duckdb.connect(db_file)

    tables = _get_tables(alcove)
    for table in tables:
        if StepURI("table", table).is_wildcard:
            continue
        table_name = table.replace("/", "_").replace("-", "").rsplit(".", 1)[0]
        table_path = (Path("data/tables") / table).with_suffix(".parquet")

        conn.execute(
            f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM read_parquet('{table_path}')"
        )

    if short:
        best_alias = {}
        for alias, table_name in _table_aliases(tables):
            best_alias[table_name] = _better_alias(
                alias, best_alias.get(table_name, table_name)
            )

        for table_name, alias in best_alias.items():
            conn.execute(f'DROP TABLE IF EXISTS "{alias}"')
            conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{alias}"')

    conn.close()


def audit_alcove(alcove: Alcove, fix: bool = False) -> None:
    """
    Audit the alcove repository for problems and inconsistencies.
    Can optionally fix issues that are found.

    Checks for:
    - Manifest checksum correctness
    - .gitignore includes .data-files reference
    - Data files are properly tracked in .data-files instead of .gitignore

    Args:
        alcove: The Alcove instance to audit
        fix: Whether to fix issues that are found
    """
    # XXX in the future, we could automatically upgrade from one alcove format
    #     version to another, if there were breaking changes
    print(f"Auditing {len(alcove.steps)} steps")

    # Check all steps
    for step in alcove.steps:
        if step.is_wildcard:
            continue
        audit_step(step, fix)
        console.print(f"[blue]{'OK':>5}[/blue]   {step}")

    for metadata_file in unreachable_snapshot_metadata(alcove):
        print(
            f"WARNING: {metadata_file} is not a step of this alcove: neither "
            "listed in alcove.yaml nor a date-named partition of a dataset "
            "declared there as `snapshot://<dataset>/*`"
        )

    # Check .gitignore and .data-files setup
    audit_gitignore_setup(fix)


def check_data_gitignore_exists(fix: bool = False) -> None:
    """
    Check if data/.gitignore exists and create it if it doesn't.

    Args:
        fix: Whether to fix issues that are found
    """
    from alcove.utils import print_op

    data_dir = Path("data")
    gitignore_path = data_dir / ".gitignore"

    # Make sure data directory exists
    if not data_dir.exists():
        if fix:
            print_op("CREATE", "data/")
            data_dir.mkdir(parents=True, exist_ok=True)
        else:
            print("WARNING: data/ directory doesn't exist")
            return

    if not gitignore_path.exists():
        if fix:
            print_op("CREATE", "data/.gitignore")
            # Always ignore build products in data/.gitignore
            gitignore_path.write_text("".join(f"{e}\n" for e in DATA_IGNORES))
        else:
            print("WARNING: data/.gitignore doesn't exist")


def ensure_data_dir_in_gitignore(fix: bool = False) -> None:
    """
    We no longer need to include a reference to .data-files in .gitignore.
    This is a no-op method retained for backward compatibility.

    Args:
        fix: Whether to fix issues that are found
    """
    # This is intentionally empty - we don't need to do this anymore


def find_data_patterns_in_gitignore() -> list[str]:
    """
    Find data file patterns in .gitignore that should be in data/.gitignore.

    Returns:
        A list of patterns that should be moved to data/.gitignore
    """
    gitignore = Path(".gitignore")

    if not gitignore.exists():
        return []

    with open(gitignore) as f:
        gitignore_entries = [line.strip() for line in f if line.strip()]

    # Look for data file patterns in .gitignore
    data_patterns = []
    for entry in gitignore_entries:
        if entry.startswith("data/"):
            data_patterns.append(entry)

    return data_patterns


def migrate_data_patterns_to_data_gitignore(fix: bool = False) -> None:
    """
    Move data file patterns from .gitignore to data/.gitignore.

    Args:
        fix: Whether to fix issues that are found
    """
    from alcove.utils import print_op

    gitignore = Path(".gitignore")
    data_dir = Path("data")

    # Find data patterns in .gitignore
    data_patterns = find_data_patterns_in_gitignore()

    if data_patterns:
        if fix:
            print(
                f"Moving {len(data_patterns)} entries from .gitignore to data/.gitignore"
            )

            # Create data directory if it doesn't exist
            if not data_dir.exists():
                data_dir.mkdir(parents=True, exist_ok=True)
                print_op("CREATE", "data/")

            # Check for existing data/.gitignore
            data_gitignore = data_dir / ".gitignore"
            if data_gitignore.exists():
                with open(data_gitignore) as f:
                    data_gitignore_entries = set(
                        line.strip() for line in f if line.strip()
                    )
            else:
                data_gitignore_entries = set(DATA_IGNORES)

            # First get list of all entries, preserving previous entries
            all_entries = set(DATA_IGNORES)
            all_entries.update(data_gitignore_entries)

            # Add migrated entries from .gitignore
            for pattern in data_patterns:
                if pattern.startswith("data/"):
                    all_entries.add(pattern[5:])  # Remove "data/" prefix

            # Add new entries to data/.gitignore, removing the "data/" prefix
            with data_gitignore.open("w") as f:
                # Write all entries in sorted order
                for entry in sorted(all_entries):
                    print(entry, file=f)

            # Read gitignore entries again to make sure we have the latest
            with open(gitignore) as f:
                gitignore_entries = [line.strip() for line in f if line.strip()]

            # Create new contents for .gitignore with data patterns removed
            new_gitignore_entries = []

            for entry in gitignore_entries:
                if entry not in data_patterns and entry != ".data-files":
                    new_gitignore_entries.append(entry)

            # Write updated .gitignore
            with gitignore.open("w") as f:
                for entry in new_gitignore_entries:
                    print(entry, file=f)
        else:
            print(
                f"WARNING: Found {len(data_patterns)} data file patterns in .gitignore that should be in data/.gitignore"
            )


def audit_gitignore_setup(fix: bool = False) -> None:
    """
    Audit the .gitignore and data/.gitignore setup.

    Checks for:
    - data/.gitignore exists and includes tables/
    - No data file patterns in .gitignore that should be in data/.gitignore

    Args:
        fix: Whether to fix issues that are found
    """
    from alcove.utils import print_op

    # Check if data/.gitignore exists
    check_data_gitignore_exists(fix)

    # Move data patterns from .gitignore to data/.gitignore
    migrate_data_patterns_to_data_gitignore(fix)

    # If .data-files exists, also migrate its contents to data/.gitignore
    old_data_files = Path(".data-files")
    if old_data_files.exists():
        if fix:
            print("Migrating .data-files to data/.gitignore")
            data_dir = Path("data")
            data_gitignore = data_dir / ".gitignore"

            # Create data directory if it doesn't exist
            if not data_dir.exists():
                data_dir.mkdir(parents=True, exist_ok=True)
                print_op("CREATE", "data/")

            # Read existing data/.gitignore content
            if data_gitignore.exists():
                with open(data_gitignore) as f:
                    data_gitignore_entries = set(
                        line.strip() for line in f if line.strip()
                    )
            else:
                data_gitignore_entries = set(DATA_IGNORES)

            # Read entries from .data-files
            with open(old_data_files) as f:
                data_files_entries = [line.strip() for line in f if line.strip()]

            # First get list of all entries, preserving previous entries too
            all_entries = set(DATA_IGNORES)
            all_entries.update(data_gitignore_entries)

            # Add migrated entries from .data-files
            for entry in data_files_entries:
                if entry.startswith("data/"):
                    all_entries.add(entry[5:])  # Remove "data/" prefix
                else:
                    all_entries.add(entry)

            # Write data/.gitignore with all entries
            with data_gitignore.open("w") as f:
                # Write all entries in sorted order
                for entry in sorted(all_entries):
                    print(entry, file=f)

            # Remove the old .data-files file
            old_data_files.unlink()
            print_op("REMOVE", ".data-files")
        else:
            print(
                "WARNING: .data-files exists and should be migrated to data/.gitignore"
            )


def unreachable_snapshot_metadata(alcove: Alcove) -> list[Path]:
    "Snapshot metadata files on disk that no step of the alcove refers to."
    snapshot_dir = Path("data") / "snapshots"
    unreachable = []
    for metadata_file in sorted(snapshot_dir.rglob("*.meta.yaml")):
        parts = metadata_file.relative_to(snapshot_dir).parts
        step = StepURI("snapshot", "/".join(parts).removesuffix(".meta.yaml"))
        if step in alcove.steps:
            continue

        # a file inside a directory snapshot's data is not metadata, nor is
        # one inside data set aside from a partitioned dataset
        inside_snapshot = ORPHANED_DIR in parts or any(
            StepURI("snapshot", "/".join(parts[:i])) in alcove.steps
            for i in range(1, len(parts))
        )
        if not inside_snapshot:
            unreachable.append(metadata_file)

    return unreachable


def audit_step(step: StepURI, fix: bool = False) -> None:
    if step.scheme != "snapshot":
        return

    snapshot = Snapshot.load(step.path)
    if snapshot.snapshot_type != "directory":
        return

    manifest = snapshot.manifest
    if not manifest:
        raise StepDefinitionError(
            f"Snapshot {step} of type 'directory' is missing a manifest"
        )

    calculated_checksum = checksum_manifest(manifest)
    if calculated_checksum != snapshot.checksum:
        print(
            f"Checksum mismatch for {step}: {snapshot.checksum} != {calculated_checksum}"
        )
        if fix:
            print(f"Fixing checksum for {step}")
            snapshot.checksum = calculated_checksum
            snapshot.save()
        else:
            raise StepDefinitionError(
                f"Checksum mismatch for {step} of type 'directory'"
            )


def new_table(
    alcove: Alcove, table_path: str, dependencies: list[str], edit: bool = False
) -> None:
    alcove.new_table(table_path, dependencies)


def execute_query(
    alcove: Alcove,
    query: str,
    names: Literal["short", "full", "both"] = "both",
    csv: bool = False,
) -> None:
    with AlcoveDB(alcove=alcove, names=names) as db:
        result = db.sql(query).to_pandas()

        if csv:
            print(result.to_csv(index=False))
        else:
            print(result.to_json(orient="records"))


def duckdb_shell(alcove: Alcove, names: str = "both") -> None:
    if names not in ("both", "short", "full"):
        raise ValueError("Names parameter must be one of 'short', 'full' or 'both'")

    tables = _get_tables(alcove)

    sql_parts: list[str] = []
    for path in tables:
        table_name = _path_to_snake(path)
        table_path = (Path("data/tables") / path).with_suffix(".parquet")

        sql_parts.append(
            f"CREATE OR REPLACE VIEW {table_name} AS\nSELECT * FROM read_parquet('{table_path}');"
        )

    if names != "full":
        for alias, table_name in _table_aliases(tables):
            if names == "short":
                sql_parts.append(f'ALTER VIEW "{table_name}" RENAME TO "{alias}";')
            elif names == "both":
                sql_parts.append(
                    f'CREATE OR REPLACE VIEW "{alias}" AS\nSELECT * FROM {table_name};'
                )

    sql = "\n\n".join(sql_parts)
    with tempfile.NamedTemporaryFile("w", suffix=".sql") as f:
        f.write(sql)
        f.flush()
        subprocess.run(f'duckdb -cmd ".read {f.name}"', shell=True)


def _maybe_add_version(dataset_name: str) -> str:
    parts = dataset_name.split("/")

    if _is_valid_version(parts[-1]):
        if len(parts) == 1:
            raise Exception("invalid dataset name")

        # the final segment is a version, all good
        return dataset_name

    # add a version to the end
    parts.append(datetime.today().strftime("%Y-%m-%d"))

    return "/".join(parts)


def _is_valid_version(version: str) -> bool:
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", version)) or version == "latest"


def _check_s3_credentials() -> None:
    for key in [
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_ENDPOINT_URL",
        "S3_BUCKET_NAME",
    ]:
        if key not in os.environ:
            raise ValueError(f"Missing S3 credentials -- please set {key} in .env")
