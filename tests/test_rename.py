"""`alcove rename` renames every version of a dataset, and what refers to it."""

import subprocess
from pathlib import Path

import polars as pl
import pytest
import yaml
from alcove import (
    Alcove,
    audit_alcove,
    init_alcove,
    new_table,
    plan_and_run,
    snapshot_to_alcove,
)
from alcove.rename import rename_dataset, rename_template_variables
from alcove.storage import StorageConfig
from alcove.utils import load_yaml


def steps() -> dict:
    return yaml.safe_load(Path("alcove.yaml").read_text())["steps"]


def write_sql(path: str, sql: str) -> Path:
    script = Path("src/steps/tables") / path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(sql)
    return script


def add_countries(tmp_path: Path, name: str = "countries/2024-01-01") -> None:
    csv = tmp_path / "countries.csv"
    csv.write_text("dim_country,value\nau,3\nnz,1\n")
    snapshot_to_alcove(csv, name)


@pytest.fixture
def project(offline, tmp_path):
    """countries (snapshot) -> geo/countries (table) -> summary (table),
    which also reads the snapshot."""
    init_alcove(StorageConfig("local"))
    add_countries(tmp_path)
    alcove = Alcove()
    alcove.new_table("geo/countries/latest", ["snapshot://countries/latest"])
    write_sql("geo/countries/latest.sql", "SELECT * FROM '{countries}'\n")
    # both dependencies are called countries, so their variables are told apart
    alcove.new_table(
        "summary/latest",
        ["table://geo/countries/latest", "snapshot://countries/latest"],
    )
    write_sql("summary/latest.sql", SUMMARY_SQL)
    plan_and_run(Alcove())
    return offline


SUMMARY_SQL = """-- reads geo/countries
SELECT t.dim_country, t.value * 2 AS doubled
FROM '{geo_countries}' AS t JOIN '{snapshots_countries}' AS s USING (dim_country)
"""


def test_rename_table(project, capsys):
    rename_dataset(Alcove(), "table://geo/countries", "table://geo/nations")

    assert "table://geo/nations/latest" in steps()
    assert "table://geo/countries/latest" not in steps()
    assert steps()["table://summary/latest"][0] == "table://geo/nations/latest"

    # the script moved, and the step reading it uses its new template variable
    assert not Path("src/steps/tables/geo/countries").exists()
    assert Path("src/steps/tables/geo/nations/latest.sql").exists()
    # the snapshot's variable no longer needs telling apart from the table's
    summary = Path("src/steps/tables/summary/latest.sql").read_text()
    assert summary == SUMMARY_SQL.replace("{geo_countries}", "{nations}").replace(
        "{snapshots_countries}", "{countries}"
    )

    # the built table moved, and is rebuilt along with what reads it
    assert Path("data/tables/geo/nations/latest.parquet").exists()
    assert not Path("data/tables/geo/countries").exists()
    capsys.readouterr()
    plan_and_run(Alcove())
    out = capsys.readouterr().out
    assert "table://geo/nations/latest" in out
    assert "table://summary/latest" in out
    df = pl.read_parquet("data/tables/summary/latest.parquet")
    assert sorted(df["doubled"].to_list()) == [2, 6]


def test_rename_reports_lines_it_left(project, capsys):
    "A comment naming the dataset is left alone, and reported."
    rename_dataset(Alcove(), "table://geo/countries", "table://geo/nations")
    out = capsys.readouterr().out
    assert "src/steps/tables/summary/latest.sql:1: -- reads geo/countries" in out


