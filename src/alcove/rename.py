"""Renaming a dataset: every version of a snapshot, table or artifact.

`alcove rename table://foo table://bar` changes everything alcove itself
keeps under the old name:

- the dataset's steps and every dependency on them in alcove.yaml
- the scripts that build a table or artifact, and their `.meta.yaml` configs
- snapshot metadata, and any fetched snapshot data and data/.gitignore entries
- built tables and artifacts, which then rebuild on the next `alcove run`,
  since the inputs recorded in their metadata no longer exist
- template variables in the SQL steps that read the dataset, e.g. `{foo}`
  becomes `{bar}`
- `inherit` keys in table configs that name the dataset

Text it does not manage, such as a Python script that reads a table by its
path or a snapshot script that names its dataset, is left as it is; the
lines that still mention the old name are listed at the end.
"""

import re
import shutil
import string
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from alcove.core import Alcove
from alcove.partitions import ORPHANED_DIR, partition_gitignore_entry
from alcove.paths import ARTIFACT_DIR, DATA_DIR, SNAPSHOT_DIR, TABLE_DIR
from alcove.schemas import check_step_uri
from alcove.snapshots import Snapshot
from alcove.table_metadata import _get_executable, _script_dir
from alcove.tables import (
    _argument_groups,
    _dependency_glob_path,
    _dependency_path,
    _simplify_dependency_names,
)
from alcove.types import Dag, StepURI
from alcove.utils import add_pattern_to_data_gitignore, add_to_data_gitignore, print_op
from alcove.wildcards import expand_wildcards

DATA_DIRS = {"snapshot": SNAPSHOT_DIR, "table": TABLE_DIR, "artifact": ARTIFACT_DIR}


@dataclass
class RenamePlan:
    old: StepURI
    new: StepURI
    # (from, to), in the order they are made
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    # snapshot metadata files to give their new uri, at their new path
    snapshot_metadata: list[tuple[Path, StepURI, StepURI]] = field(default_factory=list)
    # SQL scripts, at their new path, and the template variables to rename
    sql_renames: dict[Path, dict[str, str]] = field(default_factory=dict)
    # table configs, at their new path, whose `inherit` keys name the dataset
    configs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # for a snapshot: is it partitioned, and the extensions of its data
    partitioned: bool = False
    extensions: set[str | None] = field(default_factory=set)


def rename_dataset(alcove: Alcove, old: str, new: str, dry_run: bool = False) -> None:
    "Rename every version of a dataset, e.g. `table://foo` to `table://bar`."
    plan = plan_rename(alcove, old, new)

    for src, dest in plan.moves:
        print_op("MOVE", f"{src} -> {dest}")
    for path, renames in plan.sql_renames.items():
        changes = ", ".join(f"{{{a}}} -> {{{b}}}" for a, b in renames.items())
        print_op("UPDATE", f"{path} ({changes})")
    for path in plan.configs:
        print_op("UPDATE", path)
    print_op("UPDATE", alcove.config_file)

    # what the rename itself would rewrite isn't left over
    skip = {alcove.config_file}
    if dry_run:
        skip |= {src for src, _ in plan.moves} | set(plan.configs)
    else:
        _execute(alcove, plan)

    for warning in plan.warnings:
        print(f"WARNING: {warning}")

    leftovers = _references_left(plan, skip)
    if leftovers:
        print(
            f"\nThese lines still mention {plan.old.path}; alcove does not "
            "manage them, so change them yourself if they refer to it:"
        )
        for line in leftovers:
            print(f"  {line}")


