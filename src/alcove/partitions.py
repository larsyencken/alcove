"""Date-partitioned snapshot datasets.

A snapshot dataset is partitioned when alcove.yaml declares it once with a
wildcard, e.g. ``snapshot://gpu/usage/*: []``. Each partition is then an
ordinary snapshot named by the ISO date its data covers
(``gpu/usage/2026-10-06``), but it is not listed in alcove.yaml. Partitions
are discovered from their metadata files on disk, which are committed to git,
so adding a day's data adds one new file and edits nothing shared.
"""

import datetime
import re
import shutil
from pathlib import Path

from alcove.paths import SNAPSHOT_DIR
from alcove.types import Dag
from alcove.utils import print_op

# Matches a partition's name (and nothing else in its dataset folder, such as
# its .meta.yaml sidecar), both as a gitignore pattern and as a file glob.
PARTITION_GLOB = "????-??-??"

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_METADATA_SUFFIX = ".meta.yaml"


def is_partition_version(version: str) -> bool:
    "Partitions are named by a real calendar date, e.g. 2026-10-06."
    if not _ISO_DATE.fullmatch(version):
        return False

    try:
        datetime.date.fromisoformat(version)
    except ValueError:
        return False

    return True


def discover_partitions(base_path: str) -> list[str]:
    """The versions of a partitioned snapshot dataset that exist on disk,
    found from the date-named metadata files in its folder, oldest first.

    Other metadata files there are not partitions; `alcove audit` reports
    them rather than every command failing over one stray file.
    """
    folder = SNAPSHOT_DIR / base_path
    if not folder.is_dir():
        return []

    versions = []
    for metadata_file in folder.glob(f"*{_METADATA_SUFFIX}"):
        version = metadata_file.name.removesuffix(_METADATA_SUFFIX)
        if is_partition_version(version):
            versions.append(version)

    return sorted(versions)


def partition_gitignore_entry(base_path: str, extension: str | None) -> str:
    """One data/.gitignore pattern that covers every partition of a dataset.

    It matches partition data by its date-shaped name, so it never matches the
    .meta.yaml sidecars that must stay in git, and needs no `!` exception
    (which `alcove audit --fix` could reorder when it sorts the file).
    """
    folder = Path("snapshots") / base_path
    if extension is None:
        # directory partitions; the trailing slash matches directories only
        return f"{folder / PARTITION_GLOB}/"

    return str(folder / f"{PARTITION_GLOB}{extension}")


# Where partition-shaped data with no metadata is moved, out of reach of the
# dataset's glob, and where it is restored from if its metadata comes back.
# It carries its own .gitignore, so git never sees what's in it.
ORPHANED_DIR = ".orphaned"

# `2026-01-02~1`: a second thing set aside under the same name
_SET_ASIDE_COPY = re.compile(r"~\d+$")


def tidy_orphans(
    declared: Dag,
    expanded: Dag,
    scope: Dag | None = None,
    dry_run: bool = False,
) -> None:
    """Deal with files in a wildcard dataset's folder that none of its versions
    owns, before the globs that read a whole dataset can find them.

    For a partitioned snapshot this is data named like a partition but with
    no metadata. A partition was dropped, perhaps on another machine (its data
    was never in git); a branch was checked out that predates it; a snapshot
    was interrupted; or data was written there before being snapshotted. Some
    of these could be the only copy, so the data is moved aside into
    `<dataset>/.orphaned/` rather than deleted, and moved back if a partition
    with exactly that content reappears, e.g. on checking the newer branch
    out again.

    Tables and artifacts built for a version that's gone are build products,
    and are deleted.

    `scope` limits this to datasets with steps in it, e.g. the steps a
    filtered `alcove run` will look at.
    """
    for step in declared:
        if not step.is_wildcard:
            continue

        base = step.base_path
        if scope is not None and not any(
            s.scheme == step.scheme and s.base_path == base for s in scope
        ):
            continue

        folder = step.full_path.parent
        versions = {
            s.version
            for s in expanded
            if s.scheme == step.scheme and s.base_path == base
        }
        if step.scheme == "snapshot":
            _restore_set_aside(base, versions, dry_run)

        if not folder.is_dir():
            continue

        # datasets nested below this one, e.g. foo/bar/latest beside foo/*
        nested = {
            s.path[len(base) + 1 :].split("/")[0]
            for s in expanded
            if s.scheme == step.scheme
            and s.path.startswith(f"{base}/")
            and s.base_path != base
        }
        for path in sorted(folder.iterdir()):
            version = _owning_version(step.scheme, path)
            if version is None or version in versions | nested:
                continue

            if step.scheme == "snapshot":
                dest = _free_path(folder / ORPHANED_DIR / path.name)
                print_op(
                    "WOULD SET ASIDE" if dry_run else "SET ASIDE", f"{path} -> {dest}"
                )
                if not dry_run:
                    _make_set_aside_dir(dest.parent)
                    shutil.move(path, dest)
            elif dry_run:
                print_op("WOULD DELETE", path)
            else:
                print_op("DELETE", path)
                _delete(path)


