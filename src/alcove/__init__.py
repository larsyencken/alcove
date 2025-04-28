import argparse
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal

import duckdb
from dotenv import load_dotenv

from alcove import steps
from alcove.core import Alcove
from alcove.exceptions import StepDefinitionError
from alcove.snapshots import Snapshot
from alcove.types import StepURI
from alcove.utils import checksum_manifest, console

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
        "audit", help="Audit the alcove metadata and validate the metadata of every step"
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

    parser.print_help()


def init_alcove_config() -> None:
    """
    Initialize the alcove configuration file (alcove.yaml).
    """
    print("Initializing alcove")
    Alcove.init()


def init_data_files() -> None:
    """
    Initialize the .data-files file and ensure it's referenced in .gitignore.
    """
    from alcove.utils import ensure_data_files_in_gitignore
    ensure_data_files_in_gitignore()


def init_alcove() -> None:
    """
    Initialize alcove with the necessary files and directories.
    Creates the alcove.yaml file, .data-files, and updates .gitignore.
    """
    # Initialize configuration
    init_alcove_config()
    
    # Initialize data files setup
    init_data_files()


def snapshot_to_alcove(
    file_path: Path, dataset_name: str, edit: bool = False, force: bool = False
) -> Snapshot:
    _check_s3_credentials()

    # ensure we are tagging a version on everything
    dataset_name = _maybe_add_version(dataset_name)

    # sanity check that it does not exist
    alcove = Alcove()
    proposed_uri = StepURI("snapshot", dataset_name)
    if proposed_uri in alcove.steps and not force:
        raise ValueError(f"Dataset already exists in alcove: {proposed_uri}")

    existing_metadata = {}
    if proposed_uri in alcove.steps:
        for k, v in Snapshot.load(dataset_name).get_metadata().items():
            if k not in ["checksum", "manifest", "date_accessed"]:
                existing_metadata[k] = v

    # create and add to s3
    print(f"Creating {proposed_uri}")
    snapshot = Snapshot.create(file_path, dataset_name, existing_metadata)

    # ensure that the data itself does not enter git (if not already ignored)
    from alcove.utils import add_to_data_files
    add_to_data_files(snapshot.path)

    if edit:
        subprocess.run(["vim", snapshot.metadata_path])

    alcove.steps[proposed_uri] = []
    alcove.save()

    return snapshot


def list_steps_cmd(alcove: Alcove, regex: str | None = None, paths: bool = False) -> None:
    for step in list_steps(alcove, regex, paths):
        print(step)


def list_steps(
    alcove: Alcove, regex: str | None = None, paths: bool = False
) -> list[Path] | list[StepURI]:
    steps = sorted(alcove.steps)

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
    dag = alcove.steps

    for step, dependencies in dag.items():
        dag[step] = resolve_latest(dependencies, alcove)

    if regex:
        dag = steps.prune_with_regex(dag, regex)

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
        audit_step(step, fix)
        console.print(f"[blue]{'OK':>5}[/blue]   {step}")
    
    # Check .gitignore and .data-files setup
    audit_gitignore_setup(fix)


def check_data_files_exists(fix: bool = False) -> None:
    """
    Check if .data-files exists and create it if it doesn't.
    
    Args:
        fix: Whether to fix issues that are found
    """
    from alcove.utils import print_op
    
    data_files = Path(".data-files")
    
    if not data_files.exists():
        if fix:
            print_op("CREATE", ".data-files")
            data_files.touch()
        else:
            print("WARNING: .data-files doesn't exist")


def check_gitignore_includes_data_files(fix: bool = False) -> None:
    """
    Check if .gitignore includes a reference to .data-files.
    
    Args:
        fix: Whether to fix issues that are found
    """
    from alcove.utils import ensure_data_files_in_gitignore
    
    gitignore = Path(".gitignore")
    
    if gitignore.exists():
        with open(gitignore) as f:
            gitignore_entries = [line.strip() for line in f if line.strip()]
        
        if ".data-files" not in gitignore_entries:
            if fix:
                ensure_data_files_in_gitignore()
            else:
                print("WARNING: .gitignore doesn't include .data-files")


def find_data_patterns_in_gitignore() -> list[str]:
    """
    Find data file patterns in .gitignore that should be in .data-files.
    
    Returns:
        A list of patterns that should be moved to .data-files
    """
    gitignore = Path(".gitignore")
    
    if not gitignore.exists():
        return []
    
    with open(gitignore) as f:
        gitignore_entries = [line.strip() for line in f if line.strip()]
    
    # Look for data file patterns in .gitignore
    data_patterns = []
    for entry in gitignore_entries:
        if entry == ".data-files":
            continue
            
        if entry.startswith("data/snapshots/"):
            data_patterns.append(entry)
    
    return data_patterns


