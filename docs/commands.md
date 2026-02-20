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

## Executing SQL step definitions

If a `.sql` step definition is detected, it will be executed using DuckDB with an in-memory database. The SQL file can use `{variable}` to interpolate template variables. The following template variables are available:

- `{output_file}`: The path to the output file.
- `{dependency}`: The path of each dependency, simplified to a semantic name.

## Building your alcove

Run the `run` command to fetch any data that's out of date and build any derived tables:

```bash
alcove run
```
