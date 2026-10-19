#
#  storage.py
#
#  Where snapshot data lives: S3, a local directory, or the git repo itself.
#

import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Protocol, Union

from alcove.types import Checksum
from alcove.utils import load_yaml, print_op

type Backend = Literal["s3", "local", "git"]
BACKENDS: tuple[Backend, ...] = ("s3", "local", "git")

DEFAULT_LOCAL_STORE = Path(".alcove") / "blobs"
GIT_REF = "refs/alcove/blobs"

# overrides `path` for a local store, for a store kept on another volume
LOCAL_STORE_ENV = "ALCOVE_LOCAL_STORE"


class StorageError(Exception):
    pass


def blob_path(checksum: Checksum) -> str:
    "Where a blob lives in any store, e.g. `ab/cd/abcd...`."
    return f"{checksum[:2]}/{checksum[2:4]}/{checksum}"


@dataclass
class StorageConfig:
    backend: Backend = "s3"
    # local store only, relative to alcove.yaml
    path: Optional[str] = None

    @staticmethod
    def parse(value: Union[None, str, dict]) -> "StorageConfig":
        "Read the `storage` key of alcove.yaml; a missing key means S3."
        if value is None:
            return StorageConfig()
        if isinstance(value, str):
            value = {"backend": value}
        if value.get("backend") not in BACKENDS:
            raise StorageError(
                f"unknown storage backend {value.get('backend')!r}, "
                f"expected one of {', '.join(BACKENDS)}"
            )
        return StorageConfig(backend=value["backend"], path=value.get("path"))

    def to_yaml(self) -> Union[str, dict]:
        if self.path is None:
            return self.backend
        return {"backend": self.backend, "path": self.path}

    @staticmethod
    def load(config_file: Path) -> "StorageConfig":
        return StorageConfig.parse(load_yaml(config_file).get("storage"))


class BlobStore(Protocol):
    def put(self, files: list[tuple[Path, Checksum]]) -> None:
        "Store each file under its checksum."
        ...

    def get(self, checksum: Checksum, dest_path: Path) -> None:
        "Write the blob with this checksum to dest_path."
        ...


class LocalStore:
    "Blobs in a plain directory, laid out as in S3."

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, files: list[tuple[Path, Checksum]]) -> None:
        for file_path, checksum in files:
            dest = self.root / blob_path(checksum)
            if dest.exists():
                continue
            print_op("STORE", file_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # a blob is never checked against its checksum when read, so an
            # interrupted copy must not leave a partial file under its name
            partial = dest.with_name(
                f"{dest.name}.{os.getpid()}.{threading.get_ident()}.partial"
            )
            shutil.copy(file_path, partial)
            os.replace(partial, dest)

    def get(self, checksum: Checksum, dest_path: Path) -> None:
        source = self.root / blob_path(checksum)
        if not source.exists():
            raise StorageError(f"{checksum} is not in the local store {self.root}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        print_op("COPY", dest_path)
        shutil.copy(source, dest_path)


class GitStore:
    """Blobs in the git repo's own object store.

    They are kept reachable by a tree at `refs/alcove/blobs` whose paths are
    their checksums, laid out as in S3. Neither clone nor push copies that ref
    unless asked to, e.g. `git fetch origin 'refs/alcove/*:refs/alcove/*'`.
    """

    # a store is shared by the threads of one process; other processes are
    # caught by update-ref refusing to move the ref from a stale value
    _lock = threading.Lock()

    def __init__(self, cwd: Path = Path(".")) -> None:
        self.cwd = cwd

    def _git(self, *args: str, input: Optional[str] = None, env=None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.cwd,
            input=input,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise StorageError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def check(self) -> None:
        "Fail unless we're inside a git repo."
        self._git("rev-parse", "--git-dir")

    def _head(self) -> Optional[str]:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", GIT_REF],
            cwd=self.cwd,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None

    def put(self, files: list[tuple[Path, Checksum]]) -> None:
        if not files:
            return

        for file_path, _ in files:
            print_op("STORE", file_path)

        paths = "".join(f"{os.path.abspath(p)}\n" for p, _ in files)
        # stored byte for byte: the repo's attributes (autocrlf, clean
        # filters) would otherwise change the data away from its checksum
        oids = self._git(
            "hash-object", "-w", "--no-filters", "--stdin-paths", input=paths
        ).split()
        entries = "".join(
            f"100644 {oid}\t{blob_path(checksum)}\n"
            for oid, (_, checksum) in zip(oids, files)
        )

        with self._lock:
            for _ in range(5):
                if self._add_to_tree(entries):
                    return
        raise StorageError(f"{GIT_REF} kept changing while adding to it")

    def _add_to_tree(self, entries: str) -> bool:
        "Commit the entries onto the ref; False if another process moved it."
        old = self._head()

        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if old:
                self._git("read-tree", old, env=env)
            self._git("update-index", "--add", "--index-info", input=entries, env=env)
            tree = self._git("write-tree", env=env)

        if old and self._git("rev-parse", f"{old}^{{tree}}") == tree:
            # every blob was already there
            return True

        parents = ["-p", old] if old else []
        commit = self._git(
            "commit-tree", tree, *parents, "-m", "alcove: add blobs", env=_identity()
        )
        try:
            # an empty old value means the ref must not exist yet
            self._git("update-ref", GIT_REF, commit, old or "")
        except StorageError:
            return False
        return True

    def get(self, checksum: Checksum, dest_path: Path) -> None:
        spec = f"{GIT_REF}:{blob_path(checksum)}"
        if subprocess.run(["git", "cat-file", "-e", spec], cwd=self.cwd).returncode:
            raise StorageError(
                f"{checksum} is not in {GIT_REF}; if it was stored in another "
                "clone, try `git fetch origin 'refs/alcove/*:refs/alcove/*'`"
            )

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        print_op("CHECKOUT", dest_path)
        # written in place, like a download, so a link to another file of the
        # snapshot is written through; a file left half-written by a failure
        # fails its checksum and is fetched again next run
        with dest_path.open("wb") as f:
            subprocess.run(
                ["git", "cat-file", "blob", spec], cwd=self.cwd, stdout=f, check=True
            )


def _identity() -> dict[str, str]:
    "The environment for commit-tree, falling back to an alcove identity."
    env = dict(os.environ)
    has_identity = (
        subprocess.run(
            ["git", "var", "GIT_COMMITTER_IDENT"], capture_output=True
        ).returncode
        == 0
    )
    if not has_identity:
        for role in ("AUTHOR", "COMMITTER"):
            env.setdefault(f"GIT_{role}_NAME", "alcove")
            env.setdefault(f"GIT_{role}_EMAIL", "alcove@localhost")
    return env


def local_store_root(config: StorageConfig, base_dir: Path) -> Path:
    if LOCAL_STORE_ENV in os.environ:
        return Path(os.environ[LOCAL_STORE_ENV])
    return base_dir / (config.path or str(DEFAULT_LOCAL_STORE))
