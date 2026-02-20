# Python API

Alcove provides a programmatic Python API through `AlcoveDB`, which lets you query your tables as Polars DataFrames using DuckDB.

## Connecting

Use `alcove.connect()` from within your alcove directory (where `alcove.yaml` lives):

```python
import alcove

db = alcove.connect()
print(db.tables)  # list all available tables
```

## Querying tables

### By table name

Pass a table name to `sql()` to get all rows:

```python
df = db.sql("countries")
```

### With SQL

Pass a full SQL query:

```python
df = db.sql("SELECT name, population FROM countries WHERE population > 1000000")
```

### query() alias

`query()` is an alias for `sql()`:

```python
df = db.query("SELECT * FROM countries")
```

## Table naming

By default (`names="both"`), tables are registered under both their full path name and a short alias. You can control this with the `names` parameter:

```python
# Both full and short names (default)
db = alcove.connect(names="both")

# Only full path names (e.g. "derived_population_latest")
db = alcove.connect(names="full")

# Only short aliases (e.g. "population")
db = alcove.connect(names="short")
```

## Context manager

`AlcoveDB` supports use as a context manager:

```python
import alcove

with alcove.connect() as db:
    df = db.sql("SELECT * FROM countries")
    print(df)
# connection is closed automatically
```

## Direct DuckDB access

For advanced use, access the underlying DuckDB connection:

```python
db = alcove.connect()
result = db.conn.execute("SHOW TABLES").fetchall()
```

## API Reference

### `alcove.connect(alcove=None, names="both")`

Open a queryable connection to all Alcove tables.

- **alcove** — Optional `Alcove` instance. If `None`, creates one from the current directory.
- **names** — Table naming strategy: `"short"`, `"full"`, or `"both"` (default).
- **Returns** — `AlcoveDB` instance.

### `AlcoveDB.sql(query)` / `AlcoveDB.query(query)`

Execute a SQL query and return a Polars DataFrame. If `query` contains no spaces, it is treated as a table name and expanded to `SELECT * FROM "query"`.

### `AlcoveDB.tables`

List of all registered table names.

### `AlcoveDB.conn`

The underlying `duckdb.DuckDBPyConnection`.

### `AlcoveDB.close()`

Close the database connection.
