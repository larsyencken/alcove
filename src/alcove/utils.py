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


def add_entry_to_file(file_path: Path, entry: str) -> None:
    """
    Add an entry to a file if it doesn't already exist.
    
    Args:
        file_path: The path to the file to add the entry to
        entry: The string to add to the file
    """
    if file_path.exists():
        with open(file_path) as f:
            entries = set(line.strip() for line in f if line.strip())

        if entry in entries:
            # Entry already exists, nothing to do
            return

        print_op("UPDATE", str(file_path.name))
    else:
        print_op("CREATE", str(file_path.name))

    with file_path.open("a") as f:
        print(entry, file=f)


def ensure_data_gitignore() -> None:
    """
    Ensure that data/.gitignore exists with the tables/ entry.
    Creates the file if it doesn't exist.
    """
    data_dir = Path("data")
    data_gitignore = data_dir / ".gitignore"
    
    # Create data directory if it doesn't exist
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        print_op("CREATE", "data/")
    
    # Create data/.gitignore if it doesn't exist with the tables/ entry
    if not data_gitignore.exists():
        data_gitignore.write_text("tables/\n")
        print_op("CREATE", "data/.gitignore")
    else:
        # Make sure tables/ is in data/.gitignore
        with open(data_gitignore) as f:
            entries = set(line.strip() for line in f if line.strip())
        
        if "tables/" not in entries:
            add_entry_to_file(data_gitignore, "tables/")


def add_to_data_gitignore(path: Path) -> None:
    """
    Add a path to the data/.gitignore file, creating it if it doesn't exist.
    
    Args:
        path: The path to add to data/.gitignore
    """
    data_dir = Path("data")
    data_gitignore = data_dir / ".gitignore"
    path_str = str(path.relative_to(BASE_DIR))
    
    # First ensure data/.gitignore exists
    ensure_data_gitignore()
    
    # If path starts with "data/", remove that prefix
    if path_str.startswith("data/"):
        path_str = path_str[5:]  # Remove "data/" prefix
    
    # Then add the path to data/.gitignore
    add_entry_to_file(data_gitignore, path_str)


def add_to_gitignore(path: Path) -> None:
    """
    Legacy function to add a path directly to .gitignore.
    This will be phased out in favor of add_to_data_gitignore.
    
    Args:
        path: The path to add to .gitignore
    """
    # Instead of adding to .gitignore, add to data/.gitignore
    add_to_data_gitignore(path)


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
