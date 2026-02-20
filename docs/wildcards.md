# Wildcards

Alcove supports wildcard `*` steps for date-partitioned tables, allowing you to write a single script that gets reused across many versions of a dataset.

## How it works

A wildcard step URI like `table://foo/*` tells Alcove to expand the step into one concrete step per discovered version. Versions are discovered by scanning the DAG for concrete steps that share the same base path.

For example, if you have snapshots `snapshot://raw/2025-01`, `snapshot://raw/2025-02`, and `snapshot://raw/2025-03`, a wildcard table `table://clean/*` that depends on `snapshot://raw/*` will automatically expand into three concrete steps: `table://clean/2025-01`, `table://clean/2025-02`, and `table://clean/2025-03`.

## Configuration

Define wildcard steps in your `alcove.yaml` using `*` as the version:

```yaml
steps:
  # Concrete snapshots with specific versions
  - uri: snapshot://raw/2025-01
  - uri: snapshot://raw/2025-02
  - uri: snapshot://raw/2025-03

  # Wildcard table: one script runs per version
  - uri: table://clean/*
    deps:
      - snapshot://raw/*

  # Another wildcard table that chains off the first
  - uri: table://summary/*
    deps:
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
