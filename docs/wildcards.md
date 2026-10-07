# Wildcards

Alcove supports wildcard `*` steps for date-partitioned tables, allowing you to write a single script that gets reused across many versions of a dataset.

## How it works

A wildcard step URI like `table://foo/*` tells Alcove to expand the step into one concrete step per discovered version. Versions are discovered by scanning the DAG for concrete steps that share the same base path.

For example, if you have snapshots `snapshot://raw/2025-01-01`, `snapshot://raw/2025-02-01`, and `snapshot://raw/2025-03-01`, a wildcard table `table://clean/*` that depends on `snapshot://raw/*` will automatically expand into three concrete steps: `table://clean/2025-01-01`, `table://clean/2025-02-01`, and `table://clean/2025-03-01`.

## Configuration

Define wildcard steps in your `alcove.yaml` using `*` as the version:

```yaml
version: 1
steps:
  # Concrete snapshots with specific versions
  snapshot://raw/2025-01-01: []
  snapshot://raw/2025-02-01: []
  snapshot://raw/2025-03-01: []

  # Wildcard table: one script runs per version
  table://clean/*:
  - snapshot://raw/*

  # Another wildcard table that chains off the first
  table://summary/*:
  - table://clean/*
```

The build script for `table://clean/*` is written once and reused for each version. During `alcove run`, the wildcard expands and the script executes once per version, receiving the correct versioned dependency paths.

## Chained wildcards

Wildcard steps can depend on other wildcard steps. In the example above, `table://summary/*` depends on `table://clean/*`. Alcove processes wildcards in topological order, so `clean/*` is expanded first, and `summary/*` inherits the same set of versions.

## Union views in AlcoveDB

When you query wildcard tables through `AlcoveDB`, Alcove automatically creates a union view that combines all partitions. For a wildcard group `table://clean/*`, the view reads all matching Parquet files via `data/tables/clean/*.parquet`, giving you a single table with all versions combined.

```python
import alcove

db = alcove.connect()
# Query across all partitions at once
df = db.sql("SELECT * FROM clean")
```

## Daily partitions

For data that arrives a day at a time, such as usage metrics, billing exports or a daily API pull, snapshot each day separately and let tables read them together. Listing every day in `alcove.yaml` would mean editing it on every run, so a partitioned dataset is declared once instead.

### Declare the dataset once

```yaml
version: 1
steps:
  snapshot://gpu/usage/*: []
```

### Snapshot one day at a time

```bash
alcove snapshot usage-2026-10-06/ gpu/usage/2026-10-06
```

or from Python:

```python
from pathlib import Path
import alcove

alcove.snapshot_to_alcove(Path("usage-2026-10-06"), "gpu/usage/2026-10-06")
```

Each day is an ordinary snapshot, with these differences:

- **It is named by a date, which you must give.** Use the date its data covers, or for a pull that covers a trailing window, the window's last day (its as-of date). The name must be a real ISO date (`YYYY-MM-DD`); alcove won't fill in today's date for you.
- **Its metadata records when it was fetched** as `date_accessed`, since its name says which day it covers.
- **It is not added to `alcove.yaml`.** Alcove discovers partitions from their metadata files, `data/snapshots/gpu/usage/<date>.meta.yaml`, which you commit. A daily job therefore adds one new file and edits nothing that another run might also be editing. A metadata file there that isn't named by a date is not a partition; `alcove audit` warns about it.
- **One `data/.gitignore` pattern covers every partition**, e.g. `snapshots/gpu/usage/????-??-??/` for directories or `snapshots/gpu/usage/????-??-??.parquet` for single files. It matches the data but never the metadata.
- **A new partition copies the descriptive metadata** (`name`, `description`, `source_name`, `source_url`, `access_notes`, `license`, `license_url`) of the dataset's most recent partition.

To revise a day, for example after late-arriving data, snapshot it again with `--force`.

To drop a day, delete its `.meta.yaml` file and its data, and commit. The data was never in git, so other clones still have it after they pull. Their next `alcove run` stops and lists it rather than reading or deleting it; delete it there too. Tables and artifacts built from that day are build products, and `alcove run` deletes those itself (`--dry-run` shows what it would delete).

The same check catches data that's named like a partition but has no metadata for any other reason, such as a snapshot that was interrupted, or data written into the folder before being snapshotted. Stage new data outside `data/snapshots/`, or `alcove run` will stop until you snapshot or delete it. A run filtered to other steps (`alcove run <regex>`) only checks the datasets it touches.

### Find the days you don't have yet

```bash
alcove list gpu/usage/
```

or from Python:

```python
from alcove.core import Alcove
from alcove.types import StepURI

have = set(Alcove().versions(StepURI.parse("snapshot://gpu/usage/*")))
```

### Read every day in one table

```yaml
  table://gpu/usage_all/latest:
  - snapshot://gpu/usage/*
```

In a SQL recipe, `{usage}` becomes a glob that matches each partition's data and never its metadata: `data/snapshots/gpu/usage/????-??-??` for directory partitions, or `data/snapshots/gpu/usage/????-??-??.parquet` for single-file ones. While there is only one partition, it is that partition's own path instead; the same recipe works for both. DuckDB's path functions recover each row's partition date:

```sql
-- directory partitions, each holding a usage.parquet
SELECT parse_filename(parse_dirpath(filename))::DATE AS day, *
FROM read_parquet('{usage}/usage.parquet', filename = true)

-- single-file partitions
SELECT parse_filename(filename, true)::DATE AS day, *
FROM read_parquet('{usage}', filename = true)
```

That date is the partition's name, not necessarily the date of each row. If partitions are trailing windows that overlap, keep the rows' own timestamps and deduplicate across partitions in SQL.

SQL recipes are templated with Python's `str.format`, so prefer these functions to a regex: a quantifier like `\d{4}` would need its braces doubled (`\d{{4}}`). A Python script receives the same glob as an argument and expands it itself.

The table rebuilds whenever a partition is added, revised or dropped. All partitions of a dataset must be the same kind (all directories, or all files with one extension).

### Or build one table per day

```yaml
  table://gpu/clean/*:
  - snapshot://gpu/usage/*
```

This builds `table://gpu/clean/<date>` for each partition, and a new day only builds its own table. `AlcoveDB` unions them as described above. A step like this that reads several partitioned datasets is built for each day that all of them have, so a feed that lags a day holds that day back rather than failing.

### Before the first day arrives

A declared dataset with no partitions yet expands to no steps, as do wildcard steps that read it, so `alcove run` carries on with everything else and `AlcoveDB` leaves out their union views. A concrete step that reads it, like `table://gpu/usage_all/latest` above, can't be planned: `alcove run` stops with "matched zero concrete versions" before building anything. Snapshot the first day before adding such a step.

### Moving an existing dataset over

If you already list a dataset's dated snapshots in `alcove.yaml`, add the `snapshot://<dataset>/*: []` declaration and delete the dated lines; their metadata files are already in place. Versions not named by a plain date, such as `2025-01-01-v2`, are not partitions and must stay listed explicitly. They behave as before, including being revised with `--force`.

If you check out an older commit that lacks some partitions' metadata, `alcove run` stops and lists their data, as for a dropped day. Rather than delete it, check out the newer commit again.