def plan_rename(alcove: Alcove, old: str, new: str) -> RenamePlan:
    "Work out every change a rename makes, without making any of them."
    old_uri = _parse_dataset(old)
    new_uri = _parse_dataset(new)
    if old_uri.scheme != new_uri.scheme:
        raise ValueError(
            f"Cannot rename a {old_uri.scheme} into a {new_uri.scheme}: "
            f"{old_uri} and {new_uri} must have the same scheme"
        )
    if old_uri == new_uri:
        raise ValueError(f"{old_uri} already has that name")

    plan = RenamePlan(old_uri, new_uri)
    rename = _renamer(old_uri, new_uri)

    steps = [s for s in alcove.steps if _in_dataset(s, old_uri)]
    if not steps:
        parent = StepURI(old_uri.scheme, old_uri.base_path)
        if any(_in_dataset(s, parent) for s in alcove.steps):
            raise ValueError(
                f"{old_uri} is a version, not a dataset; name the dataset "
                f"without its version, e.g. {old_uri.scheme}://{old_uri.base_path}, "
                "to rename all its versions together"
            )
        raise ValueError(f"No dataset {old_uri} in this alcove")

    taken = [
        s
        for s in [*alcove.steps, *(d for ds in alcove.steps.values() for d in ds)]
        if _in_dataset(s, new_uri)
    ]
    if taken:
        raise ValueError(f"{new_uri} already exists in this alcove: {taken[0]}")

    for step in steps:
        check_step_uri(str(rename(step)))

    # read before anything moves, since it loads snapshot metadata
    plan.sql_renames = _plan_sql_renames(alcove, old_uri, new_uri, plan)

    if old_uri.scheme == "snapshot":
        _plan_snapshot_moves(alcove, steps, plan)
    else:
        _plan_script_moves(steps, plan)
        # tables and artifacts built per partition of a wildcard are not
        # steps in alcove.yaml, so find what was built from the files
        _plan_built_moves(plan)

    plan.configs = _plan_config_updates(alcove, plan)

    # check every destination up front, so a rename never stops half done
    sources = {src for src, _ in plan.moves}
    for src, dest in plan.moves:
        if dest.exists() and dest not in sources:
            raise ValueError(f"Cannot move {src} to {dest}: it already exists")

    return plan


def _parse_dataset(name: str) -> StepURI:
    if "://" not in name:
        raise ValueError(
            f"Give {name} with its scheme, e.g. table://{name} or snapshot://{name}"
        )
    uri = StepURI.parse(name.rstrip("/"))
    if not uri.path or uri.is_wildcard:
        raise ValueError(f"{name} does not name a dataset")
    return uri


def _in_dataset(step: StepURI, dataset: StepURI) -> bool:
    "Is this step a version (`latest` and `*` included) of the dataset?"
    return step.scheme == dataset.scheme and step.base_path == dataset.path


def _version(dataset: StepURI, version: str) -> StepURI:
    "A version of a dataset; unlike StepURI.with_version, it adds a segment."
    return StepURI(dataset.scheme, f"{dataset.path}/{version}")


def _renamer(old: StepURI, new: StepURI):
    def rename(step: StepURI) -> StepURI:
        if _in_dataset(step, old):
            return _version(new, step.version)
        return step

    return rename


def _rename_path(path: Path, root: Path, old: str, new: str) -> Path:
    "data/tables/foo/latest.parquet -> data/tables/bar/latest.parquet, for foo -> bar."
    rel = str(path.relative_to(root))
    assert rel == old or rel.startswith((f"{old}/", f"{old}.")), (path, old)
    return root / f"{new}{rel[len(old) :]}"


def _plan_snapshot_moves(alcove: Alcove, steps: list[StepURI], plan: RenamePlan):
    old, new = plan.old.path, plan.new.path
    plan.partitioned = _version(plan.old, "*") in alcove.steps

    for step in sorted(steps):
        if step.is_wildcard:
            continue

        snapshot = Snapshot.load(step.path)
        plan.extensions.add(
            snapshot.extension if snapshot.snapshot_type == "file" else None
        )
        metadata = snapshot.metadata_path
        new_metadata = _rename_path(metadata, SNAPSHOT_DIR, old, new)
        plan.moves.append((metadata, new_metadata))
        plan.snapshot_metadata.append(
            (new_metadata, step, _version(plan.new, step.version))
        )

        if snapshot.path.exists():
            plan.moves.append(
                (snapshot.path, _rename_path(snapshot.path, SNAPSHOT_DIR, old, new))
            )

    orphaned = SNAPSHOT_DIR / old / ORPHANED_DIR
    if plan.partitioned and orphaned.exists():
        plan.moves.append((orphaned, SNAPSHOT_DIR / new / ORPHANED_DIR))


def _plan_script_moves(steps: list[StepURI], plan: RenamePlan) -> None:
    "The script (or step folder) that builds each version, and its config."
    script_dir = _script_dir(plan.old)
    seen = set()
    for step in sorted(steps):
        try:
            script = _get_executable(step, check=False)
        except FileNotFoundError:
            plan.warnings.append(f"{step} has no script to move")
            continue

        for path in [script, script.with_suffix(".meta.yaml")]:
            if path in seen or not path.exists():
                continue
            seen.add(path)
            plan.moves.append(
                (path, _rename_path(path, script_dir, plan.old.path, plan.new.path))
            )


