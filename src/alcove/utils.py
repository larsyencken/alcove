import hashlib
from pathlib import Path
from typing import Any, Union

import yaml
from rich.console import Console

from alcove.paths import BASE_DIR
from alcove.types import Checksum, Manifest

console = Console()

IGNORE_FILES = {".DS_Store"}


def checksum_file(file_path: Union[str, Path]) -> Checksum:
    sha256 = hashlib.sha256()

    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(4096), b""):
            sha256.update(block)

    return sha256.hexdigest()


def checksum_folder(dir_path: Path) -> Manifest:
    manifest = {}
    # walk the subdirectory tree, adding relative path and checksums to the manifest
    for file_path in dir_path.rglob("*"):
        if file_path.is_file():
            if file_path.name in IGNORE_FILES:
                continue
            rel_path = file_path.relative_to(dir_path)
            manifest[str(rel_path)] = checksum_file(file_path)

    if not manifest:
        raise Exception(f'No files found in "{dir_path}" to checksum')

    return manifest


def checksum_manifest(manifest: Manifest) -> Checksum:
    sha256 = hashlib.sha256()

    for file_name, checksum in sorted(manifest.items()):
        sha256.update(file_name.encode())
        sha256.update(checksum.encode())

    return sha256.hexdigest()


def print_op(type_: str, message: Any) -> None:
    console.print(f"[blue]{type_:>15}[/blue]   {message}")


def add_to_gitignore(path: Path) -> None:
    gitignore = Path(".gitignore")
    path_str = str(path.relative_to(BASE_DIR))

    if gitignore.exists():
        with open(gitignore) as f:
            entries = set(line.strip() for line in f if line.strip())

        if path_str in entries:
            # it's already here
            return

        print_op("UPDATE", ".gitignore")
    else:
        print_op("CREATE", ".gitignore")

    with gitignore.open("a") as f:
        print(path_str, file=f)


def dump_yaml_with_comments(obj: dict, f) -> None:
    for key, value in obj.items():
        if value is None:
            f.write(f"# {key}: \n")
        else:
            yaml.dump({key: value}, f, sort_keys=False)


def save_yaml(obj: dict, path: Path, include_comments: bool = False) -> None:
    if path.exists():
        print_op("UPDATE", path)
    else:
        print_op("CREATE", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        if include_comments:
            dump_yaml_with_comments(obj, f)
        else:
            yaml.dump(obj, f, sort_keys=False)


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text())
