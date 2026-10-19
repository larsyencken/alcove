"""`alcove new-table -l <lang>` writes a starter script that runs as written."""

import os
import shutil
import subprocess
from pathlib import Path

import polars as pl
import pytest
from alcove import new_table, plan_and_run, snapshot_to_alcove
from alcove.core import Alcove
from alcove.paths import ARTIFACT_SCRIPT_DIR, TABLE_DIR, TABLE_SCRIPT_DIR
from alcove.tables import _simplify_dependency_names
from alcove.types import StepURI


def has_nu_polars() -> bool:
    if shutil.which("nu") is None:
        return False
    result = subprocess.run(
        ["nu", "-c", "plugin list | where name == polars | length"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "1"


# CI installs nushell and its polars plugin, so these only skip locally
needs_nu_polars = pytest.mark.skipif(
    not has_nu_polars() and os.environ.get("GITHUB_ACTIONS") != "true",
    reason="needs nushell with the polars plugin registered",
)

LANGUAGES = [
    "python",
    "sql",
    pytest.param("nushell", marks=needs_nu_polars),
]

WRITE_ARTIFACT = """#!/usr/bin/env python3
import sys
from pathlib import Path

(Path(sys.argv[-1]) / "index.html").write_text("hello")
"""

EXTENSIONS = {"python": ".py", "sql": ".sql", "nushell": ".nu"}


def add_countries(tmp_path: Path) -> None:
    csv = tmp_path / "countries.csv"
    csv.write_text("dim_country,value\nau,3\nnz,1\n")
    snapshot_to_alcove(csv, "countries/2024-01-01")


@pytest.mark.parametrize("lang", LANGUAGES)
def test_template_without_dependencies(setup_test_environment, lang):
    alcove = Alcove.init()
    new_table(alcove, "example/latest", [], lang)

    script = (TABLE_SCRIPT_DIR / "example/latest").with_suffix(EXTENSIONS[lang])
    assert script.exists()

    plan_and_run(Alcove())
    df = pl.read_parquet(TABLE_DIR / "example/latest.parquet")
    assert df["dim_key"].to_list() == ["a", "b", "c"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_template_copies_first_dependency(setup_test_environment, lang):
    Alcove.init()
    add_countries(setup_test_environment)
    alcove = Alcove()
    new_table(alcove, "copy/latest", ["snapshot://countries/latest"], lang)

    script = (TABLE_SCRIPT_DIR / "copy/latest").with_suffix(EXTENSIONS[lang])
    assert "snapshot://countries/latest" in script.read_text()

    plan_and_run(Alcove())
    df = pl.read_parquet(TABLE_DIR / "copy/latest.parquet")
    assert df["dim_country"].to_list() == ["au", "nz"]


def test_sql_template_lists_template_variables(setup_test_environment):
    Alcove.init()
    add_countries(setup_test_environment)
    alcove = Alcove()
    new_table(alcove, "first/latest", ["snapshot://countries/latest"], "sql")
    new_table(
        alcove,
        "both/latest",
        ["snapshot://countries/latest", "table://first/latest"],
        "sql",
    )

    sql = (TABLE_SCRIPT_DIR / "both/latest.sql").read_text()
    assert "{countries}  snapshot://countries/latest" in sql
    assert "{first}      table://first/latest" in sql
    assert "FROM '{countries}'" in sql

    plan_and_run(Alcove())
    assert pl.read_parquet(TABLE_DIR / "both/latest.parquet").height == 2


@pytest.mark.parametrize("lang", LANGUAGES)
def test_template_for_unreadable_format_writes_example(setup_test_environment, lang):
    Alcove.init()
    notes = setup_test_environment / "notes.txt"
    notes.write_text("hello")
    snapshot_to_alcove(notes, "notes/2024-01-01")
    new_table(Alcove(), "notes/latest", ["snapshot://notes/latest"], lang)

    script = (TABLE_SCRIPT_DIR / "notes/latest").with_suffix(EXTENSIONS[lang])
    assert "doesn't know how to read snapshot://notes/latest" in script.read_text()

    plan_and_run(Alcove())
    df = pl.read_parquet(TABLE_DIR / "notes/latest.parquet")
    assert df["dim_key"].to_list() == ["a", "b", "c"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_template_copies_tsv(setup_test_environment, lang):
    Alcove.init()
    tsv = setup_test_environment / "countries.tsv"
    tsv.write_text("dim_country\tvalue\nau\t3\nnz\t1\n")
    snapshot_to_alcove(tsv, "countries/2024-01-01")
    new_table(Alcove(), "copy/latest", ["snapshot://countries/latest"], lang)

    plan_and_run(Alcove())
    df = pl.read_parquet(TABLE_DIR / "copy/latest.parquet")
    assert df.columns == ["dim_country", "value"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_template_copies_glob_over_versions(setup_test_environment, lang):
    Alcove.init()
    for day, country in [("2024-01-01", "au"), ("2024-01-02", "nz")]:
        csv = setup_test_environment / f"{day}.csv"
        csv.write_text(f"dim_country,value\n{country},1\n")
        snapshot_to_alcove(csv, f"countries/{day}")
    new_table(
        Alcove(),
        "all/latest",
        ["snapshot://countries/2024-01-01", "snapshot://countries/2024-01-02"],
        lang,
    )

    plan_and_run(Alcove())
    df = pl.read_parquet(TABLE_DIR / "all/latest.parquet")
    assert sorted(df["dim_country"].to_list()) == ["au", "nz"]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_template_for_json_glob(setup_test_environment, lang):
    Alcove.init()
    for day, country in [("2024-01-01", "au"), ("2024-01-02", "nz")]:
        f = setup_test_environment / f"{day}.json"
        f.write_text(f'[{{"dim_country": "{country}", "value": 1}}]')
        snapshot_to_alcove(f, f"events/{day}")
    new_table(
        Alcove(),
        "all/latest",
        ["snapshot://events/2024-01-01", "snapshot://events/2024-01-02"],
        lang,
    )

    plan_and_run(Alcove())
    df = pl.read_parquet(TABLE_DIR / "all/latest.parquet")
    if lang == "sql":
        # DuckDB reads a glob of JSON files, so SQL still copies them
        assert sorted(df["dim_country"].to_list()) == ["au", "nz"]
    else:
        assert df["dim_key"].to_list() == ["a", "b", "c"]


def add_report_versions(versions: list[str]) -> None:
    alcove = Alcove()
    for version in versions:
        alcove.new_artifact(f"rep/{version}", [])
    script = ARTIFACT_SCRIPT_DIR / "rep.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(WRITE_ARTIFACT)
    script.chmod(0o755)


@pytest.mark.parametrize(
    "deps",
    [
        ["artifact://rep/latest", "artifact://rep/v1"],
        ["artifact://rep/latest", "artifact://rep/v2"],
        ["artifact://rep/*"],
    ],
)
def test_sql_template_with_artifact_versions_runs(setup_test_environment, deps):
    Alcove.init()
    add_report_versions(["v1", "v2"])
    new_table(Alcove(), "from_rep/latest", deps, "sql")

    sql = (TABLE_SCRIPT_DIR / "from_rep/latest.sql").read_text()
    assert "rep_<version>" in sql
    assert "{rep" not in sql

    plan_and_run(Alcove())
    assert pl.read_parquet(TABLE_DIR / "from_rep/latest.parquet").height == 3


def test_failed_template_unregisters_table(setup_test_environment):
    Alcove.init()
    add_report_versions(["v-1", "v1"])
    with pytest.raises(ValueError, match="distinct"):
        new_table(
            Alcove(), "z/latest", ["artifact://rep/v-1", "artifact://rep/v1"], "sql"
        )

    assert StepURI.parse("table://z/latest") not in Alcove().steps
    new_table(Alcove(), "z/latest", [], "python")


def test_sql_template_names_clashing_dependencies(setup_test_environment):
    Alcove.init()
    add_countries(setup_test_environment)
    alcove = Alcove()
    new_table(alcove, "countries/latest", ["snapshot://countries/latest"], "sql")
    new_table(
        alcove,
        "both/latest",
        ["snapshot://countries/latest", "table://countries/latest"],
        "sql",
    )

    sql = (TABLE_SCRIPT_DIR / "both/latest.sql").read_text()
    assert "{snapshots_countries}  snapshot://countries/latest" in sql
    assert "{tables_countries}     table://countries/latest" in sql

    plan_and_run(Alcove())
    assert pl.read_parquet(TABLE_DIR / "both/latest.parquet").height == 2


def test_sql_template_with_numeric_dataset_name(setup_test_environment):
    Alcove.init()
    csv = setup_test_environment / "who.csv"
    csv.write_text("dim_country,value\nau,3\n")
    snapshot_to_alcove(csv, "who/1990/2024-01-01")
    new_table(Alcove(), "who/latest", ["snapshot://who/1990/latest"], "sql")

    assert "{who_1990}" in (TABLE_SCRIPT_DIR / "who/latest.sql").read_text()
    plan_and_run(Alcove())
    assert pl.read_parquet(TABLE_DIR / "who/latest.parquet").height == 1


def test_failed_registration_leaves_no_script(setup_test_environment):
    alcove = Alcove.init()
    with pytest.raises(ValueError, match="Did you mean snapshot://gdp_v2/latest"):
        new_table(alcove, "gdp/latest", ["snapshot://gdp.v2/latest"], "sql")

    assert not (TABLE_SCRIPT_DIR / "gdp/latest.sql").exists()
    new_table(Alcove(), "gdp/latest", [], "sql")


def test_table_path_with_extension_is_refused(setup_test_environment):
    alcove = Alcove.init()
    with pytest.raises(ValueError, match="Leave the .sql off"):
        new_table(alcove, "population/latest.sql", [], "sql")


def test_simplify_dependency_names():
    names = _simplify_dependency_names(
        [
            Path("data/snapshots/nums/2024-01-01.csv"),
            Path("data/tables/nums/latest.parquet"),
            Path("data/snapshots/bee/latest.csv"),
            Path("data/artifacts/rep/v1"),
            Path("data/artifacts/rep/v2"),
            Path("data/snapshots/who/1990/????-??-??.csv"),
        ]
    )
    assert {name: str(path) for name, path in names.items()} == {
        "snapshots_nums": "data/snapshots/nums/2024-01-01.csv",
        "tables_nums": "data/tables/nums/latest.parquet",
        "bee": "data/snapshots/bee/latest.csv",
        "rep_v1": "data/artifacts/rep/v1",
        "rep_v2": "data/artifacts/rep/v2",
        "who_1990": "data/snapshots/who/1990/????-??-??.csv",
    }


def test_wildcard_table_template_is_shared(setup_test_environment):
    Alcove.init()
    add_countries(setup_test_environment)
    alcove = Alcove()
    new_table(alcove, "daily/*", ["snapshot://countries/*"], "python")

    assert (TABLE_SCRIPT_DIR / "daily.py").exists()

    plan_and_run(Alcove())
    df = pl.read_parquet(TABLE_DIR / "daily/2024-01-01.parquet")
    assert df["dim_country"].to_list() == ["au", "nz"]


def test_template_refuses_existing_script(setup_test_environment):
    alcove = Alcove.init()
    script = TABLE_SCRIPT_DIR / "example/latest.py"
    script.parent.mkdir(parents=True)
    script.write_text("# mine\n")

    with pytest.raises(ValueError, match="already has a script"):
        new_table(alcove, "example/latest", [], "sql")

    assert script.read_text() == "# mine\n"
    assert not (TABLE_SCRIPT_DIR / "example/latest.sql").exists()
    assert StepURI.parse("table://example/latest") not in Alcove().steps


def test_snapshot_rejects_names_tables_cannot_depend_on(setup_test_environment):
    Alcove.init()
    csv = setup_test_environment / "nodes.csv"
    csv.write_text("dim_node,value\nh100,3\n")

    with pytest.raises(ValueError, match="snapshot://secrets/h100_prime_002/"):
        snapshot_to_alcove(csv, "secrets/h100-prime-002/2026-10-15-1900")
    assert StepURI.parse("snapshot://secrets/h100-prime-002/2026-10-15-1900") not in Alcove().steps


def test_datetime_version_snapshot_works_as_dependency(setup_test_environment):
    Alcove.init()
    csv = setup_test_environment / "nodes.csv"
    csv.write_text("dim_node,value\nh100,3\n")
    snapshot_to_alcove(csv, "secrets/h100_prime_002/2026-10-15-1900")

    new_table(
        Alcove(),
        "secrets/combined/2026-10-15-1900",
        ["snapshot://secrets/h100_prime_002/2026-10-15-1900"],
        "sql",
    )
    plan_and_run(Alcove())
    assert (TABLE_DIR / "secrets/combined/2026-10-15-1900.parquet").exists()


def test_new_table_rejects_hyphenated_table_name(setup_test_environment):
    alcove = Alcove.init()
    with pytest.raises(ValueError, match="Did you mean table://gdp_v2/latest"):
        new_table(alcove, "gdp-v2/latest", [], "sql")

    assert not (TABLE_SCRIPT_DIR / "gdp-v2/latest.sql").exists()