def _plan_built_moves(plan: RenamePlan) -> None:
    "Every built version of a table or artifact, found by its metadata file."
    root = DATA_DIRS[plan.old.scheme]
    folder = root / plan.old.path
    if not folder.is_dir():
        return

    for metadata in sorted(folder.glob("*.meta.yaml")):
        version = metadata.name.removesuffix(".meta.yaml")
        if plan.old.scheme == "table":
            outputs = [folder / f"{version}.parquet"]
        else:
            outputs = [folder / version]

        for path in [*outputs, metadata]:
            if path.exists():
                plan.moves.append(
                    (path, _rename_path(path, root, plan.old.path, plan.new.path))
                )


def _expanded_dag(alcove: Alcove) -> Dag:
    "The steps `alcove run` would consider, with `latest` and `*` resolved."
    from alcove import resolve_latest

    dag = {step: resolve_latest(deps, alcove) for step, deps in alcove.steps.items()}
    expanded, _ = expand_wildcards(dag)
    return expanded


def _argument_paths(deps: list[StepURI]) -> list[Path]:
    "What a step's script is given for its dependencies, as when building it."
    paths = []
    for group in _argument_groups(deps):
        if len(group) > 1:
            paths.append(_dependency_glob_path(group))
        else:
            paths.append(_dependency_path(group[0]))
    return paths


def _plan_sql_renames(
    alcove: Alcove, old: StepURI, new: StepURI, plan: RenamePlan
) -> dict[Path, dict[str, str]]:
    """The template variables each SQL step must rename, keyed by where its
    script will be.

    A dependency's variable is named after its path, so renaming the dataset
    renames it, and can rename another dependency's variable too, if the new
    name clashes with it or the old one did.
    """
    try:
        dag = _expanded_dag(alcove)
    except ValueError as e:
        plan.warnings.append(
            f"SQL template variables were not updated, since the steps could "
            f"not be expanded: {e}"
        )
        return {}

    root = DATA_DIRS[old.scheme]
    old_dir = root / old.path

    def renamed(path: Path) -> Path:
        if path.parent == old_dir:
            return root / new.path / path.name
        return path

    per_script: dict[Path, dict[str, str]] = {}
    conflicts: set[Path] = set()
    for step, deps in dag.items():
        if step.scheme not in ("table", "artifact"):
            continue
        if not any(_in_dataset(d, old) for d in deps):
            continue
        try:
            script = _get_executable(step, check=False)
        except FileNotFoundError:
            continue
        if script.suffix != ".sql":
            continue

        before = _argument_paths(deps)
        after = [renamed(p) for p in before]
        before_names = {p: n for n, p in _simplify_dependency_names(before).items()}
        after_names = {p: n for n, p in _simplify_dependency_names(after).items()}
        renames = {
            before_names[b]: after_names[a]
            for b, a in zip(before, after)
            if before_names[b] != after_names[a]
        }
        if not renames:
            continue

        if _in_dataset(step, old):
            script = _rename_path(script, _script_dir(old), old.path, new.path)

        if script in per_script and per_script[script] != renames:
            # a script shared by several versions whose dependencies differ
            conflicts.add(script)
        per_script.setdefault(script, {}).update(renames)

    for script in conflicts:
        plan.warnings.append(
            f"{script} is shared by steps whose template variables rename "
            "differently, so it was left as it is; update its variables yourself"
        )
        del per_script[script]

    return per_script


def rename_template_variables(sql: str, renames: dict[str, str]) -> str:
    """Rename `{old}` to `{new}` in a str.format template, leaving `{{...}}`
    and everything else as it is."""
    formatter = string.Formatter()
    parts = list(formatter.parse(sql))

    def rebuild(rename: dict[str, str]) -> str:
        out = []
        for literal, name, spec, conversion in parts:
            out.append(literal.replace("{", "{{").replace("}", "}}"))
            if name is None:
                continue
            base, rest = re.match(r"([^.\[]*)(.*)", name).groups()  # type: ignore[union-attr]
            out.append("{" + rename.get(base, base) + rest)
            if conversion:
                out.append(f"!{conversion}")
            if spec:
                out.append(f":{spec}")
            out.append("}")
        return "".join(out)

    if rebuild({}) != sql:
        # parsing lost something, e.g. an empty `{foo:}`; don't risk a rewrite
        raise ValueError("cannot rewrite this template faithfully")

    return rebuild(renames)


def _plan_config_updates(alcove: Alcove, plan: RenamePlan) -> list[Path]:
    "Table configs whose `inherit` keys name a version of the dataset."
    moved = dict(plan.moves)
    configs = set()
    for step in alcove.steps:
        if step.scheme == "snapshot":
            continue
        try:
            config = _get_executable(step, check=False).with_suffix(".meta.yaml")
        except FileNotFoundError:
            continue
        if config.exists() and _uri_pattern(plan.old).search(config.read_text()):
            configs.add(moved.get(config, config))
    return sorted(configs)


