# Commands

## Command Reference

Alcove provides the following commands:

| Command | Description |
|---------|-------------|
| `alcove init` | Initialize a new alcove workspace |
| `alcove snapshot <path> <dataset>` | Add a file or directory to your alcove |
| `alcove run` | Build all tables and fetch outdated data |
| `alcove list` | List all datasets, dependencies first, with consecutive days collapsed into one range (`--full` lists each one) |
| `alcove audit` | Validate the alcove metadata |
| `alcove new-table <path> [deps...] --lang <lang>` | Create a new derived table |
| `alcove new-artifact <path> [deps...]` | Create a derived output that is not a table |
| `alcove db [query]` | Open a DuckDB shell or execute a query |
| `alcove export-duckdb <file>` | Export tables to a DuckDB file |

## Creating a new table

To create a new table, use the `new-table` command and pick the language of its script with `-l`/`--lang`:

```bash
alcove new-table <table-path> [dep1 [dep2 [...]]] --lang python|sql|nushell
```

This registers the table in `alcove.yaml` and writes a starter script to `src/steps/tables/<table-path>.py`, `.sql` or `.nu`. For a wildcard table such as `daily/*`, the script is `src/steps/tables/daily.py`, shared by every version. The script runs as written: with no dependencies it writes a small example table, and otherwise it copies its first dependency into the table. Its header comments say which variable holds each dependency's path. For example:

```bash
alcove new-table countries/latest snapshot://countries/latest --lang python
```

writes:

```python
#!/usr/bin/env python3
#
#  Builds table://countries/latest. Alcove calls this script with the path of each
#  dependency, then the Parquet file to write:
#
#    deps[0]      snapshot://countries/latest
#    output_file  the Parquet file to write
#
import sys

import polars as pl

*deps, output_file = sys.argv[1:]

df = pl.read_csv(deps[0])
df.write_parquet(output_file)
```

Python scripts read the dependency with polars, SQL scripts with DuckDB, and nushell scripts with nushell's polars plugin, which must be registered with `plugin add`. Templates copy tables and CSV, TSV, Parquet, JSON or NDJSON snapshots. For any other dependency, such as a directory snapshot, another file format or an artifact, the script writes the example table instead, with a comment saying to replace it.

## Creating an artifact

Not every derived output is a table. A rendered HTML dashboard, a fitted model or a bundle of charts is still a pure function of its dependencies, but has no place in DuckDB. Register these as artifact steps:

```bash
alcove new-artifact <artifact-path> [dep1 [dep2 [...]]]
```

For example:

```bash
alcove new-artifact reports/health/latest table://health/daily/latest
```

Write the build script at `src/steps/artifacts/<artifact-path>.py`. It is called with the path of each dependency, then an empty output directory, and must write at least one file into that directory:

```python
#!/usr/bin/env python3
import sys
from pathlib import Path
import polars as pl

*deps, out_dir = sys.argv[1:]
daily = pl.read_parquet(deps[0])

(Path(out_dir) / "index.html").write_text(f"<h1>{len(daily)} days</h1>")
```

The output lands in `data/artifacts/<artifact-path>/`, with a `.meta.yaml` sidecar recording a checksum of every file produced and of every input. Artifacts rebuild when any dependency or the script changes, can depend on snapshots, tables and other artifacts, and can be depended on by tables in turn. Artifact scripts must be Python or [nushell](#nushell-steps); they do not appear in `alcove db`.

## Steps as folders

A step can be a folder instead of a single file. Put a `__main__.py` entrypoint in `src/steps/tables/<path>/` (or `src/steps/artifacts/<path>/`) and alcove runs the folder with Python, exactly as `python <folder>` would. Anything else in the folder, such as a helper module, a lookup CSV or an HTML template, is part of the step: every file is checksummed into the step's input manifest, so editing, adding or removing one rebuilds the step. Bytecode caches (`__pycache__`) are ignored.

```
src/steps/artifacts/reports/health/latest/
├── __main__.py      # reads sys.argv, writes into the output directory
└── template.html    # read relative to __file__
```

A single `<path>.py`, `<path>.sql` or `<path>.nu` file takes precedence over a folder at the same path, and the folder takes precedence over a version-shared `<parent>.py`, `<parent>.sql` or `<parent>.nu`. The optional metadata config for a folder step sits beside it as `<path>.meta.yaml`, exactly as for a single-file step.

What counts as part of the folder: every regular file, following symlinked directories, so a template shared between steps through a symlink is still an input. Dangling symlinks are an error. Ignored: `__pycache__`, anything starting with a dot (editor swap files, `.ruff_cache`, `.DS_Store`), and editor backups ending in `~` or wrapped in `#`. Step scripts run with bytecode caching disabled and redirected, so a `__pycache__` left inside the folder by running the script by hand is neither read nor written.

## Nushell steps

A table or artifact step can be a nushell script, `<path>.nu`, which alcove runs with the `nu` on your `PATH`. Its arguments are the same as a Python step's: the path of each dependency, then the output path. Take them as one rest parameter and split off the last. Nushell doesn't stop you declaring a positional parameter after a rest parameter, but `def main [...deps: path, dest: path]` silently puts the first argument in `$dest`.

```nu
#!/usr/bin/env nu
plugin use polars

def main [...args: path] {
    let dest = $args | last
    let deps = $args | drop 1

    polars open --eager $deps.0
    | polars filter ((polars col value) > 0)
    | polars save $dest
}
```

Plain nushell can read CSV and JSON snapshots, but it can neither read nor write Parquet. So a step that depends on a table, and every table step's output, goes through the [polars plugin](https://www.nushell.sh/book/dataframes.html), as in the example, or the `duckdb` CLI. Several versions of one dependency, such as a wildcard's partitions, arrive as a single glob pattern like `data/tables/foo/*.parquet`. `polars open --eager` can't read a pattern. Open it lazily and collect it (`polars open $dep | polars collect`), or expand it with `glob $dep`. Plain `open` on a pattern returns nothing rather than failing. Nushell reads neither `config.nu` nor `env.nu` when it runs a script, so commands defined only in your shell config aren't available to a step; plugins registered with `plugin add` are. As with Python packages, the version of nushell and its plugins isn't part of a step's checksum, so upgrading them doesn't rebuild anything.

A `<path>.py` or `<path>.sql` script takes precedence over a `<path>.nu` beside it. Step folders need a `__main__.py` entrypoint, so a nushell step is always a single file: a module it loads with `use ./helpers.nu` isn't part of its checksum, and editing that module doesn't rebuild the step.

## Executing SQL step definitions

If a `.sql` step definition is detected, it will be executed using DuckDB with an in-memory database. The SQL file can use `{variable}` to interpolate template variables, one for each dependency:

- `{dependency}`: The path of each dependency, simplified to a semantic name.

## Building your alcove

Run the `run` command to fetch any data that's out of date and build any derived tables:

```bash
alcove run
```

It downloads the snapshot files it needs before building anything, eight at a time; `--jobs` (`-j`) sets how many.