def migrate_data_patterns_to_data_files(fix: bool = False) -> None:
    """
    Move data file patterns from .gitignore to .data-files.
    
    Args:
        fix: Whether to fix issues that are found
    """
    gitignore = Path(".gitignore")
    data_files = Path(".data-files")
    
    # Find data patterns in .gitignore
    data_patterns = find_data_patterns_in_gitignore()
    
    if data_patterns:
        if fix:
            print(f"Moving {len(data_patterns)} entries from .gitignore to .data-files")
            
            # Read existing .data-files content
            if data_files.exists():
                with open(data_files) as f:
                    data_files_entries = set(line.strip() for line in f if line.strip())
            else:
                data_files_entries = set()
            
            # Add new entries to .data-files
            with data_files.open("a") as f:
                for pattern in data_patterns:
                    if pattern not in data_files_entries:
                        print(pattern, file=f)
            
            # Read gitignore entries again to make sure we have the latest
            with open(gitignore) as f:
                gitignore_entries = [line.strip() for line in f if line.strip()]
            
            # Create new contents for .gitignore with data patterns removed
            new_gitignore_entries = [
                ".data-files"  # Always include this
            ]
            
            for entry in gitignore_entries:
                if entry not in data_patterns and entry != ".data-files":
                    new_gitignore_entries.append(entry)
            
            # Write updated .gitignore
            with gitignore.open("w") as f:
                for entry in new_gitignore_entries:
                    print(entry, file=f)
        else:
            print(f"WARNING: Found {len(data_patterns)} data file patterns in .gitignore that should be in .data-files")


def audit_gitignore_setup(fix: bool = False) -> None:
    """
    Audit the .gitignore and .data-files setup.
    
    Checks for:
    - .gitignore includes .data-files
    - No data file patterns in .gitignore that should be in .data-files
    
    Args:
        fix: Whether to fix issues that are found
    """
    # Check if .data-files exists
    check_data_files_exists(fix)
    
    # Check if .gitignore includes .data-files
    check_gitignore_includes_data_files(fix)
    
    # Move data patterns from .gitignore to .data-files
    migrate_data_patterns_to_data_files(fix)


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
    table_uri = StepURI("table", table_path)
    if table_uri in alcove.steps:
        raise ValueError(f"Table already exists in alcove: {table_uri}")

    alcove.steps[table_uri] = [StepURI.parse(dep) for dep in dependencies]
    alcove.save()


def execute_query(
    alcove: Alcove,
    query: str,
    names: Literal["short", "full", "both"] = "both",
    csv: bool = False,
) -> None:
    tables = _get_tables(alcove)

    # Create temporary views
    conn = duckdb.connect(":memory:")
    for path in tables:
        table_name = _path_to_snake(path)
        table_path = (Path("data/tables") / path).with_suffix(".parquet")
        conn.execute(
            f"CREATE VIEW {table_name} AS SELECT * FROM read_parquet('{table_path}')"
        )

    if names == "both":
        for alias, table_name in _table_aliases(tables):
            conn.execute(f'CREATE VIEW "{alias}" AS SELECT * FROM "{table_name}"')

    elif names == "short":
        for alias, table_name in _table_aliases(tables):
            conn.execute(f'ALTER VIEW "{table_name}" RENAME TO "{alias}"')

    if query.count(" ") == 0:
        # this is a full-table extraction
        query = f'SELECT * FROM "{query}"'

    result = conn.execute(query).fetchdf()

    if csv:
        print(result.to_csv(index=False))
    else:
        print(result.to_json(orient="records"))

    conn.close()


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


def _path_to_snake(path: str) -> str:
    return path.replace("/", "_").replace("-", "").rsplit(".", 1)[0]


def _get_tables(alcove: Alcove) -> list[str]:
    tables = []
    for step in alcove.steps:
        if step.scheme == "table":
            tables.append(step.path)

    return tables


def _table_aliases(tables: list[str]) -> list[tuple[str, str]]:
    # map potential aliases to table names
    potential_aliases: dict[str, set[str]] = defaultdict(set)
    for path in tables:
        parts = path.split("/")

        # Generate all possible suffixes without version and with version
        for i in range(len(parts) - 1):
            # Suffix without version
            no_version = "/".join(parts[i:-1])
            if no_version:
                potential_aliases[no_version].add(path)

            # Suffix with version
            with_version = "/".join(parts[i:])
            if with_version != path:
                potential_aliases[with_version].add(path)

    # only keep unique ones
    best_alias: dict[str, str] = {}
    for alias, paths in potential_aliases.items():
        if len(paths) == 1:
            # this is a potentially unique alias
            (path,) = paths
            table_alias = _path_to_snake(alias)
            table_name = _path_to_snake(path)

            best_alias[table_name] = _better_alias(
                table_alias, best_alias.get(table_name)
            )

    return [(table_alias, table_name) for table_name, table_alias in best_alias.items()]


def _better_alias(a: str, b: str | None) -> str:
    if not b:
        return a

    return min([(_has_version(a), len(a), a), (_has_version(b), len(b), b)])[-1]


def _has_version(name: str) -> bool:
    return bool(re.match(r".*_((d{4}-\d{2}-\d{2})|latest)$", name))


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
