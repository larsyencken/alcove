# Commands

## Command Reference

Alcove provides the following commands:

| Command | Description |
|---------|-------------|
| `alcove init` | Initialize a new alcove workspace |
| `alcove snapshot <path> <dataset>` | Add a file or directory to your alcove |
| `alcove run` | Build all tables and fetch outdated data |
| `alcove list` | List all datasets in alphabetical order |
| `alcove audit` | Validate the alcove metadata |
| `alcove new-table <path> [deps...]` | Create a new derived table |
| `alcove new-artifact <path> [deps...]` | Create a derived output that is not a table |
| `alcove db [query]` | Open a DuckDB shell or execute a query |
| `alcove export-duckdb <file>` | Export tables to a DuckDB file |

## Creating a new table

To create a new table, use the `new-table` command:

```bash
alcove new-table <table-path> [dep1 [dep2 [...]]]
```

This creates a placeholder executable script that generates an example data file based on the file extension (.parquet or .sql).

### Creating a Parquet table

```bash
alcove new-table path/to/your/table
```

This creates a placeholder Python script that generates an example Parquet file:

```python
#!/usr/bin/env python3
import sys
import polars as pl

data = {
    "a": [1, 1, 3],
    "b": [2, 3, 5],
    "c": [3, 4, 6]
}

df = pl.DataFrame(data)

output_file = sys.argv[-1]
df.write_parquet(output_file)
```

### Creating a SQL table

```bash
alcove new-table path/to/your/table.sql
```

This creates a placeholder SQL script:

```sql
-- SQL script to create a table
CREATE TABLE example_table AS
SELECT
    1 AS a,
    2 AS b,
    3 AS c
```

### Opening in your editor

The command also supports the `--edit` option to open the metadata file in your editor:

```bash
alcove new-table path/to/your/table --edit
```

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

The output lands in `data/artifacts/<artifact-path>/`, with a `.meta.yaml` sidecar recording a checksum of every file produced and of every input. Artifacts rebuild when any dependency or the script changes, can depend on snapshots, tables and other artifacts, and can be depended on by tables in turn. Artifact scripts must be Python; they do not appear in `alcove db`.

## Executing SQL step definitions

If a `.sql` step definition is detected, it will be executed using DuckDB with an in-memory database. The SQL file can use `{variable}` to interpolate template variables. The following template variables are available:

- `{output_file}`: The path to the output file.
- `{dependency}`: The path of each dependency, simplified to a semantic name.

## Building your alcove

Run the `run` command to fetch any data that's out of date and build any derived tables:

```bash
alcove run
```
