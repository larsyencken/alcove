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


def remove_orphans(declared: Dag, expanded: Dag) -> None:
    """Delete data in a wildcard dataset's folder that none of its versions
    owns any more.

    Partition data and the outputs built from each partition are not in git.
    When a partition is dropped by deleting its metadata, perhaps on another
    machine and then pulled, or a snapshot is interrupted before its metadata
    is written, that data stays on disk, where the globs that read a whole
    dataset at once would still find it.

    Only build products and date-named partition data are touched: snapshot
    metadata, and any snapshot data not named like a partition, are left be.
    """
    for step in declared:
        if not step.is_wildcard:
            continue

        folder = step.full_path.parent
        if not folder.is_dir():
            continue

        base = step.base_path
        versions = {
            s.version
            for s in expanded
            if s.scheme == step.scheme and s.base_path == base
        }
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
            if version is not None and version not in versions | nested:
                print_op("DELETE", path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()


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
