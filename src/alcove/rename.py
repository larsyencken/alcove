"""Renaming a dataset: every version of a snapshot, table or artifact.

`alcove rename table://foo table://bar` changes everything alcove itself
keeps under the old name:

- the dataset's steps and every dependency on them in alcove.yaml
- the scripts that build a table or artifact, with their `.meta.yaml`
  configs and any other files in the dataset's script folder
- snapshot metadata, and any fetched snapshot data and data/.gitignore entries
- built tables and artifacts, which then rebuild on the next `alcove run`,
  since the inputs recorded in their metadata no longer exist
- template variables in the SQL steps that read the dataset, e.g. `{foo}`
  becomes `{bar}`
- the dataset's URIs, e.g. `table://foo/latest`, in table and artifact
  scripts and configs, such as `inherit` keys

Text it does not manage, such as a Python script that reads a table by its
path or a snapshot script that names its dataset, is left as it is; the
lines that still mention the old name are listed at the end.
"""

import os
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
from alcove.table_metadata import _get_executable, _is_step_debris, _script_dir
from alcove.tables import (
    _argument_groups,
    _dependency_glob_path,
    _dependency_path,
    _simplify_dependency_names,
)
from alcove.types import SCHEMES, Dag, StepURI
from alcove.utils import add_pattern_to_data_gitignore, add_to_data_gitignore, print_op
from alcove.wildcards import expand_wildcards

DATA_DIRS = {"snapshot": SNAPSHOT_DIR, "table": TABLE_DIR, "artifact": ARTIFACT_DIR}
SCRIPT_SUFFIXES = (".py", ".sql", ".nu", ".meta.yaml")
SCRIPT_DIRS = [_script_dir(StepURI(s, "x")) for s in ("table", "artifact")]


@dataclass
class RenamePlan:
    old: StepURI
    new: StepURI
    # (from, to), in the order they are made
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    # snapshot metadata, at its new path, and the uri it changes from and to
    snapshot_metadata: dict[Path, tuple[StepURI, StepURI]] = field(default_factory=dict)
    # SQL scripts, at their new path, and the template variables to rename
    sql_renames: dict[Path, dict[str, str]] = field(default_factory=dict)
    # scripts and configs, at their new path, that name a version's URI
    uri_files: set[Path] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    # datasets nested inside the old name, e.g. `bar` for foo/bar/latest
    nested: set[str] = field(default_factory=set)
    # for a snapshot: is it partitioned, and the extensions of its data
    partitioned: bool = False
    extensions: set[str | None] = field(default_factory=set)

    def moved(self, path: Path) -> Path:
        "Where a file will be once the rename is done."
        for src, dest in self.moves:
            if path == src or path.is_relative_to(src):
                return dest / path.relative_to(src)
        return path

    @property
    def rewritten(self) -> list[Path]:
        "Files whose text changes, at their new path."
        return sorted({*self.snapshot_metadata, *self.uri_files, *self.sql_renames})


