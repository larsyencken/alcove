from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from alcove.schemas import ALCOVE_SCHEMA
from alcove.types import Dag, StepURI
from alcove.utils import load_yaml, save_yaml

DEFAULT_ALCOVE_PATH = Path("alcove.yaml")


@dataclass
class Alcove:
    config_file: Path
    steps: Dag = field(default_factory=dict)
    version: int = 1

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
                str(k): [str(v) for v in vs] for k, vs in sorted(self.steps.items())
            },
        }
        jsonschema.validate(config, ALCOVE_SCHEMA)
        save_yaml(config, self.config_file)

    def new_table(self, table_path: str, dependencies: list[str]) -> None:
        table_uri = StepURI("table", table_path)
        if table_uri in self.steps:
            raise ValueError(f"Table already exists in alcove: {table_uri}")

        self.steps[table_uri] = [StepURI.parse(dep) for dep in dependencies]
        self.save()

    def get_latest_version(self, step: StepURI) -> StepURI:
        assert step.path.endswith("/latest")
        prefix = step.path.rsplit("/", 1)[0]
        versions = [
            s
            for s in self.steps
            if s.scheme == step.scheme and s.path.startswith(prefix)
        ]
        return max(versions)