def test_rename_snapshot(project, capsys):
    rename_dataset(Alcove(), "snapshot://countries", "snapshot://places/countries")

    assert "snapshot://places/countries/2024-01-01" in steps()
    assert steps()["table://geo/countries/latest"] == [
        "snapshot://places/countries/latest"
    ]

    metadata = Path("data/snapshots/places/countries/2024-01-01.meta.yaml")
    assert load_yaml(metadata)["uri"] == "snapshot://places/countries/2024-01-01"
    assert Path("data/snapshots/places/countries/2024-01-01.csv").exists()
    assert not Path("data/snapshots/countries").exists()

    ignored = Path("data/.gitignore").read_text().split()
    assert "snapshots/places/countries/2024-01-01.csv" in ignored
    assert "snapshots/countries/2024-01-01.csv" not in ignored

    # its variable is named after its new path, in both steps reading it
    table_sql = Path("src/steps/tables/geo/countries/latest.sql").read_text()
    assert table_sql == "SELECT * FROM '{countries}'\n"
    summary = Path("src/steps/tables/summary/latest.sql").read_text()
    assert summary == SUMMARY_SQL.replace("{snapshots_countries}", "{places_countries}")

    capsys.readouterr()
    audit_alcove(Alcove())
    plan_and_run(Alcove())
    assert "table://geo/countries/latest" in capsys.readouterr().out


def test_rename_snapshot_refetches(project):
    "Snapshot data is content-addressed, so the renamed snapshot fetches as before."
    rename_dataset(Alcove(), "snapshot://countries", "snapshot://nations")
    Path("data/snapshots/nations/2024-01-01.csv").unlink()
    plan_and_run(Alcove())
    assert (
        Path("data/snapshots/nations/2024-01-01.csv")
        .read_text()
        .startswith("dim_country")
    )


def test_rename_partitioned_snapshot(offline, tmp_path):
    init_alcove(StorageConfig("local"))
    alcove = Alcove()
    alcove.steps[alcove_uri("snapshot://usage/*")] = []
    alcove.save()
    for day in ["2024-01-01", "2024-01-02"]:
        add_countries(tmp_path, f"usage/{day}")
    new_table(Alcove(), "daily/*", ["snapshot://usage/*"], "sql")
    plan_and_run(Alcove())

    rename_dataset(Alcove(), "snapshot://usage", "snapshot://gpu_usage")

    assert steps() == {
        "snapshot://gpu_usage/*": [],
        "table://daily/*": ["snapshot://gpu_usage/*"],
    }
    assert sorted(Alcove().versions(alcove_uri("snapshot://gpu_usage/*"))) == [
        "2024-01-01",
        "2024-01-02",
    ]
    ignored = Path("data/.gitignore").read_text().split()
    assert "snapshots/gpu_usage/????-??-??.csv" in ignored
    assert "snapshots/usage/????-??-??.csv" not in ignored

    sql = Path("src/steps/tables/daily.sql").read_text()
    assert "{gpu_usage}" in sql and "{usage}" not in sql
    plan_and_run(Alcove())
    assert Path("data/tables/daily/2024-01-02.parquet").exists()


def test_rename_wildcard_table_moves_every_built_partition(offline, tmp_path):
    init_alcove(StorageConfig("local"))
    alcove = Alcove()
    alcove.steps[alcove_uri("snapshot://usage/*")] = []
    alcove.save()
    for day in ["2024-01-01", "2024-01-02"]:
        add_countries(tmp_path, f"usage/{day}")
    new_table(Alcove(), "daily/*", ["snapshot://usage/*"], "sql")
    plan_and_run(Alcove())

    rename_dataset(Alcove(), "table://daily", "table://usage_days")

    assert Path("src/steps/tables/usage_days.sql").exists()
    for day in ["2024-01-01", "2024-01-02"]:
        assert Path(f"data/tables/usage_days/{day}.parquet").exists()
        assert Path(f"data/tables/usage_days/{day}.meta.yaml").exists()
    assert not Path("data/tables/daily").exists()


def test_rename_updates_inherit_config(project):
    config = Path("src/steps/tables/summary/latest.meta.yaml")
    config.write_text("inherit:\n  table://geo/countries/latest:\n    fields: [name]\n")
    rename_dataset(Alcove(), "table://geo/countries", "table://geo/nations")
    assert "table://geo/nations/latest:" in config.read_text()
    plan_and_run(Alcove())


def test_dry_run_changes_nothing(project, capsys):
    before = {p: p.read_bytes() for p in Path(".").rglob("*") if p.is_file()}
    rename_dataset(Alcove(), "table://geo/countries", "table://geo/nations", True)
    after = {p: p.read_bytes() for p in Path(".").rglob("*") if p.is_file()}
    assert before == after
    # rich wraps long lines
    out = " ".join(capsys.readouterr().out.split())
    assert "geo/nations/latest.sql" in out
    assert "({geo_countries} -> {nations}, {snapshots_countries} -> {countries})" in out