def rename_dataset(alcove: Alcove, old: str, new: str, dry_run: bool = False) -> None:
    "Rename every version of a dataset, e.g. `table://foo` to `table://bar`."
    plan = plan_rename(alcove, old, new)

    for src, dest in plan.moves:
        print_op("MOVE", f"{src} -> {dest}")

    if dry_run:
        for path in plan.rewritten:
            print_op("UPDATE", _describe_update(plan, path))
        print_op("UPDATE", alcove.config_file)
    else:
        _execute(alcove, plan)

    for warning in plan.warnings:
        print(f"WARNING: {warning}")

    leftovers = _references_left(plan, alcove.config_file, dry_run)
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
    every_uri = {*alcove.steps, *(d for ds in alcove.steps.values() for d in ds)}

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

    taken = sorted(s for s in every_uri if _in_dataset(s, new_uri))
    if taken:
        raise ValueError(f"{new_uri} already exists in this alcove: {taken[0]}")

    for step in steps:
        check_step_uri(str(rename(step)))

    versions = {s.version for s in steps if not s.is_wildcard}
    plan.nested = _nested(old_uri, every_uri)
    if versions & plan.nested:
        raise ValueError(
            f"{old_uri} has versions named like datasets nested inside it "
            f"({', '.join(sorted(versions & plan.nested))}), so their files "
            "can't be told apart; rename it by hand"
        )

    try:
        dag = _expanded_dag(alcove)
    except ValueError as e:
        raise ValueError(
            f"Cannot work out what each step reads, to update SQL template "
            f"variables: {e}"
        )

    if old_uri.scheme == "snapshot":
        _plan_snapshot_metadata(alcove, steps, plan)

    data_root = DATA_DIRS[old_uri.scheme]
    folder = data_root / old_uri.path
    # tables and artifacts built per partition of a wildcard are not steps in
    # alcove.yaml, so their versions are found from their metadata files
    built = (
        {p.name.removesuffix(".meta.yaml") for p in folder.glob("*.meta.yaml")}
        if folder.is_dir()
        else set()
    )
    _plan_folder_moves(data_root, versions | built, plan)
    _check_free(data_root, _nested(new_uri, every_uri), plan)

    if old_uri.scheme != "snapshot":
        script_root = _script_dir(old_uri)
        _plan_folder_moves(script_root, versions, plan)
        _check_free(script_root, _nested(new_uri, every_uri), plan)
        # a script shared by every version, e.g. foo.sql for table://foo/*
        for suffix in SCRIPT_SUFFIXES:
            shared = script_root / f"{old_uri.path}{suffix}"
            if shared.exists():
                plan.moves.append((shared, script_root / f"{new_uri.path}{suffix}"))

    # check every destination up front, so a rename never stops half done
    for src, dest in plan.moves:
        if dest.exists() or dest.is_symlink():
            raise ValueError(f"Cannot move {src} to {dest}: it already exists")
        if dest.is_relative_to(src):
            raise ValueError(f"Cannot move {src} into itself, to {dest}")

    plan.sql_renames = _plan_sql_renames(dag, plan)
    plan.uri_files = _plan_uri_updates(plan)
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


def _nested(dataset: StepURI, uris: set[StepURI]) -> set[str]:
    "The folders inside a dataset's folder that hold other datasets."
    prefix = f"{dataset.path}/"
    return {
        s.path.removeprefix(prefix).split("/")[0]
        for s in uris
        if s.scheme == dataset.scheme
        and s.path.startswith(prefix)
        and not _in_dataset(s, dataset)
    }


def _renamer(old: StepURI, new: StepURI):
    def rename(step: StepURI) -> StepURI:
        if _in_dataset(step, old):
            return _version(new, step.version)
        return step

    return rename


def _plan_snapshot_metadata(alcove: Alcove, steps: list[StepURI], plan: RenamePlan):
    plan.partitioned = _version(plan.old, "*") in alcove.steps
    for step in sorted(steps):
        if step.is_wildcard:
            continue

        snapshot = Snapshot.load(step.path)
        plan.extensions.add(
            snapshot.extension if snapshot.snapshot_type == "file" else None
        )
        new_uri = _version(plan.new, step.version)
        new_metadata = SNAPSHOT_DIR / f"{new_uri.path}.meta.yaml"
        plan.snapshot_metadata[new_metadata] = (step, new_uri)


def _plan_folder_moves(root: Path, versions: set[str], plan: RenamePlan) -> None:
    """Move what's in the dataset's folder under root to its new folder.

    With no other dataset nested inside it, the folder is all the dataset's,
    so everything in it moves, helper modules and set-aside data included.
    Otherwise only each version's own files and folders move.
    """
    folder = root / plan.old.path
    if not folder.is_dir():
        return

    for entry in sorted(folder.iterdir()):
        if entry.name in plan.nested:
            continue
        own = entry.name.split(".")[0] in versions or entry.name == ORPHANED_DIR
        if plan.nested and not own:
            if not _is_step_debris(entry.name):
                plan.warnings.append(
                    f"{entry} was left where it is, since datasets nested in "
                    f"{plan.old} share its folder; move it yourself if "
                    f"{plan.new} needs it"
                )
            continue
        plan.moves.append((entry, root / plan.new.path / entry.name))


