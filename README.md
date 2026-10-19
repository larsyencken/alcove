# alcove

[![CI](https://github.com/larsyencken/alcove/actions/workflows/ci.yml/badge.svg)](https://github.com/larsyencken/alcove/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

_A personal ETL and data lake._

## Overview

Alcove is an opinionated small-scale ETL framework for managing data files and directories in a content-addressable way.

## Core principles

- **A reusable framework.** Alcove provides a structured way of managing data files, scripts and their interdependencies that can be used across multiple projects.
- **First class metadata.** Every data file has an accompanying metadata sidecar that can be used to store provenance, licensing and other information.
- **Content addressed.** An `alcove` DAG is a Merkle tree of checksums that includes data, metadata and scripts, used to lazily rebuild only what is out of date.
- **Data versioning.** Every step in the DAG has a URI that includes a version, which can be an ISO date or `latest`, to encourage a reproducible workflow that still allows for change.
- **SQL support.** Alcove is a Python framework, but allows you to write steps in SQL which will be executed by DuckDB.
- **Parquet interchange.** All derived tables are generated as Parquet, which makes reuse easier.
- **Artifacts too.** Outputs that are not tables, such as rendered dashboards or fitted models, live in the same DAG as artifact steps.

## Quick Start

```bash
# Install alcove
pip install alcove  # or: uv add alcove

# Initialize a new alcove
mkdir my-data-project && cd my-data-project
alcove init

# Add a data file to your alcove
alcove snapshot ~/Downloads/countries.csv countries/latest

# Create a derived table
alcove new-table population/latest snapshot://countries/latest --lang sql

# Build all tables
alcove run

# Explore your data with DuckDB
alcove db
```

## Usage

### Install the package

You can install the `alcove` package from PyPI using pip, uv, or any other Python package manager:

```bash
# Using pip
pip install alcove

# Using uv (recommended)
uv add alcove

# For development
uv add --dev alcove
```

You can also install directly from GitHub for the latest development version:

```bash
pip install git+https://github.com/larsyencken/alcove
```

### Using Alcove in your project

#### Starting a new project

To start a new project with alcove:

```bash
# Create and navigate to your project directory
mkdir my-data-project
cd my-data-project

# Set up your Python environment (optional, but recommended)
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install alcove
pip install alcove

# Or with uv
uv add alcove

# Initialize the alcove
alcove init
```

#### Adding to an existing project

To add alcove to an existing project:

```bash
# Navigate to your project directory
cd your-project

# Install alcove
uv add alcove   # Or pip install alcove

# Initialize alcove in a subdirectory (optional)
mkdir data
cd data
alcove init
```

### Initialise an alcove

From the folder where you want to store your data and metadata, run:

```bash
alcove init
```

This will create a `alcove.yaml` file, which will serve as the catalogue of all the data in your alcove.

### Choose where data is stored

By default, alcove keeps snapshot data in an S3-compatible bucket. You can choose where when you initialise the alcove:

```bash
alcove init                   # an S3-compatible bucket (the default)
alcove init --storage local   # a directory on this machine, .alcove/blobs by default
alcove init --storage git     # the git repo the alcove is in
```

The choice is saved as `storage` in `alcove.yaml`, so everyone working on the alcove uses the same store. An `alcove.yaml` without a `storage` key uses S3.

**S3.** Configure your S3-compatible storage credentials in a `.env` file, in the same directory as your `alcove.yaml` file. Define:

```
S3_ACCESS_KEY=your_application_key_id
S3_SECRET_KEY=your_application_key
S3_BUCKET_NAME=your_bucket_name
S3_ENDPOINT_URL=your_endpoint_url
```

Files downloaded from S3 are also cached in `~/.cache/alcove`.

**Local.** Data is kept in a directory, laid out as in the bucket, with no credentials or network needed. `--path` picks another directory, relative to `alcove.yaml`. The default `.alcove/` is added to `.gitignore`; a directory you choose isn't. To keep the store elsewhere on just one machine, such as on an encrypted volume, set `ALCOVE_LOCAL_STORE=/path/to/store` in `.env`. Local storage doesn't use the `~/.cache/alcove` cache.

**Git.** Data is kept in the repo's own object store, byte for byte, under the ref `refs/alcove/blobs`; no branch is touched. Plain `git clone` and `git push` don't copy that ref, so the data leaves the machine only if you ask for it:

```bash
git push origin 'refs/alcove/*:refs/alcove/*'    # share the data
git fetch origin 'refs/alcove/*:refs/alcove/*'   # get it in another clone
git bundle create incident.bundle --all          # code and data in one file
```

Git storage suits small to medium data. Git compresses each file as it's stored, which is slow for large ones (about 18 s per GB), and reading a file back needs memory about the size of the file, for each of the files `alcove run` fetches at once (`--jobs`). Hosts also limit file size (100 MB on GitHub). Git storage doesn't use the `~/.cache/alcove` cache either.

Now your alcove is ready to use.

### Adding a file or folder

From within your alcove folder, use the `snapshot` command to add a file or folder to your alcove:

```bash
alcove snapshot path/to/your/file_or_folder dataset_name
```

For example:

```bash
alcove snapshot ~/Downloads/countries.csv countries/latest
```

This will add the file to your alcove's store, and create a metadata file at `data/<dataset_name>.meta.yaml` directory for you to complete.

The metadata format has some minimum fields, but is meant for you to extend as needed for your own purposes. Best practice would be to retain the provenance and licence information of any data you add to your alcove, especially if it originates from a third party.

### Creating a new table

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

### Creating an artifact

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

### Steps as folders

A step can be a folder instead of a single file. Put a `__main__.py` entrypoint in `src/steps/tables/<path>/` (or `src/steps/artifacts/<path>/`) and alcove runs the folder with Python, exactly as `python <folder>` would. Anything else in the folder, such as a helper module, a lookup CSV or an HTML template, is part of the step: every file is checksummed into the step's input manifest, so editing, adding or removing one rebuilds the step. Bytecode caches (`__pycache__`) are ignored.

```
src/steps/artifacts/reports/health/latest/
├── __main__.py      # reads sys.argv, writes into the output directory
└── template.html    # read relative to __file__
```

A single `<path>.py`, `<path>.sql` or `<path>.nu` file takes precedence over a folder at the same path, and the folder takes precedence over a version-shared `<parent>.py`, `<parent>.sql` or `<parent>.nu`. The optional metadata config for a folder step sits beside it as `<path>.meta.yaml`, exactly as for a single-file step.

What counts as part of the folder: every regular file, following symlinked directories, so a template shared between steps through a symlink is still an input. Dangling symlinks are an error. Ignored: `__pycache__`, anything starting with a dot (editor swap files, `.ruff_cache`, `.DS_Store`), and editor backups ending in `~` or wrapped in `#`. Step scripts run with bytecode caching disabled and redirected, so a `__pycache__` left inside the folder by running the script by hand is neither read nor written.

### Nushell steps

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

### Executing SQL step definitions

If a `.sql` step definition is detected, it will be executed using DuckDB with an in-memory database. The SQL file can use `{variable}` to interpolate template variables, one for each dependency:

- `{dependency}`: The path of each dependency, simplified to a semantic name.

### Command Reference

Alcove provides the following commands:

- `alcove init` - Initialize a new alcove workspace
- `alcove snapshot <path> <dataset>` - Add a file or directory to your alcove
- `alcove run` - Build all tables and fetch outdated data
- `alcove list` - List all datasets, dependencies first, with consecutive days collapsed into one range (`--full` lists each one)
- `alcove audit` - Validate the alcove metadata
- `alcove new-table <path> [deps...] --lang <lang>` - Create a new derived table
- `alcove new-artifact <path> [deps...]` - Create a derived output that is not a table
- `alcove rename <old> <new>` - Rename a snapshot, table or artifact, and update what depends on it
- `alcove db [query]` - Open a DuckDB shell or execute a query
- `alcove export-duckdb <file>` - Export tables to a DuckDB file

### Building your alcove

Run the `run` command to fetch any data that's out of date and build any derived tables:

```bash
alcove run
```

It downloads the snapshot files it needs before building anything, eight at a time; `--jobs` (`-j`) sets how many.

## Development

### Testing with MinIO

For testing with S3-compatible storage, this project uses automatically managed containers:

```bash
# Run tests with Docker-based MinIO
make test
```

All tests require Docker with MinIO container to be available.

### Docker Context Support

The testing framework automatically detects your current Docker context and uses it for container operations. This ensures tests work properly with:

- Docker Desktop
- Colima
- OrbStack
- Remote Docker contexts

### MinIO Configuration

With Docker, these credentials are automatically used:

- Access Key: minioadmin
- Secret Key: minioadmin
- Bucket: test-bucket
- Endpoint: <http://localhost:9000>

Containers are automatically managed and kept running between test runs for performance.
MinIO's health is verified before tests run to ensure proper S3 compatibility.

## Bugs

Please report any issues at: <https://github.com/larsyencken/alcove/issues>

## Changelog

- `dev`
  - Added `alcove rename <old> <new>`, which renames every version of a snapshot, table or artifact, and updates `alcove.yaml`, its scripts or snapshot metadata and data, the template variables of SQL steps that read it, and its URIs in other step scripts and configs; it lists the lines in other files that still mention the old name (see [Renaming a dataset](docs/commands.md#renaming-a-dataset))
  - When a step's Python or nushell script fails, `alcove run` and `alcove export-duckdb` exit with the script's own error message and exit code, instead of following it with a Python traceback ending in `CalledProcessError`; a script killed by a signal gets a one-line `killed by SIGKILL`-style error and exit code 128+N
  - `alcove snapshot` rejects names that tables could not depend on (hyphens outside the final version segment, e.g. `secrets/h100-prime-002/2026-10-15-1900`) before uploading anything, suggesting the underscored name; previously it accepted them and `alcove new-table` later failed with a schema traceback. `new-table` and `new-artifact` give the same error for such dependencies, and now also refuse such names for the new step itself. `alcove.yaml` and step metadata files now reject such names when loaded, so a project that has them fails with a schema error until they are renamed
  - Snapshot data can be kept in a local directory (`alcove init --storage local`) or in the git repo itself under `refs/alcove/blobs` (`alcove init --storage git`), as well as in S3; the choice is saved as `storage` in `alcove.yaml`, and neither needs S3 credentials or uses `~/.cache/alcove`
  - Saving `alcove.yaml` keeps settings other than `steps`; previously it dropped them, including `data_dir`
  - A snapshot folder left without files by a failed download, even if it still has subfolders, is fetched again; previously the next `alcove run` failed with `No files found ... to checksum`
  - Fetching a folder snapshot that has subfolders over an existing copy no longer fails trying to delete those subfolders as stray files
  - `.env` is read from the directory alcove runs in (or the nearest parent with one), wherever alcove is installed; previously it was looked for beside the `alcove` script, so an alcove whose virtualenv lived elsewhere didn't see its `.env`
  - `alcove list` collapses each run of consecutive daily versions of a dataset into one line, e.g. `snapshot://gpu/usage/[2026-01-01 -> 2026-10-07]`, and lists datasets after the datasets they depend on; `--full` lists every version and `--alphabetical` restores the old order. `--paths` still lists every path. `list_steps()` in Python also returns dependency order unless given `alphabetical=True`
  - Table and artifact steps can be nushell scripts, `<path>.nu`, run with the `nu` on your `PATH` and given the same arguments as Python steps (see [Nushell steps](#nushell-steps))
  - `alcove new-table` requires `--lang python|sql|nushell` and writes a starter script in that language that runs as written: it copies the first dependency into the table, or writes an example table if there are none or alcove can't read the first, and its comments name the variable holding each dependency's path. Previously it wrote no script
  - Removed the `{output_file}` SQL template variable from the docs; SQL steps never had it
  - SQL template variables tell apart dependencies that share a name, such as `snapshot://countries/latest` and `table://countries/latest` (now `{snapshots_countries}` and `{tables_countries}`), and a dataset named with digits, such as `who/1990`, gets a name `str.format` accepts (`{who_1990}`). Previously the second dependency silently took the first one's name, and an all-digit name failed. A SQL step that used the shared name (`{countries}`) must switch to the new names
  - Removed `alcove new-table --edit`, which did nothing
- `0.5.1` (2026-10-09)
  - `alcove run` downloads snapshot files several at a time, 8 by default (`--jobs`/`-j`), and downloads each distinct file once however many snapshots hold it; previously it downloaded one file at a time, making a new S3 client for each. A failed download or Ctrl-C abandons the downloads still in progress rather than waiting for them
  - A file is added to the local cache under a temporary name and renamed into place, so an interrupted run can't leave a partial file that later reads as a cache hit (it can leave a `*.partial` file beside it, which is never read)
  - Requires boto3 1.36 or later. 0.5.0 already needed it, since older versions reject the checksum settings alcove passes for B2 ([#60](https://github.com/larsyencken/alcove/issues/60)), but didn't say so
- `0.5.0` (2026-10-07)
  - Snapshot datasets declared once as `snapshot://foo/*` are date-partitioned: each day is snapshotted as `foo/YYYY-MM-DD`, is discovered from its metadata file rather than listed in `alcove.yaml`, records when it was fetched, and is covered by a single `data/.gitignore` pattern; `alcove run` stops and names any day missing between a dataset's first partition and its last (see [Wildcards](docs/wildcards.md#daily-partitions))
  - `alcove run` moves partition-shaped snapshot data that has no metadata (such as a partition dropped on another clone, or missing from an older branch) into the dataset's `.orphaned/` folder rather than letting its glob read it, restores it when a partition with that content returns, and deletes per-partition tables and artifacts whose version no longer exists; `--dry-run` reports what it would do
  - A wildcard step with wildcard dependencies is built for the versions they all have; previously it took the first dependency's versions (so a lagging second dependency failed with a `KeyError`), and another step referring to one of its versions directly narrowed it to that version
  - A declared partitioned dataset with no partitions yet, and wildcard steps fed by it, expand to no steps; `AlcoveDB` skips union views for wildcard tables with nothing built
  - `alcove audit` warns about snapshot metadata files that no step refers to
  - Fixed tables and artifacts not rebuilding when a `latest` dependency resolves to a newer version, or when a wildcard dependency gains or loses a partition ([#70](https://github.com/larsyencken/alcove/issues/70)); steps that were stale this way rebuild on the first run after upgrading
  - Fixed `latest` resolving to a sibling dataset that shares a name prefix, and failing with a bare `max()` error when there are no versions ([#69](https://github.com/larsyencken/alcove/issues/69))
  - Changed the template value for several single-file snapshot versions to include their extension (`data/snapshots/foo/????-??-??.csv`), matching what a single version already gave, so one recipe (`'{foo}'`) works for both; recipes that appended the extension themselves (`'{foo}.csv'`) need updating
  - Added `Alcove.versions()` to list a dataset's versions

- `0.4.0` (2026-09-22)
  - Added `artifact://` steps for derived outputs that are not tables (e.g. rendered dashboards, models), built by a Python script into `data/artifacts/<path>/`
  - Added `alcove new-artifact <path> [deps...]`
  - Steps can be folders with a `__main__.py` entrypoint; every file in the folder (following symlinks, skipping caches and editor debris) is checksummed as an input, so templates and helper modules trigger rebuilds, and step scripts run with bytecode caching disabled
  - Fixed config validation rejecting ISO-date versions (e.g. `snapshot://foo/2024-09-04`) as dependencies; hyphens are now allowed in the version segment only

- `0.3.0`
  - Added wildcard `*` support in step URIs for date-partitioned tables (e.g. `table://foo/*` expands per snapshot version)
  - `AlcoveDB` now registers union views across all partitions of a wildcard group
  - Fixed DAG mutation bug in `plan_and_run` (steps dict is now copied before modification)
  - Table names now require underscores instead of dashes
  - Added documentation site with Zensical

- `0.2.2`
  - Fixed `snapshot --force` failing with `FileExistsError` when overwriting directory snapshots
  - Added programmatic API: `alcove.connect()` returns an `AlcoveDB` that queries tables as Polars DataFrames
  - Enforced `dim_` columns as composite primary key when new tables are generated

- `0.2.1` (2025-04-28)
  - Fixed gitignore handling by using `data/.gitignore` instead of `.data-files`
  - Always include `tables/` in `data/.gitignore`
  - `alcove audit --fix` now migrates patterns from `.gitignore` and `.data-files` to `data/.gitignore`

- `0.2.0` (2025-04-28)
  - Added `.data-files` file for managing alcove data ignores (#61)
  - `alcove init` now creates empty `.data-files` and ensures it's in `.gitignore`
  - `alcove audit --fix` can move patterns from `.gitignore` to `.data-files`
  - Prevents `.gitignore` from changing frequently with data file updates

- `0.1.2` (2025-04-25)
  - Fixed B2 compatibility with recent boto3 versions by disabling checksum validation (#60)
  - Simplified testing approach by always requiring Docker with MinIO
  - Added PyPI package configuration and installation instructions
  - Improved documentation with quick start guide and command reference

- `0.1.1` (2025-04-25)
  - Renamed project from "shelf" to "alcove"
  - Added automated Docker container management for testing with MinIO
  - Enhanced Docker context support for different environments (Docker Desktop, Colima, OrbStack)
  - Improved S3-compatible storage testing reliability
  - Fixed test fixtures to use consistent credentials

- `0.1.0` (Initial release)
  - Initialise a repo with `shelf.yaml`
  - `shelf snapshot` and `shelf run` with file and directory support
  - Only fetch things that are out of date
  - `shelf list` to see what datasets are available
  - `shelf audit` to ensure your alcove is coherent and correct
  - `shelf db` to enter an interactive DuckDB shell with all your data