def _restore_set_aside(base_path: str, versions: set[str], dry_run: bool) -> None:
    "Move set-aside data back for partitions that have their metadata again."
    from alcove.snapshots import Snapshot
    from alcove.utils import checksum_file, checksum_folder, checksum_manifest

    set_aside = SNAPSHOT_DIR / base_path / ORPHANED_DIR
    if not set_aside.is_dir():
        return

    for path in sorted(set_aside.iterdir()):
        name = _SET_ASIDE_COPY.sub("", path.name)
        version = name[:10]
        if version not in versions:
            continue

        snapshot = Snapshot.load(f"{base_path}/{version}")
        if snapshot.path.name != name or snapshot.path.exists():
            continue
        if snapshot.path.is_symlink():
            # a dangling link where the data should be; leave it for fetch
            continue

        # only data that is exactly this partition's, or we'd lose it when the
        # partition is fetched over it
        try:
            if snapshot.snapshot_type == "file":
                matches = checksum_file(path) == snapshot.checksum
            else:
                matches = checksum_manifest(checksum_folder(path)) == snapshot.checksum
        except Exception:
            # e.g. an empty folder or a dangling link: not this partition's data
            matches = False

        if matches:
            print_op("WOULD RESTORE" if dry_run else "RESTORE", snapshot.path)
            if not dry_run:
                shutil.move(path, snapshot.path)

    if not dry_run and all(p.name == ".gitignore" for p in set_aside.iterdir()):
        shutil.rmtree(set_aside)


def _make_set_aside_dir(path: Path) -> None:
    path.mkdir(exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")


def _free_path(path: Path) -> Path:
    "`path`, or `path~1`, `path~2`... if something is already there."
    candidate, n = path, 0
    while candidate.exists() or candidate.is_symlink():
        n += 1
        candidate = path.with_name(f"{path.name}~{n}")
    return candidate


def _delete(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _owning_version(scheme: str, path: Path) -> str | None:
    "Which version of its dataset a file or folder belongs to, if any."
    name = path.name
    if scheme == "snapshot":
        # partition data only: `2026-10-06/` or `2026-10-06.<ext>`
        version, rest = name[:10], name[10:]
        if name.endswith(_METADATA_SUFFIX) or not is_partition_version(version):
            return None
        if path.is_dir():
            return version if not rest else None
        return version if rest.startswith(".") and rest.count(".") == 1 else None

    if scheme == "table":
        # `<version>.parquet` and `<version>.meta.yaml`
        for suffix in (".parquet", _METADATA_SUFFIX):
            if path.is_file() and name.endswith(suffix):
                return name.removesuffix(suffix)
        return None

    if scheme == "artifact":
        # `<version>/` and `<version>.meta.yaml`
        if path.is_dir():
            # an in-progress build is a sibling `<version>.building` folder
            return None if name.endswith(".building") else name
        if name.endswith(_METADATA_SUFFIX):
            return name.removesuffix(_METADATA_SUFFIX)
        return None

    return None