@pytest.mark.parametrize(
    "old, new, error",
    [
        ("table://geo/countries/latest", "table://geo/x", "is a version"),
        ("table://nope", "table://x", "No dataset"),
        ("table://geo/countries", "table://summary", "already exists"),
        ("table://geo/countries", "snapshot://x", "same scheme"),
        ("geo/countries", "table://x", "with its scheme"),
        ("table://geo/countries", "table://geo/bad-name", "invalid step name"),
    ],
)
def test_rename_refuses(project, old, new, error):
    before = Path("alcove.yaml").read_text()
    with pytest.raises(ValueError, match=error):
        rename_dataset(Alcove(), old, new)
    assert Path("alcove.yaml").read_text() == before


def test_rename_template_variables():
    sql = "SELECT '{{literal}}', * FROM '{a}' JOIN '{b}' USING (x) -- {a}\n"
    assert rename_template_variables(sql, {"a": "c"}) == (
        "SELECT '{{literal}}', * FROM '{c}' JOIN '{b}' USING (x) -- {c}\n"
    )


def alcove_uri(uri: str):
    from alcove.types import StepURI

    return StepURI.parse(uri)


def test_shared_script_left_alone_if_another_step_needs_its_variables(
    offline, tmp_path, capsys
):
    "Only report/a reads geo/countries, but report.sql builds report/b too."
    init_alcove(StorageConfig("local"))
    add_countries(tmp_path)
    alcove = Alcove()
    for name in ["geo/countries", "other/countries"]:
        alcove.new_table(f"{name}/latest", ["snapshot://countries/latest"])
        write_sql(f"{name}/latest.sql", "SELECT * FROM '{countries}'\n")
    alcove.new_table("report/a", ["table://geo/countries/latest"])
    alcove.new_table("report/b", ["table://other/countries/latest"])
    write_sql("report.sql", "SELECT * FROM '{countries}'\n")

    rename_dataset(Alcove(), "table://geo/countries", "table://geo/nations")

    assert Path("src/steps/tables/report.sql").read_text() == (
        "SELECT * FROM '{countries}'\n"
    )
    assert "report.sql is shared by steps" in capsys.readouterr().out


def test_rename_refuses_to_move_into_its_own_version(offline, tmp_path):
    init_alcove(StorageConfig("local"))
    folder = tmp_path / "d1"
    folder.mkdir()
    (folder / "a.txt").write_text("a")
    snapshot_to_alcove(folder, "dirs/latest")
    before = sorted(Path(".").rglob("*"))

    with pytest.raises(ValueError, match="already exists|into itself"):
        rename_dataset(Alcove(), "snapshot://dirs", "snapshot://dirs/latest")
    assert sorted(Path(".").rglob("*")) == before


def test_rename_refuses_a_stray_script_at_the_new_name(project):
    write_sql("geo/nations/latest.sql", "SELECT 1 AS dim_stale\n")
    with pytest.raises(ValueError, match="geo/nations/latest.sql already exists"):
        rename_dataset(Alcove(), "table://geo/countries", "table://geo/nations")
    assert Path("src/steps/tables/geo/countries/latest.sql").exists()


def test_rename_refuses_undeclared_snapshot_metadata_at_the_new_name(project, tmp_path):
    stray = Path("data/snapshots/places/2024-01-05.meta.yaml")
    stray.parent.mkdir(parents=True)
    stray.write_text("uri: snapshot://places/2024-01-05\n")
    with pytest.raises(ValueError, match="already exists"):
        rename_dataset(Alcove(), "snapshot://countries", "snapshot://places")


def test_rename_moves_helpers_in_the_script_folder(project):
    helper = Path("src/steps/tables/geo/countries/common.py")
    helper.write_text("X = 1\n")
    rename_dataset(Alcove(), "table://geo/countries", "table://geo/nations")
    assert Path("src/steps/tables/geo/nations/common.py").read_text() == "X = 1\n"
    assert not helper.parent.exists()


