import json
import os

import polars as pl
import pytest

from alcove.core import Alcove
from alcove.db import AlcoveDB, connect
from alcove.utils import save_yaml


@pytest.fixture
def alcove_with_tables(tmp_path):
    """Create a minimal alcove with two parquet tables, no Docker needed."""
    os.chdir(tmp_path)

    # Create alcove.yaml
    save_yaml(
        {
            "version": 1,
            "data_dir": "data",
            "steps": {
                "table://things/latest": [],
                "table://ns/widgets/latest": [],
            },
        },
        tmp_path / "alcove.yaml",
    )

    # Write parquet files
    for rel, data in [
        ("things/latest", {"dim_id": [1, 2, 3], "value": ["a", "b", "c"]}),
        ("ns/widgets/latest", {"dim_id": [10, 20], "name": ["x", "y"]}),
    ]:
        p = tmp_path / "data" / "tables" / (rel + ".parquet")
        p.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(data).write_parquet(p)

    return Alcove(tmp_path / "alcove.yaml")


def test_connect_returns_alcove_db(alcove_with_tables):
    db = connect(alcove=alcove_with_tables)
    assert isinstance(db, AlcoveDB)
    db.close()


def test_repr(alcove_with_tables):
    db = AlcoveDB(alcove=alcove_with_tables, names="full")
    assert repr(db) == "AlcoveDB(2 tables)"
    db.close()


def test_tables_property(alcove_with_tables):
    db = AlcoveDB(alcove=alcove_with_tables)
    assert isinstance(db.tables, list)
    assert len(db.tables) >= 2
    db.close()


def test_sql_full_table(alcove_with_tables):
    db = AlcoveDB(alcove=alcove_with_tables)
    df = db.sql("things_latest")
    assert isinstance(df, pl.DataFrame)
    assert df.shape == (3, 2)
    db.close()


def test_sql_select(alcove_with_tables):
    db = AlcoveDB(alcove=alcove_with_tables)
    df = db.sql("SELECT dim_id FROM things_latest WHERE dim_id > 1")
    assert list(df["dim_id"]) == [2, 3]
    db.close()


def test_query_alias(alcove_with_tables):
    db = AlcoveDB(alcove=alcove_with_tables)
    df1 = db.sql("things_latest")
    df2 = db.query("things_latest")
    assert df1.equals(df2)
    db.close()


def test_conn_escape_hatch(alcove_with_tables):
    import duckdb

    db = AlcoveDB(alcove=alcove_with_tables)
    assert isinstance(db.conn, duckdb.DuckDBPyConnection)
    db.close()


def test_context_manager(alcove_with_tables):
    with AlcoveDB(alcove=alcove_with_tables) as db:
        df = db.sql("things_latest")
        assert df.shape == (3, 2)


def test_names_short(alcove_with_tables):
    db = AlcoveDB(alcove=alcove_with_tables, names="short")
    # Short aliases should exist, full names should not
    tables = db.tables
    assert len(tables) == 2
    db.close()


def test_names_full(alcove_with_tables):
    db = AlcoveDB(alcove=alcove_with_tables, names="full")
    # Only full names, no aliases
    assert "things_latest" in db.tables
    assert "ns_widgets_latest" in db.tables
    assert len(db.tables) == 2
    db.close()


def test_names_both(alcove_with_tables):
    db = AlcoveDB(alcove=alcove_with_tables, names="both")
    # Should have both full names and short aliases
    assert "things_latest" in db.tables
    assert "ns_widgets_latest" in db.tables
    assert len(db.tables) > 2
    db.close()


@pytest.fixture
def alcove_with_wildcard_tables(tmp_path):
    """Alcove with partitioned tables and a wildcard entry."""
    os.chdir(tmp_path)

    save_yaml(
        {
            "version": 1,
            "data_dir": "data",
            "steps": {
                "table://costs/2025-02-17": [],
                "table://costs/2025-02-18": [],
                "table://costs/*": ["snapshot://raw/*"],
            },
        },
        tmp_path / "alcove.yaml",
    )

    # Write partition parquet files
    for version, vals in [
        ("2025-02-17", {"dim_id": [1, 2], "amount": [10, 20]}),
        ("2025-02-18", {"dim_id": [3, 4], "amount": [30, 40]}),
    ]:
        p = tmp_path / "data" / "tables" / "costs" / f"{version}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(vals).write_parquet(p)

    return Alcove(tmp_path / "alcove.yaml")


def test_union_view_queries_all_partitions(alcove_with_wildcard_tables):
    db = AlcoveDB(alcove=alcove_with_wildcard_tables, names="full")
    df = db.sql("costs")
    assert len(df) == 4
    assert set(df["dim_id"].to_list()) == {1, 2, 3, 4}
    db.close()


def test_individual_partition_views(alcove_with_wildcard_tables):
    db = AlcoveDB(alcove=alcove_with_wildcard_tables, names="full")
    df = db.sql("costs_20250217")
    assert len(df) == 2
    assert set(df["dim_id"].to_list()) == {1, 2}
    db.close()


def test_union_view_in_tables_list(alcove_with_wildcard_tables):
    db = AlcoveDB(alcove=alcove_with_wildcard_tables, names="full")
    assert "costs" in db.tables
    assert "costs_20250217" in db.tables
    assert "costs_20250218" in db.tables
    db.close()


def test_sql_statement_without_result(alcove_with_tables):
    with AlcoveDB(alcove=alcove_with_tables) as db:
        df = db.sql("SET timezone = 'UTC'")
        assert isinstance(df, pl.DataFrame)
        assert df.is_empty()


def test_execute_query_json(alcove_with_tables, capsys):
    from alcove import execute_query

    execute_query(alcove_with_tables, "SELECT * FROM things_latest WHERE dim_id < 3")
    assert json.loads(capsys.readouterr().out) == [
        {"dim_id": 1, "value": "a"},
        {"dim_id": 2, "value": "b"},
    ]


def test_execute_query_csv(alcove_with_tables, capsys):
    from alcove import execute_query

    execute_query(alcove_with_tables, "things_latest", csv=True)
    assert capsys.readouterr().out == "dim_id,value\n1,a\n2,b\n3,c\n"


def test_execute_query_csv_nested_columns(alcove_with_tables, capsys):
    from alcove import execute_query

    execute_query(
        alcove_with_tables,
        "SELECT [1, 2] AS l, {'k': 'v'} AS st, NULL::INT[] AS n",
        csv=True,
    )
    assert capsys.readouterr().out == 'l,st,n\n"[1, 2]","{""k"": ""v""}",\n'


def test_execute_query_json_time_columns(alcove_with_tables, capsys):
    from alcove import execute_query

    execute_query(
        alcove_with_tables,
        "SELECT TIME '01:02:03.5' AS t, [TIME '01:00:00'] AS l",
    )
    assert json.loads(capsys.readouterr().out) == [
        {"t": "01:02:03.500", "l": ["01:00:00"]}
    ]
