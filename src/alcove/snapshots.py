#
#  snapshots.py
#
#  Adding and removing snapshots from the Alcove.
#


import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Union

import boto3
import jsonschema
from botocore.config import Config

from alcove.paths import BASE_DIR, SNAPSHOT_DIR
from alcove.schemas import SNAPSHOT_SCHEMA, validate_snapshot
from alcove.types import Checksum, DatasetName, FileName, Manifest, StepURI
from alcove.utils import (
    checksum_file,
    checksum_folder,
    checksum_manifest,
    load_yaml,
    print_op,
    save_yaml,
)


@dataclass
class Snapshot:
    uri: StepURI
    snapshot_type: Literal["file", "directory"]
    checksum: Checksum
    version: int = 1

    manifest: Optional[Manifest] = None
    extension: Optional[str] = None

    name: Optional[str] = None
    description: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    date_accessed: Optional[str] = None
    access_notes: Optional[str] = None
    license: Optional[str] = None
    license_url: Optional[str] = None

    @property
    def path(self):
        if self.snapshot_type == "file":
            return SNAPSHOT_DIR / f"{self.uri.path}{self.extension}"

        elif self.snapshot_type == "directory":
            return SNAPSHOT_DIR / self.uri.path

        raise ValueError(f"Unknown snapshot type: {self.snapshot_type}")

    @property
    def metadata_path(self) -> Path:
        return (SNAPSHOT_DIR / self.uri.path).with_suffix(".meta.yaml")

    @staticmethod
    def load(path: str) -> "Snapshot":
        "Load an existing snapshot from its metadata file."
        metadata_file = (SNAPSHOT_DIR / path).with_suffix(".meta.yaml")

        metadata = load_yaml(metadata_file)
        if "date_accessed" in metadata:
            metadata["date_accessed"] = str(metadata["date_accessed"])
        jsonschema.validate(metadata, SNAPSHOT_SCHEMA)

        metadata["uri"] = StepURI.parse(metadata["uri"])

        return Snapshot(**metadata)

    @staticmethod
    def create(
        local_path: Path, dataset_name: str, metadata: Optional[dict[str, Any]] = None
    ) -> "Snapshot":
        if local_path.is_dir():
            snapshot = Snapshot.create_from_directory(
                local_path, dataset_name, metadata
            )
        else:
            snapshot = Snapshot.create_from_file(local_path, dataset_name, metadata)

        return snapshot

    @staticmethod
    def create_from_directory(
        local_path: Path,
        dataset_name: DatasetName,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "Snapshot":
        data_path = SNAPSHOT_DIR / dataset_name

        # copy directory to data/snapshots/...
        copy_dir(local_path, data_path)

        # upload to s3
        manifest = add_directory_to_s3(data_path)
        checksum = checksum_manifest(manifest)

        # Create metadata record
        snapshot = Snapshot(
            uri=StepURI("snapshot", dataset_name),
            checksum=checksum,
            snapshot_type="directory",
            manifest=manifest,
            **(metadata or {}),
        )
        snapshot.save()

        return snapshot

    def get_metadata(self) -> dict:
        """Get all metadata fields that should be preserved"""
        return {
            "name": self.name,
            "description": self.description,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "date_accessed": self.date_accessed,
            "access_notes": self.access_notes,
            "license": self.license,
            "license_url": self.license_url,
        }

    def save(self, comments: bool = True):
        # prep the metadata record
        record = self.to_dict()
        validate_snapshot(record)

        if not comments:
            record = prune_empty_values(record)

        save_yaml(record, self.metadata_path, include_comments=comments)

    def to_dict(self) -> dict:
        record = asdict(self)
        record["uri"] = str(self.uri)
        # Include all fields, even if they are None
        return record

    @staticmethod
    def create_from_file(
        local_path: Path, dataset_name: DatasetName, metadata: Optional[dict[str, Any]]
    ) -> "Snapshot":
        # first we checksum
        checksum = checksum_file(local_path)

        # then copy it over right away, as a convenience
        data_path = (SNAPSHOT_DIR / dataset_name).with_suffix(local_path.suffix)
        if local_path != data_path:
            # if you edit a snapshot in place, the paths may be the same
            copy_file(local_path, data_path)

        # it tells us the s3 path to store it at
        add_to_s3(data_path, checksum)

        # then save the metadata record
        snapshot = Snapshot(
            uri=StepURI("snapshot", dataset_name),
            checksum=checksum,
            snapshot_type="file",
            extension=local_path.suffix,
            **(metadata or {}),
        )
        snapshot.save()

        return snapshot

    def is_up_to_date(self):
        if self.snapshot_type == "file":
            return self.path.exists() and self.checksum == checksum_file(self.path)

        elif self.snapshot_type == "directory":
            return self.path.is_dir() and self.checksum == checksum_manifest(
                checksum_folder(self.path)
            )

        raise ValueError(f"Unknown snapshot type: {self.snapshot_type}")

    def fetch(self) -> None:
        if self.snapshot_type == "file":
            fetch_from_s3(self.checksum, self.path)
            return

        elif self.snapshot_type == "directory":
            assert self.manifest is not None

            # if the diretory exists, remove any files that are not in the manifest
            if self.path.exists():
                for file_name in self.path.iterdir():
                    if file_name.name not in self.manifest:
                        print_op("DELETE", file_name)
                        file_name.unlink()

            for file_name, checksum in self.manifest.items():
                fetch_from_s3(checksum, self.path / file_name)
            return

        raise ValueError(f"Unknown snapshot type: {self.snapshot_type}")


def add_directory_to_s3(file_path: Path) -> dict[FileName, Checksum]:
    checksums = checksum_folder(file_path)
    for file_name, checksum in checksums.items():
        add_to_s3(file_path / file_name, checksum)

    return checksums


def add_to_s3(file_path: Union[str, Path], checksum: Checksum) -> None:
    # Test environment check
    if os.environ.get("TEST_ENVIRONMENT") == "1":
        print_op("UPLOAD", file_path)
        cache_path = (
            Path.home() / ".cache" / "alcove" / checksum[:2] / checksum[2:4] / checksum
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(file_path, cache_path)
        return

    s3 = s3_client()
    bucket_name = os.environ["S3_BUCKET_NAME"]
    dest_path = f"{checksum[:2]}/{checksum[2:4]}/{checksum}"
    print_op("UPLOAD", file_path)
    s3.upload_file(file_path, bucket_name, str(dest_path))


def open_in_editor(self, file_path: Path) -> None:
    editor = os.getenv("EDITOR", "vim")
    subprocess.run([editor, file_path])


def copy_file(local_path: Path, data_path: Path) -> None:
    assert not Path(local_path).is_dir()

    data_path.parent.mkdir(parents=True, exist_ok=True)

    print_op("ADD", f"{data_path.relative_to(BASE_DIR)}")
    shutil.copy(local_path, data_path)


def copy_dir(local_path: Path, data_path: Path) -> None:
    assert local_path.is_dir()

    data_path.parent.mkdir(parents=True, exist_ok=True)

    print_op("ADD", f"{data_path.relative_to(BASE_DIR)}/")
    shutil.copytree(local_path, data_path)


def is_completed(uri: StepURI) -> bool:
    assert uri.scheme == "snapshot"
    return Snapshot.load(uri.path).is_up_to_date()


def download_file(s3_path: str, dest_path: Path) -> None:
    s3 = s3_client()

    bucket_name = os.environ["S3_BUCKET_NAME"]
    dest_path_rel = dest_path.resolve().relative_to(BASE_DIR.resolve())

    print_op(
        "DOWNLOAD",
        dest_path_rel,
    )

    s3.download_file(bucket_name, s3_path, str(dest_path))


def s3_client():
    s3 = boto3.client(
        "s3",
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        # disable outgoing checksum header and skip checksum validation unless enforced
        # https://github.com/larsyencken/alcove/issues/60
        config=Config(
            request_checksum_calculation="when_required",  # ← disable outgoing checksum header
            response_checksum_validation="when_required",  # ← skip checksum validation unless enforced
        ),
    )
    return s3


def check_local_cache(checksum: Checksum) -> Optional[Path]:
    cache_path = (
        Path.home() / ".cache" / "alcove" / checksum[:2] / checksum[2:4] / checksum
    )

    if cache_path.exists():
        print_op("CACHE HIT", f"~/{cache_path.relative_to(Path.home())}")
        return cache_path

    return None


def fetch_from_s3(checksum: Checksum, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    cache_path = check_local_cache(checksum)
    if cache_path:
        shutil.copy(cache_path, dest_path)
        return

    s3_path = f"{checksum[:2]}/{checksum[2:4]}/{checksum}"
    download_file(s3_path, dest_path)

    cache_path = (
        Path.home() / ".cache" / "alcove" / checksum[:2] / checksum[2:4] / checksum
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    print_op("CACHE ADD", f"~/{cache_path.relative_to(Path.home())}")
    shutil.copy(dest_path, cache_path)


def prune_empty_values(record):
    record = record.copy()
    for k, v in list(record.items()):
        if v is None:
            del record[k]

    return record
