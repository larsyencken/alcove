"""Starter scripts for `alcove new-table`.

Each template runs as soon as it is written: it copies its first dependency
into the table, or writes an example table if there are no dependencies or the
first is in a format alcove doesn't know how to read as a table. Its header
comments name the variable that holds each dependency's path.
"""

from pathlib import Path
from typing import Literal

from alcove.core import Alcove
from alcove.paths import ARTIFACT_DIR, SNAPSHOT_DIR, TABLE_DIR
from alcove.snapshots import Snapshot
from alcove.table_metadata import _get_executable, _script_dir
from alcove.tables import _argument_groups, _simplify_dependency_names
from alcove.types import StepURI

Language = Literal["python", "sql", "nushell"]

LANGUAGES: dict[Language, str] = {"python": ".py", "sql": ".sql", "nushell": ".nu"}

# File formats every template can read: polars has a reader for each, and
# DuckDB recognises each by its extension
POLARS_READERS = {
    ".csv": "pl.read_csv({})",
    ".tsv": 'pl.read_csv({}, separator="\\t")',
    ".parquet": "pl.read_parquet({})",
    ".json": "pl.read_json({})",
    ".jsonl": "pl.read_ndjson({})",
    ".ndjson": "pl.read_ndjson({})",
}

EXAMPLE_ROWS = [("a", 1), ("b", 2), ("c", 3)]


def script_path(uri: StepURI, language: Language) -> Path:
    """Where the new script goes: `<path>.<ext>`, or for a wildcard table such
    as `table://foo/*`, the `foo.<ext>` shared by all its versions."""
    base = _script_dir(uri) / uri.path
    if uri.is_wildcard:
        base = base.parent
    return base.with_suffix(LANGUAGES[language])


def check_no_script(uri: StepURI) -> None:
    "Refuse to scaffold a step that already has a script of any kind."
    try:
        existing = _get_executable(uri, check=False)
    except FileNotFoundError:
        return
    raise ValueError(f"{uri} already has a script: {existing}")


def write_template(
    alcove: Alcove, uri: StepURI, dependencies: list[StepURI], language: Language
) -> Path:
    "Write a starter script for a table step, returning its path."
    assert uri.scheme == "table"
    check_no_script(uri)

    groups = _argument_groups(dependencies)
    labels = [_label(uri, group) for group in groups]
    readable = _readable_format(alcove, dependencies[0]) if dependencies else None
    glob = bool(groups) and _is_glob(uri, groups[0])
    if readable == ".json" and glob and language != "sql":
        # neither polars nor its nushell plugin reads a glob of JSON files
        readable = None

    # explain why a step with dependencies writes the example table instead
    fallback = (
        None
        if readable or not dependencies
        else f"alcove doesn't know how to read {dependencies[0]} as a table, so "
        "this writes an example table; replace it with code that reads it"
    )

    if language == "python":
        reader = POLARS_READERS[readable] if readable else None
        content = _python_template(uri, labels, reader, fallback)
    elif language == "sql":
        content = _sql_template(uri, groups, labels, fallback)
    else:
        content = _nushell_template(uri, labels, readable, glob, fallback)

    path = script_path(uri, language)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if language != "sql":
        path.chmod(0o755)

    return path


def _is_glob(uri: StepURI, group: list[StepURI]) -> bool:
    """Several versions, or a wildcard feeding a single table, arrive as a
    glob; artifacts never do."""
    dep = group[0]
    if dep.scheme == "artifact":
        return False
    return len(group) > 1 or (dep.is_wildcard and not uri.is_wildcard)


def _is_spread(uri: StepURI, group: list[StepURI]) -> bool:
    "A wildcard artifact feeding a single table arrives as one path per version."
    dep = group[0]
    return dep.scheme == "artifact" and dep.is_wildcard and not uri.is_wildcard


def _label(uri: StepURI, group: list[StepURI]) -> str:
    label = ", ".join(map(str, group))
    if _is_glob(uri, group):
        label += " (a glob over every version on disk)"
    elif _is_spread(uri, group):
        label += " (one path per version, from here on)"
    return label


def _readable_format(alcove: Alcove, dep: StepURI) -> str | None:
    "The extension of `dep`'s data, if every template can read it as a table."
    if dep.scheme == "table":
        return ".parquet"

    if dep.scheme == "snapshot":
        versions = alcove.versions(dep)
        if versions:
            snapshot = Snapshot.load(dep.with_version(versions[-1]).path)
            extension = (snapshot.extension or "").lower()
            if snapshot.snapshot_type == "file" and extension in POLARS_READERS:
                return extension

    return None


def _comment_rows(names: list[str], labels: list[str]) -> list[str]:
    width = max(map(len, names))
    return [f"{name.ljust(width)}  {label}" for name, label in zip(names, labels)]


