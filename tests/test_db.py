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
