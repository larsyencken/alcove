import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import duckdb
import polars as pl
from dotenv import find_dotenv, load_dotenv

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
from alcove.exceptions import StepDefinitionError, StepScriptError
from alcove.paths import SNAPSHOT_DIR
from alcove.provenance import (
    CommandNotFound,
    describe_caller,
    redact,
    run_and_capture,
    utc_now,
)
from alcove.rename import rename_dataset
from alcove.partitions import (
    ORPHANED_DIR,
    check_contiguous,
    compact_steps,
    is_partition_version,
    partition_gitignore_entry,
    tidy_orphans,
)
from alcove.schemas import check_step_uri
from alcove.snapshots import DEFAULT_FETCH_JOBS, Snapshot
from alcove.storage import (
    BACKENDS,
    DEFAULT_LOCAL_STORE,
    GitStore,
    StorageConfig,
    StorageError,
)
from alcove.templates import LANGUAGES, Language, check_no_script, write_template
from alcove.types import StepURI
from alcove.utils import DATA_IGNORES, checksum_manifest, console, print_op

# the .env beside alcove.yaml, wherever alcove itself is installed
load_dotenv(find_dotenv(usecwd=True))


BLACKLIST = [".DS_Store"]


def _exit_for_failed_script(parser: argparse.ArgumentParser, e: StepScriptError):
    "Exit as the failed step script did, without a traceback through alcove."
    if e.returncode > 0:
        # the script already printed its own error; a traceback would bury it
        parser.exit(e.returncode)

    # killed by a signal, e.g. for running out of memory, so the script
    # couldn't say what went wrong
    try:
        name = signal.Signals(-e.returncode).name
    except ValueError:
        name = f"signal {-e.returncode}"
    parser.exit(
        128 - e.returncode, f"alcove: error: the step's script was killed by {name}\n"
    )


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

    capture_parser = subparsers.add_parser(
        "capture",
        help="Run a command and snapshot its output, recording how it was made",
        description="Run a command and snapshot its output, recording the "
        "command, when it ran and how it exited in the snapshot's produced_by "
        "metadata. Put the command after --; it runs without a shell, so for "
        "a pipeline use: -- sh -c '...'",
    )
    capture_parser.add_argument(
        "dataset_name",
        type=str,
        help="Dataset to snapshot into, e.g. prime_001/k3s_log",
    )
    capture_parser.add_argument(
        # not `command`, which names the subcommand
        "captured_command",
        metavar="command",
        nargs="*",
        help="The command to run, after --",
    )
    capture_parser.add_argument(
        "--version",
        help="Version to snapshot as (default: the UTC time, YYYY-MM-DD-HHMM, "
        "or the UTC date for a partitioned dataset)",
    )
    capture_parser.add_argument(
        "--note", help="A note to keep with the record of how it was made"
    )
    capture_parser.add_argument(
        "--dir",
        action="store_true",
        help="Snapshot the files the command writes into the empty directory "
        "it runs in, rather than its stdout",
    )
    capture_parser.add_argument(
        "--ext",
        default=".txt",
        help="Extension for a captured stdout (default: .txt)",
    )
    capture_parser.add_argument(
        "--keep-failed",
        action="store_true",
        help="Snapshot the output even if the command fails",
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
    run_parser.add_argument(
        "--jobs",
        "-j",
        type=positive_int,
        default=DEFAULT_FETCH_JOBS,
        help="How many snapshot files to download at once "
        f"(default: {DEFAULT_FETCH_JOBS})",
    )

    list_parser = subparsers.add_parser(
        "list",
        help="List all datasets, each after the datasets it depends on",
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
        help="Return relative paths instead of URIs (implies --full)",
    )
    list_parser.add_argument(
        "--full",
        action="store_true",
        help="List every version, rather than collapsing consecutive days "
        "into one line",
    )
    list_parser.add_argument(
        "--alphabetical",
        action="store_true",
        help="List in alphabetical order rather than dependency order",
    )

    init_parser = subparsers.add_parser(
        "init", help="Initialize the alcove with the necessary directories"
    )
    init_parser.add_argument(
        "--storage",
        choices=BACKENDS,
        help="Where to keep snapshot data: an S3-compatible bucket (the default), "
        "a local directory, or this git repo, under refs/alcove/blobs",
    )
    init_parser.add_argument(
        "--path",
        help=f"For local storage, the directory to keep data in (default: {DEFAULT_LOCAL_STORE})",
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
        "-l",
        "--lang",
        required=True,
        choices=list(LANGUAGES),
        help="The language of the starter script to write",
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

    rename_parser = subparsers.add_parser(
        "rename",
        help="Rename every version of a snapshot, table or artifact, and "
        "update what depends on it",
    )
    rename_parser.add_argument(
        "old", type=str, help="The dataset to rename, e.g. table://spend/daily"
    )
    rename_parser.add_argument(
        "new", type=str, help="Its new name, e.g. table://spend/user_days"
    )
    rename_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change, without changing anything",
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
        if args.path and args.storage != "local":
            parser.error("--path only applies to --storage local")
        storage = None
        if args.storage:
            storage = StorageConfig(backend=args.storage, path=args.path)
        try:
            return init_alcove(storage)
        except (StorageError, ValueError) as e:
            parser.exit(1, f"alcove: error: {e}\n")

    alcove = Alcove()

    if args.command == "snapshot":
        snapshot_to_alcove(
            Path(args.file_path), args.dataset_name, edit=args.edit, force=args.force
        )
        return

    elif args.command == "capture":
        try:
            capture_to_alcove(
                args.dataset_name,
                args.captured_command,
                version=args.version,
                note=args.note,
                directory=args.dir,
                extension=args.ext,
                keep_failed=args.keep_failed,
            )
        except CommandNotFound as e:
            parser.exit(127, f"alcove: error: command not found: {e}\n")
        except KeyboardInterrupt:
            parser.exit(130, "alcove: interrupted\n")
        except CaptureFailed as e:
            # a command killed by signal N exits 128+N, as from a shell
            code = e.returncode if e.returncode > 0 else 128 - e.returncode
            if e.snapshot is None:
                parser.exit(
                    code,
                    f"alcove: error: the command exited with {e.returncode}, so "
                    "nothing was snapshotted; use --keep-failed to keep its output\n",
                )
            parser.exit(
                code,
                f"alcove: the command exited with {e.returncode}; snapshotted "
                f"its output anyway as {e.snapshot.uri}\n",
            )
        except ValueError as e:
            parser.exit(1, f"alcove: error: {e}\n")
        return

    elif args.command == "list":
        return list_steps_cmd(
            alcove, args.regex, args.paths, args.full, args.alphabetical
        )

    elif args.command == "run":
        try:
            return plan_and_run(alcove, args.path, args.force, args.dry_run, args.jobs)
        except StepScriptError as e:
            _exit_for_failed_script(parser, e)

    elif args.command == "audit":
        return audit_alcove(alcove, args.fix)

    elif args.command == "export-duckdb":
        try:
            return export_duckdb(alcove, args.db_file, args.short)
        except StepScriptError as e:
            _exit_for_failed_script(parser, e)

    elif args.command == "db":
        if args.query:
            return execute_query(alcove, args.query, names=args.names, csv=args.csv)
        return duckdb_shell(alcove, names=args.names)

    elif args.command == "new-table":
        return new_table(alcove, args.table_path, args.dependencies, args.lang)

    elif args.command == "new-artifact":
        return alcove.new_artifact(args.artifact_path, args.dependencies)

    elif args.command == "rename":
        try:
            return rename_dataset(alcove, args.old, args.new, dry_run=args.dry_run)
        except (ValueError, FileNotFoundError) as e:
            parser.exit(1, f"alcove: error: {e}\n")

    parser.print_help()


def init_alcove_config(storage: StorageConfig | None = None) -> None:
    """
    Initialize the alcove configuration file (alcove.yaml).
    """
    print("Initializing alcove")
    if storage is not None and storage.backend == "git":
        GitStore().check()
    Alcove.init(storage=storage)
    if storage is not None and storage.backend == "local" and storage.path is None:
        from alcove.utils import add_entry_to_file

        add_entry_to_file(Path(".gitignore"), f"/{DEFAULT_LOCAL_STORE.parts[0]}/")


def init_data_files() -> None:
    """
    Initialize data/.gitignore to handle data files that should not be tracked.
    """
    from alcove.utils import ensure_data_gitignore

    ensure_data_gitignore()


def init_alcove(storage: StorageConfig | None = None) -> None:
    """
    Initialize alcove with the necessary files and directories.
    Creates the alcove.yaml file and data/.gitignore for ignoring data files.
    """
    # Initialize configuration
    init_alcove_config(storage)

    # Initialize data files setup
    init_data_files()


def snapshot_to_alcove(
    file_path: Path,
    dataset_name: str,
    edit: bool = False,
    force: bool = False,
    produced_by: dict[str, Any] | None = None,
) -> Snapshot:
    """Add a file or folder to the alcove as a new snapshot version.

    Its `produced_by` metadata records how it was made: the calling script (if
    any) and its arguments, the commit it ran at and when, plus any keys given
    in `produced_by`.
    """
    alcove = Alcove()
    if alcove.storage.backend == "s3":
        _check_s3_credentials()

    proposed_uri, partitioned = _name_snapshot(alcove, dataset_name, force)
    dataset_name = proposed_uri.path

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

    # unlike the metadata above, this describes only the version being made
    existing_metadata["produced_by"] = describe_caller(produced_by)

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


def _name_snapshot(
    alcove: Alcove, dataset_name: str, force: bool = False
) -> tuple[StepURI, bool]:
    """The snapshot a new version of `dataset_name` would be, and whether it is
    a partition. Raises ValueError for a name alcove would refuse."""
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
    # check before uploading anything, so tables can depend on this snapshot
    check_step_uri(str(proposed_uri))
    partitioned = alcove.is_partitioned(proposed_uri) and proposed_uri not in listed

    if proposed_uri in alcove.steps and not force:
        raise ValueError(f"Dataset already exists in alcove: {proposed_uri}")

    return proposed_uri, partitioned


class CaptureFailed(Exception):
    "The captured command exited with an error."

    def __init__(self, returncode: int, snapshot: Optional[Snapshot]) -> None:
        super().__init__(f"the command exited with {returncode}")
        self.returncode = returncode
        self.snapshot = snapshot


def capture_to_alcove(
    dataset_name: str,
    command: list[str],
    version: Optional[str] = None,
    note: Optional[str] = None,
    directory: bool = False,
    extension: str = ".txt",
    keep_failed: bool = False,
) -> Snapshot:
    """Run a command and snapshot what it produces, recording how in `produced_by`.

    The snapshot is its stdout, or, with `directory`, the files it writes into
    the empty directory it runs in. Without a version in `dataset_name` or
    `version`, the version is the UTC time it ran (YYYY-MM-DD-HHMM), or the
    UTC date for a partitioned dataset.

    If the command fails, raises CaptureFailed. Its output is snapshotted
    anyway only with `keep_failed`.
    """
    if not command:
        raise ValueError("give the command to capture after --")
    if not directory and not re.fullmatch(r"\.[a-z0-9]+", extension):
        raise ValueError(f"invalid extension {extension!r}: use e.g. .txt or .json")

    # name the snapshot, and refuse a bad or taken name, before running
    # anything: the same checks snapshot_to_alcove makes afterwards
    dataset_name = dataset_name.strip("/")
    alcove = Alcove()
    if alcove.storage.backend == "s3":
        _check_s3_credentials()
    if version is not None and not _is_valid_version(version):
        raise ValueError(
            f"invalid version {version!r}: use an ISO date, optionally followed "
            "by more, e.g. 2026-10-07 or 2026-10-07-1443, or latest"
        )
    if version is None and not _is_valid_version(dataset_name.split("/")[-1]):
        now = datetime.now(timezone.utc)
        if alcove.is_partitioned(StepURI("snapshot", f"{dataset_name}/*")):
            version = now.strftime("%Y-%m-%d")
        else:
            version = now.strftime("%Y-%m-%d-%H%M")
    if version is not None:
        dataset_name = f"{dataset_name}/{version}"
    uri, _ = _name_snapshot(alcove, dataset_name)
    if (SNAPSHOT_DIR / f"{uri.path}.meta.yaml").exists():
        raise ValueError(f"{uri} already exists; give another version with --version")
    dataset_name = uri.path

    started_at = utc_now()
    with tempfile.TemporaryDirectory() as tmp:
        if directory:
            out = Path(tmp) / "out"
            out.mkdir()
            returncode, stderr = run_and_capture(command, None, cwd=out)
        else:
            out = Path(tmp) / f"output{extension}"
            with out.open("wb") as f:
                returncode, stderr = run_and_capture(command, f)
        finished_at = utc_now()

        if returncode != 0 and not keep_failed:
            raise CaptureFailed(returncode, None)
        if directory and not any(p.is_file() for p in out.rglob("*")):
            if returncode != 0:
                raise CaptureFailed(returncode, None)
            raise ValueError(
                "the command wrote no files into the directory it ran in, "
                "so there is nothing to snapshot"
            )

        record: dict[str, Any] = {
            "command": redact(command),
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": returncode,
        }
        if stderr:
            record["stderr"] = stderr
        if note:
            record["note"] = note
        snapshot = snapshot_to_alcove(out, dataset_name, produced_by=record)

    if returncode != 0:
        raise CaptureFailed(returncode, snapshot)

    return snapshot


def list_steps_cmd(
    alcove: Alcove,
    regex: str | None = None,
    paths: bool = False,
    full: bool = False,
    alphabetical: bool = False,
) -> None:
    uris = _matching_steps(alcove, regex, alphabetical)

    lines: list[str] | list[Path]
    if paths:
        # paths are for scripts, which need every one
        lines = [s.rel_path for s in uris]
    elif full:
        lines = [str(s) for s in uris]
    else:
        lines = compact_steps(uris)

    for line in lines:
        print(line)


def list_steps(
    alcove: Alcove,
    regex: str | None = None,
    paths: bool = False,
    alphabetical: bool = False,
) -> list[Path] | list[StepURI]:
    found = _matching_steps(alcove, regex, alphabetical)

    if paths:
        return [s.rel_path for s in found]

    return found


def _matching_steps(
    alcove: Alcove, regex: str | None, alphabetical: bool
) -> list[StepURI]:
    if alphabetical:
        found = sorted(alcove.steps)
    else:
        found = steps.in_dataset_order(alcove.steps)

    found = [s for s in found if not s.is_wildcard]

    if regex:
        found = [s for s in found if re.search(regex, str(s))]

    return found


def plan_and_run(
    alcove: Alcove,
    regex: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    jobs: int = DEFAULT_FETCH_JOBS,
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

    steps.execute_dag(dag, dry_run=dry_run, jobs=jobs)


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {n}")
    return n


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
    alcove: Alcove, table_path: str, dependencies: list[str], lang: Language
) -> None:
    "Register a table in alcove.yaml and write a starter script that builds it."
    suffix = Path(table_path).suffix
    if suffix in (".py", ".sql", ".nu", ".parquet"):
        raise ValueError(
            f"Leave the {suffix} off the table path; pick the script's language "
            "with --lang"
        )

    uri = StepURI("table", table_path)
    check_no_script(uri)

    # register first: saving alcove.yaml validates the step and its
    # dependencies, and a failure there should leave no script behind
    alcove.new_table(table_path, dependencies)

    deps = [StepURI.parse(dep) for dep in dependencies]
    try:
        script = write_template(alcove, uri, deps, lang)
    except Exception:
        del alcove.steps[uri]
        alcove.save()
        raise
    print_op("CREATE", script)


def execute_query(
    alcove: Alcove,
    query: str,
    names: Literal["short", "full", "both"] = "both",
    csv: bool = False,
) -> None:
    with AlcoveDB(alcove=alcove, names=names) as db:
        result = db.sql(query)

        if csv:
            # CSV has no nested types, so write lists and structs as JSON
            nested = [c for c, t in result.schema.items() if t.is_nested()]
            result = result.with_columns(
                pl.Series(
                    c,
                    [
                        None if v is None else json.dumps(v, default=str)
                        for v in result[c].to_list()
                    ],
                    dtype=pl.String,
                )
                for c in nested
            )
            print(result.write_csv(), end="")
        else:
            print(result.write_json())


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
            raise ValueError(
                f"invalid dataset name {dataset_name!r}: give the dataset a name "
                "before its version, e.g. countries/latest"
            )

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