def _check_free(root: Path, nested_new: set[str], plan: RenamePlan) -> None:
    """Refuse to move into a folder that already holds files, which the
    renamed dataset would pick up as its own: a stray script, or metadata of
    a snapshot that alcove.yaml doesn't name."""
    folder = root / plan.new.path
    sources = {src for src, _ in plan.moves}
    clutter = []
    if folder.is_dir():
        clutter = [
            p
            for p in sorted(folder.iterdir())
            if p.name not in nested_new
            and p not in sources
            and not _is_step_debris(p.name)
        ]
    if root in SCRIPT_DIRS:
        clutter += [
            p
            for suffix in SCRIPT_SUFFIXES
            if (p := root / f"{plan.new.path}{suffix}").exists()
        ]
    if clutter:
        listed = ", ".join(map(str, clutter[:3]))
        if len(clutter) > 3:
            listed += f" and {len(clutter) - 3} more"
        raise ValueError(
            f"Cannot rename {plan.old} to {plan.new}: {listed} already "
            "exists, and would be taken as part of it; move it away first"
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


def _plan_sql_renames(dag: Dag, plan: RenamePlan) -> dict[Path, dict[str, str]]:
    """The template variables each SQL script must rename, keyed by where the
    script will be.

    A dependency's variable is named after its path, so renaming the dataset
    renames it, and can rename another dependency's variable too, if the new
    name clashes with it or the old one did. A script shared by several steps
    is only changed if they all rename its variables the same way.
    """
    root = DATA_DIRS[plan.old.scheme]
    old_dir = root / plan.old.path

    def renamed(path: Path) -> Path:
        if path.parent == old_dir:
            return root / plan.new.path / path.name
        return path

    per_script: dict[Path, list[dict[str, str]]] = {}
    for step, deps in dag.items():
        if step.scheme == "snapshot":
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
        per_script.setdefault(script, []).append(
            {
                before_names[b]: after_names[a]
                for b, a in zip(before, after)
                if before_names[b] != after_names[a]
            }
        )

    sql_renames = {}
    for script, renames in per_script.items():
        if not any(renames):
            continue
        if any(r != renames[0] for r in renames):
            plan.warnings.append(
                f"{script} is shared by steps whose template variables would "
                "rename differently, or not at all, so it was left as it is; "
                "update its variables yourself"
            )
            continue
        sql_renames[plan.moved(script)] = renames[0]

    return sql_renames


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


def _plan_uri_updates(plan: RenamePlan) -> set[Path]:
    """Table and artifact scripts and configs naming a version of the dataset
    by its URI, e.g. an `inherit` key, or the header of a starter script."""
    pattern = _uri_pattern(plan.old)
    found = set()
    for script_dir in SCRIPT_DIRS:
        for path in script_dir.rglob("*") if script_dir.is_dir() else []:
            if not path.is_file() or not path.name.endswith(SCRIPT_SUFFIXES):
                continue
            try:
                if pattern.search(path.read_text()):
                    found.add(plan.moved(path))
            except (UnicodeDecodeError, OSError):
                continue
    return found


def _uri_pattern(dataset: StepURI) -> re.Pattern:
    "Matches the URI of any version of the dataset, e.g. table://foo/latest."
    return re.compile(
        rf"(?<![\w/]){re.escape(dataset.scheme)}://{re.escape(dataset.path)}"
        r"/(?P<version>[\w*-]+)(?![\w/-])"
    )


def _rewrite(plan: RenamePlan, path: Path, text: str) -> str:
    "The text of a file, at its new path, as the rename leaves it."
    if path in plan.snapshot_metadata:
        old_uri, new_uri = plan.snapshot_metadata[path]
        text = text.replace(str(old_uri), str(new_uri))

    if path in plan.uri_files:
        replacement = f"{plan.new.scheme}://{plan.new.path}/\\g<version>"
        text = _uri_pattern(plan.old).sub(replacement, text)

    if path in plan.sql_renames:
        text = rename_template_variables(text, plan.sql_renames[path])

    return text


def _describe_update(plan: RenamePlan, path: Path) -> str:
    if path not in plan.sql_renames:
        return str(path)
    changes = ", ".join(
        f"{{{a}}} -> {{{b}}}" for a, b in plan.sql_renames[path].items()
    )
    return f"{path} ({changes})"


def _execute(alcove: Alcove, plan: RenamePlan) -> None:
    for src, dest in plan.moves:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest)
        _remove_empty_parents(src.parent)

    for path in plan.rewritten:
        try:
            path.write_text(_rewrite(plan, path, path.read_text()))
        except ValueError:
            changes = ", ".join(
                f"{{{a}}} to {{{b}}}" for a, b in plan.sql_renames[path].items()
            )
            plan.warnings.append(
                f"Could not update {path}; change {changes} in it yourself"
            )
            continue
        print_op("UPDATE", _describe_update(plan, path))

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
    stop = {Path("."), DATA_DIR, *DATA_DIRS.values(), *SCRIPT_DIRS}
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
        for _, new_uri in plan.snapshot_metadata.values():
            add_to_data_gitignore(Snapshot.load(new_uri.path).path)


