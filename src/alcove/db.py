import re
from collections import defaultdict
from pathlib import Path
from typing import Literal

import duckdb
import polars as pl

from alcove.core import Alcove


class AlcoveDB:
    """Programmatic interface to query Alcove tables as Polars DataFrames."""

    def __init__(
        self,
        alcove: Alcove | None = None,
        names: Literal["short", "full", "both"] = "both",
    ):
        if alcove is None:
            alcove = Alcove()

        tables = _get_tables(alcove)

        self._conn = duckdb.connect(":memory:")
        for path in tables:
            table_name = _path_to_snake(path)
            table_path = (Path("data/tables") / path).with_suffix(".parquet")
            self._conn.execute(
                f"CREATE VIEW \"{table_name}\" AS SELECT * FROM read_parquet('{table_path}')"
            )

        if names == "both":
            for alias, table_name in _table_aliases(tables):
                self._conn.execute(
                    f'CREATE VIEW "{alias}" AS SELECT * FROM "{table_name}"'
                )
        elif names == "short":
            for alias, table_name in _table_aliases(tables):
                self._conn.execute(f'ALTER VIEW "{table_name}" RENAME TO "{alias}"')

        self._tables = sorted(
            row[0] for row in self._conn.execute("SHOW TABLES").fetchall()
        )

    @property
    def tables(self) -> list[str]:
        return self._tables

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        return self._conn

    def sql(self, query: str) -> pl.DataFrame:
        if query.count(" ") == 0:
            query = f'SELECT * FROM "{query}"'

        return self._conn.execute(query).pl()

    def query(self, query: str) -> pl.DataFrame:
        return self.sql(query)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AlcoveDB":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"AlcoveDB({len(self._tables)} tables)"


def connect(
    alcove: Alcove | None = None,
    names: Literal["short", "full", "both"] = "both",
) -> AlcoveDB:
    """Open a queryable connection to all Alcove tables."""
    return AlcoveDB(alcove=alcove, names=names)


def _path_to_snake(path: str) -> str:
    return path.replace("/", "_").replace("-", "").rsplit(".", 1)[0]


def _get_tables(alcove: Alcove) -> list[str]:
    tables = []
    for step in alcove.steps:
        if step.scheme == "table":
            tables.append(step.path)

    return tables


def _table_aliases(tables: list[str]) -> list[tuple[str, str]]:
    potential_aliases: dict[str, set[str]] = defaultdict(set)
    for path in tables:
        parts = path.split("/")

        for i in range(len(parts) - 1):
            no_version = "/".join(parts[i:-1])
            if no_version:
                potential_aliases[no_version].add(path)

            with_version = "/".join(parts[i:])
            if with_version != path:
                potential_aliases[with_version].add(path)

    best_alias: dict[str, str] = {}
    for alias, paths in potential_aliases.items():
        if len(paths) == 1:
            (path,) = paths
            table_alias = _path_to_snake(alias)
            table_name = _path_to_snake(path)

            best_alias[table_name] = _better_alias(
                table_alias, best_alias.get(table_name)
            )

    return [(table_alias, table_name) for table_name, table_alias in best_alias.items()]


def _better_alias(a: str, b: str | None) -> str:
    if not b:
        return a

    return min([(_has_version(a), len(a), a), (_has_version(b), len(b), b)])[-1]


def _has_version(name: str) -> bool:
    return bool(re.match(r".*_((d{4}-\d{2}-\d{2})|latest)$", name))
