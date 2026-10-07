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
from pathlib import Path

from alcove.paths import SNAPSHOT_DIR

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
    found from the metadata files in its folder, oldest first."""
    folder = SNAPSHOT_DIR / base_path
    if not folder.is_dir():
        return []

    versions = []
    for metadata_file in folder.glob(f"*{_METADATA_SUFFIX}"):
        version = metadata_file.name.removesuffix(_METADATA_SUFFIX)
        if not is_partition_version(version):
            raise ValueError(
                f"Found {metadata_file} in partitioned dataset "
                f"snapshot://{base_path}/*, but partitions must be named by "
                "an ISO date (YYYY-MM-DD)"
            )
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