def _references_left(plan: RenamePlan, config_file: Path, dry_run: bool) -> list[str]:
    """Lines in the project's own files that still mention the dataset by
    path, URI or DuckDB view name, as `path:line: text`. For a dry run, they
    are the lines the rename would leave."""
    old = plan.old.path
    # not the same name under another scheme, e.g. snapshot://foo for
    # table://foo, nor a dataset nested inside it, nor a template variable
    others = [s for s in SCHEMES if s != plan.old.scheme]
    not_after = "".join(rf"(?<!{s}://)(?<!{s}s/)" for s in others)
    not_nested = (
        rf"(?!/(?:{'|'.join(map(re.escape, sorted(plan.nested)))})(?![\w-]))"
        if plan.nested
        else ""
    )
    patterns = [rf"(?<![\w{{-]){not_after}{re.escape(old)}(?![\w}}-]){not_nested}"]
    if plan.old.scheme == "table":
        # views in `alcove db`, e.g. foo_bar_latest
        patterns.append(rf"\b{re.escape(old.replace('/', '_'))}_")
    regex = re.compile("|".join(patterns))

    skip = {config_file, DATA_DIR / ".gitignore"}
    found = []
    for source in _project_files():
        path = plan.moved(source) if dry_run else source
        if path in skip or path.is_relative_to(TABLE_DIR):
            continue
        if path.is_relative_to(ARTIFACT_DIR):
            continue
        try:
            text = source.read_text()
            if dry_run:
                text = _rewrite(plan, path, text)
        except (UnicodeDecodeError, ValueError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            # the new name may contain the old one, e.g. foo -> archive/foo
            if regex.search(line.replace(plan.new.path, "")):
                found.append(f"{path}:{i}: {line.strip()}")
    return found


def _project_files() -> list[Path]:
    "The files git tracks or would track, else everything outside data/."
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
        )
        paths = [Path(p) for p in result.stdout.split("\0") if p]
        return [p for p in paths if p.is_file()]
    except (FileNotFoundError, subprocess.CalledProcessError):
        files = []
        for folder, dirs, names in os.walk("."):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in ("data", "node_modules")
                and d != "__pycache__"
            ]
            files += [Path(folder, n) for n in names]
        return [Path(os.path.normpath(p)) for p in files]