def _uri_pattern(dataset: StepURI) -> re.Pattern:
    "Matches the URI of any version of the dataset, e.g. table://foo/latest."
    return re.compile(
        rf"(?<![\w/]){re.escape(dataset.scheme)}://{re.escape(dataset.path)}"
        r"/(?P<version>[\w*-]+)(?![\w/-])"
    )


def _execute(alcove: Alcove, plan: RenamePlan) -> None:
    for src, dest in plan.moves:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest)
        _remove_empty_parents(src.parent)

    pattern = _uri_pattern(plan.old)
    replacement = f"{plan.new.scheme}://{plan.new.path}/\\g<version>"

    for metadata, old_uri, new_uri in plan.snapshot_metadata:
        text = metadata.read_text()
        metadata.write_text(text.replace(str(old_uri), str(new_uri)))

    for config in plan.configs:
        config.write_text(pattern.sub(replacement, config.read_text()))

    for script, renames in plan.sql_renames.items():
        try:
            script.write_text(rename_template_variables(script.read_text(), renames))
        except ValueError:
            changes = ", ".join(f"{{{a}}} to {{{b}}}" for a, b in renames.items())
            plan.warnings.append(
                f"Could not update {script}; change {changes} in it yourself"
            )

    if plan.old.scheme == "snapshot":
        _update_data_gitignore(plan)

    rename = _renamer(plan.old, plan.new)
    alcove.steps = {
        rename(step): [rename(d) for d in deps]
        for step, deps in alcove.steps.items()
        if step not in alcove.discovered
    }
    alcove.discovered = set()
    alcove.save()
    alcove.refresh()


def _remove_empty_parents(folder: Path) -> None:
    "Tidy away folders a move left empty, up to the top of their tree."
    stop = {Path("."), DATA_DIR, SNAPSHOT_DIR, TABLE_DIR, ARTIFACT_DIR}
    stop |= {_script_dir(StepURI(s, "x")) for s in ("table", "artifact")}
    while folder not in stop and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


def _update_data_gitignore(plan: RenamePlan) -> None:
    """Point the snapshot's data/.gitignore entries at its new folder, so its
    data stays out of git, and drop the old ones."""
    gitignore = DATA_DIR / ".gitignore"
    old_prefix = f"snapshots/{plan.old.path}/"
    new_prefix = f"snapshots/{plan.new.path}/"
    if gitignore.exists():
        lines = gitignore.read_text().splitlines()
        updated = []
        for line in lines:
            rest = line.removeprefix(old_prefix)
            # only the dataset's own entries, not a dataset nested inside it
            if rest != line and "/" not in rest.rstrip("/"):
                line = new_prefix + rest
            updated.append(line)
        if updated != lines:
            gitignore.write_text("".join(f"{line}\n" for line in updated))
            print_op("UPDATE", gitignore)

    if plan.partitioned:
        for extension in plan.extensions:
            add_pattern_to_data_gitignore(
                partition_gitignore_entry(plan.new.path, extension)
            )
    else:
        for _, _, new_uri in plan.snapshot_metadata:
            add_to_data_gitignore(Snapshot.load(new_uri.path).path)


def _references_left(plan: RenamePlan, skip: set[Path]) -> list[str]:
    """Lines in the project's own files that still mention the dataset by
    path, URI or DuckDB view name, as `path:line: text`."""
    old = plan.old.path
    patterns = [rf"(?<![\w-]){re.escape(old)}(?![\w-])"]
    if plan.old.scheme == "table":
        # views in `alcove db`, e.g. foo_bar_latest
        patterns.append(rf"\b{re.escape(old.replace('/', '_'))}_")
    regex = re.compile("|".join(patterns))

    found = []
    for path in _project_files():
        if path in skip or path.is_relative_to(TABLE_DIR):
            continue
        if path.is_relative_to(ARTIFACT_DIR):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                found.append(f"{path}:{i}: {line.strip()}")
    return found


def _project_files() -> list[Path]:
    "The files git tracks or would track, else everything outside data/."
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [Path(p) for p in result.stdout.splitlines() if Path(p).is_file()]
    except (FileNotFoundError, subprocess.CalledProcessError):
        return [
            p
            for p in Path(".").rglob("*")
            if p.is_file() and not p.parts[0].startswith(".") and p.parts[0] != "data"
        ]
