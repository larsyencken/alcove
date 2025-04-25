from dataclasses import dataclass
from functools import total_ordering
from typing import Literal

from alcove import paths

type Checksum = str
type FileName = str
type DatasetName = str
type Manifest = dict[FileName, Checksum]
type Dag = dict["StepURI", list["StepURI"]]
type DType = str
type Schema = dict[str, DType]


@total_ordering
@dataclass
class StepURI:
    scheme: Literal["snapshot", "table"]
    path: DatasetName

    @property
    def uri(self):
        return f"{self.scheme}://{self.path}"

    @property
    def full_path(self):
        if self.scheme == "snapshot":
            return paths.SNAPSHOT_DIR / self.path

        elif self.scheme == "table":
            return paths.TABLE_DIR / self.path

        raise ValueError(f'no common directory found for scheme "{self.scheme}"')

    @property
    def rel_path(self):
        return self.full_path.relative_to(paths.BASE_DIR)

    @classmethod
    def parse(cls, uri: str) -> "StepURI":
        scheme, path = uri.split("://")
        if scheme not in ["snapshot", "table"]:
            raise ValueError(f"Unknown scheme: {scheme}")
        return cls(scheme, path)  # type: ignore

    def __str__(self):
        return self.uri

    def __eq__(self, other):
        return self.uri == other.uri

    def __lt__(self, other):
        return self.uri < other.uri

    def __hash__(self):
        return hash(self.uri)