def _wrap_comment(prefix: str, text: str, width: int = 76) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(prefix) + len(line) + 1 + len(word) > width:
            lines.append(prefix + line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    return lines + [prefix + line]


def _script_header(
    shebang: str, uri: StepURI, names: list[str], labels: list[str]
) -> list[str]:
    rows = _comment_rows(names, labels + ["the Parquet file to write"])
    return [
        shebang,
        "#",
        f"#  Builds {uri}. Alcove calls this script with the path of each",
        "#  dependency, then the Parquet file to write:",
        "#",
        *[f"#    {row}" for row in rows],
        "#",
    ]


def _python_template(
    uri: StepURI, labels: list[str], reader: str | None, fallback: str | None
) -> str:
    names = [f"deps[{i}]" for i in range(len(labels))] + ["output_file"]
    header = _script_header("#!/usr/bin/env python3", uri, names, labels)

    if reader:
        body = [f"df = {reader.format('deps[0]')}"]
    else:
        body = _wrap_comment("# ", fallback) if fallback else []
        dims = ", ".join(f'"{k}"' for k, _ in EXAMPLE_ROWS)
        values = ", ".join(str(v) for _, v in EXAMPLE_ROWS)
        body.append(f'df = pl.DataFrame({{"dim_key": [{dims}], "value": [{values}]}})')

    return "\n".join(
        header
        + [
            "import sys",
            "",
            "import polars as pl",
            "",
            "*deps, output_file = sys.argv[1:]",
            "",
            *body,
            "df.write_parquet(output_file)",
            "",
        ]
    )


def _argument_path(group: list[StepURI]) -> Path:
    """A path shaped like the one the SQL runner names a dependency from, which
    is all its name depends on: data/<kind>/<dataset>/<version>."""
    dep = group[0]
    glob = len(group) > 1 or dep.is_wildcard
    if dep.scheme == "snapshot":
        return SNAPSHOT_DIR / dep.base_path / ("*" if glob else dep.version)
    if dep.scheme == "table":
        return TABLE_DIR / dep.base_path / ("*" if glob else f"{dep.version}.parquet")
    return ARTIFACT_DIR / dep.path


def _name_unknown(
    uri: StepURI, group: list[StepURI], groups: list[list[StepURI]]
) -> bool:
    """Whether an artifact's template variable name depends on versions we
    can't see yet: a wildcard expanding to several paths, or any version of an
    artifact listed alongside its `latest`, whose names depend on the version
    `latest` resolves to."""
    dep = group[0]
    if dep.scheme != "artifact":
        return False
    if _is_spread(uri, group):
        return True
    # `latest` may resolve to another version listed beside it, and the two
    # then share one path and one name, so none of these names is certain
    return (
        any(
            g[0].scheme == "artifact"
            and g[0].base_path == dep.base_path
            and g[0].version == "latest"
            for g in groups
            if len(groups) > 1
        )
        and sum(
            g[0].scheme == "artifact" and g[0].base_path == dep.base_path
            for g in groups
        )
        > 1
    )


def _sql_template(
    uri: StepURI,
    groups: list[list[StepURI]],
    labels: list[str],
    fallback: str | None,
) -> str:
    lines = [
        f"-- Builds {uri}. Alcove runs this query with DuckDB and writes its",
        "-- result to the table.",
        "--",
    ]

    names: list[str] = []
    if groups:
        paths = [_argument_path(group) for group in groups]
        by_path = {
            path: name for name, path in _simplify_dependency_names(paths).items()
        }
        names = [
            # the runner formats this file, so a name it won't define must
            # not appear in braces
            f"{group[0].base_path.split('/')[-1]}_<version>"
            if _name_unknown(uri, group, groups)
            else "{" + by_path[path] + "}"
            for group, path in zip(groups, paths)
        ]
        lines += [
            "-- These template variables hold each dependency's path:",
            "--",
            *[f"--   {row}" for row in _comment_rows(names, labels)],
            "--",
            # the runner formats the whole file, comments included
            "-- Write any other braces in the query twice, as {{ and }}.",
            "--",
        ]
    else:
        lines += ["-- This table has no dependencies, so no template variables.", "--"]

    if fallback:
        lines += _wrap_comment("-- ", fallback)
    if groups and not fallback:
        lines.append(f"SELECT * FROM '{names[0]}'")
    else:
        values = ", ".join(f"('{k}', {v})" for k, v in EXAMPLE_ROWS)
        lines += ["SELECT *", f"FROM (VALUES {values}) AS t(dim_key, value)"]

    return "\n".join(lines) + "\n"


def _nushell_template(
    uri: StepURI,
    labels: list[str],
    readable: str | None,
    glob: bool,
    fallback: str | None,
) -> str:
    names = [f"$deps.{i}" for i in range(len(labels))] + ["$dest"]
    header = _script_header("#!/usr/bin/env nu", uri, names, labels)
    header += [
        "#  Needs nushell's polars plugin, registered with `plugin add`.",
        "#",
        "",
        "plugin use polars",
    ]

    if readable:
        flags = ' --delimiter "\\t"' if readable == ".tsv" else ""
        if glob:
            # an eager open can't read a glob, so read lazily and collect
            read = f"polars open{flags} $deps.0 | polars collect"
        else:
            read = f"polars open --eager{flags} $deps.0"
        body = [f"    {read} | polars save $dest"]
    else:
        body = _wrap_comment("    # ", fallback) if fallback else []
        rows = " ".join(f"[{k} {v}]" for k, v in EXAMPLE_ROWS)
        body.append(
            f"    [[dim_key value]; {rows}] | polars into-df | polars save $dest"
        )

    return "\n".join(
        header
        + [
            "",
            "# takes every argument as one list: nushell accepts `[...deps, dest]` but binds",
            "# the first argument to `dest`",
            "def main [...args: path] {",
            "    let dest = $args | last",
            "    let deps = $args | drop 1",
            "",
            *body,
            "}",
            "",
        ]
    )