def test_rename_leaves_helpers_shared_with_nested_datasets(project, capsys):
    alcove = Alcove()
    alcove.new_table("geo/countries/big/latest", ["table://geo/countries/latest"])
    write_sql("geo/countries/big/latest.sql", "SELECT * FROM '{countries}'\n")
    helper = Path("src/steps/tables/geo/countries/common.py")
    helper.write_text("X = 1\n")

    rename_dataset(Alcove(), "table://geo/countries", "table://geo/nations")

    assert helper.exists()
    assert Path("src/steps/tables/geo/countries/big/latest.sql").exists()
    assert Path("src/steps/tables/geo/nations/latest.sql").exists()
    out = capsys.readouterr().out
    assert "common.py was left where it is" in out
    # the nested dataset isn't reported as a leftover
    assert "geo/countries/big" not in out.split("still mention")[-1]


def test_rename_rewrites_uris_in_step_scripts(project):
    script = Path("src/steps/tables/summary/latest.sql")
    script.write_text("-- reads table://geo/countries/latest\n" + SUMMARY_SQL)
    rename_dataset(Alcove(), "table://geo/countries", "table://geo/nations")
    assert script.read_text().startswith("-- reads table://geo/nations/latest\n")


def test_leftovers_ignore_other_schemes_and_the_new_name(project, capsys):
    "Renaming the snapshot doesn't report the table of the same name."
    Path("notes.md").write_text(
        "see data/tables/countries and table://countries/latest\n"
        "see data/snapshots/countries/2024-01-01.csv\n"
    )
    rename_dataset(Alcove(), "snapshot://countries", "snapshot://places/countries")
    leftovers = capsys.readouterr().out.split("still mention")[-1]
    assert "notes.md:2:" in leftovers
    assert "notes.md:1:" not in leftovers
    assert "places/countries" not in leftovers.replace("notes.md", "")


@pytest.mark.parametrize("in_git", [True, False])
def test_dry_run_reports_the_same_leftovers(project, capsys, in_git):
    "Files are found with git ls-files, or by walking the project without git."
    if in_git:
        subprocess.run(["git", "init", "-q"], check=True)
    Path("notes café.md").write_text("geo/countries\n")

    def leftovers(dry_run: bool) -> str:
        rename_dataset(
            Alcove(), "table://geo/countries", "table://geo/nations", dry_run
        )
        return capsys.readouterr().out.split("still mention")[-1]

    dry = leftovers(True)
    assert "notes café.md:1:" in dry
    assert dry == leftovers(False)


def test_leftovers_found_when_the_new_name_is_inside_the_old(project, capsys):
    Path("analysis.py").write_text('read("data/tables/geo/countries/latest.parquet")\n')
    rename_dataset(Alcove(), "table://geo/countries", "table://geo/count")
    assert "analysis.py:1:" in capsys.readouterr().out.split("still mention")[-1]


def test_rename_out_of_a_folder_with_a_shared_script(offline, tmp_path):
    "table://foo/x/* is built by foo/x.sql, which moves to foo.sql."
    init_alcove(StorageConfig("local"))
    alcove = Alcove()
    alcove.steps[alcove_uri("snapshot://usage/*")] = []
    alcove.save()
    add_countries(tmp_path, "usage/2024-01-01")
    new_table(Alcove(), "foo/x/*", ["snapshot://usage/*"], "sql")

    rename_dataset(Alcove(), "table://foo/x", "table://foo")

    assert Path("src/steps/tables/foo.sql").exists()
    plan_and_run(Alcove())
    assert Path("data/tables/foo/2024-01-01.parquet").exists()


def test_rename_keeps_relative_symlinks_pointing_out_of_a_step_folder(project):
    shared = Path("src/steps/tables/shared/note.txt")
    shared.parent.mkdir(parents=True)
    shared.write_text("hello")
    link = Path("src/steps/tables/geo/countries/latest.txt")
    link.symlink_to("../../shared/note.txt")

    rename_dataset(Alcove(), "table://geo/countries", "table://a/b/nations")

    moved = Path("src/steps/tables/a/b/nations/latest.txt")
    assert moved.is_symlink() and moved.read_text() == "hello"
