from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import jsonschema

from alcove.partitions import discover_partitions
from alcove.schemas import ALCOVE_SCHEMA
from alcove.types import Dag, StepURI
from alcove.utils import load_yaml, save_yaml

DEFAULT_ALCOVE_PATH = Path("alcove.yaml")


@dataclass
class Alcove:
    config_file: Path
    steps: Dag = field(default_factory=dict)
    version: int = 1
    # partitions of wildcard snapshot datasets, found on disk rather than
    # declared in alcove.yaml; never written back to it
    discovered: set[StepURI] = field(default_factory=set)

    def __init__(self, config_file: Path = DEFAULT_ALCOVE_PATH):
        "Load an existing alcove.yaml file from disk."
        if not config_file.exists():
            raise FileNotFoundError("alcove.yaml not found")

        self.config_file = config_file
        self.refresh()

    def refresh(self) -> None:
        config = load_yaml(self.config_file)
        jsonschema.validate(config, ALCOVE_SCHEMA)

        self.version = config["version"]
        self.steps = {
            StepURI.parse(s): [StepURI.parse(d) for d in deps]
            for s, deps in config["steps"].items()
        }

        # a snapshot declared as `snapshot://foo/*` is date-partitioned: its
        # partitions are discovered from their metadata files on disk
        self.discovered = set()
        for step in list(self.steps):
            if step.scheme == "snapshot" and step.is_wildcard:
                for version in discover_partitions(step.base_path):
                    partition = step.with_version(version)
                    if partition not in self.steps:
                        self.steps[partition] = []
                        self.discovered.add(partition)

    @staticmethod
    def init(alcove_file: Path = DEFAULT_ALCOVE_PATH) -> "Alcove":
        if not alcove_file.exists():
            save_yaml(
                {
                    "version": 1,
                    "data_dir": "data",
                    "steps": {},
                },
                alcove_file,
            )
        else:
            print(f"{alcove_file} already exists")

        return Alcove()

    def save(self) -> None:
        config = {
            "version": self.version,
            "steps": {
                str(k): [str(v) for v in vs]
                for k, vs in sorted(self.steps.items())
                if k not in self.discovered
            },
        }
        jsonschema.validate(config, ALCOVE_SCHEMA)
        save_yaml(config, self.config_file)

    def new_step(
        self, scheme: Literal["table", "artifact"], path: str, dependencies: list[str]
    ) -> None:
        "Register a derived step and its dependencies in alcove.yaml."
        uri = StepURI(scheme, path)
        if uri in self.steps:
            raise ValueError(f"{scheme.capitalize()} already exists in alcove: {uri}")

        self.steps[uri] = [StepURI.parse(dep) for dep in dependencies]
        self.save()

    def new_table(self, table_path: str, dependencies: list[str]) -> None:
        self.new_step("table", table_path, dependencies)

    def new_artifact(self, artifact_path: str, dependencies: list[str]) -> None:
        self.new_step("artifact", artifact_path, dependencies)

    def is_partitioned(self, step: StepURI) -> bool:
        "Is this a version of a dataset declared as partitioned (`foo/*`)?"
        return step.scheme == "snapshot" and step.with_version("*") in self.steps

    def versions(self, step: StepURI) -> list[str]:
        """Every concrete version of the dataset `step` belongs to, oldest first.

        `step` can be any version of the dataset, e.g. `snapshot://foo/*`;
        only exact siblings count, not `foo_v2/...` or `foo/bar/...`.
        """
        return sorted(
            s.version
            for s in self.steps
            if s.scheme == step.scheme
            and s.base_path == step.base_path
            and not s.is_wildcard
        )

    def get_latest_version(self, step: StepURI) -> StepURI:
        assert step.path.endswith("/latest")
        versions = self.versions(step)
        if not versions:
            raise ValueError(
                f"Cannot resolve {step}: no versions of {step.base_path} found"
            )

        return step.with_version(versions[-1])
