import subprocess
from pathlib import Path

import polars as pl
import pytest
from alcove import plan_and_run, snapshot_to_alcove, steps
from alcove.artifacts import artifact_path
from alcove.core import Alcove
from alcove.paths import (
    ARTIFACT_SCRIPT_DIR,
    SNAPSHOT_DIR,
    TABLE_DIR,
    TABLE_SCRIPT_DIR,
)
from alcove.snapshots import Snapshot
from alcove.types import StepURI
from alcove.utils import load_yaml, save_yaml
from alcove.wildcards import expand_wildcards

# A union over every directory partition, with each row's partition date
# recovered from its file path.
UNION_SQL = """
SELECT
    parse_filename(parse_dirpath(filename))::DATE AS day,
    namespace,
    gpu_hours
FROM read_parquet('{usage}/usage.parquet', filename = true)
"""


def declare(alcove: Alcove, step: str, deps: list[str] | None = None) -> None:
    "Add a step to alcove.yaml, keeping whatever snapshots have added since."
    alcove.refresh()
    alcove.steps[StepURI.parse(step)] = [StepURI.parse(d) for d in deps or []]
    alcove.save()


def write_sql(path: str, sql: str) -> None:
    script = TABLE_SCRIPT_DIR / path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(sql)


def snapshot_day(day: str, gpu_hours: float = 1.0, force: bool = False) -> Snapshot:
    "Snapshot one day of GPU usage as a directory partition of gpu/usage."
    folder = Path("in") / day
    folder.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"namespace": ["u-a"], "gpu_hours": [gpu_hours]}).write_parquet(
        folder / "usage.parquet"
    )
    return snapshot_to_alcove(folder, f"gpu/usage/{day}", force=force)


def snapshot_file_day(day: str, usd: float = 1.0) -> Snapshot:
    "Snapshot one day of spend as a single-file partition of spend/levels."
    path = Path("in") / f"{day}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"usd": [usd]}).write_parquet(path)
    return snapshot_to_alcove(path, f"spend/levels/{day}")


def dirty_steps(alcove: Alcove) -> set[str]:
    "The steps `alcove run` would execute next."
    alcove.refresh()
    dag = {s: alcove.steps[s] for s in alcove.steps}
    for step, deps in dag.items():
        dag[step] = [
            alcove.get_latest_version(d) if d.version == "latest" else d for d in deps
        ]
    dag, _ = expand_wildcards(dag)
    return {str(s) for s in steps.prune_completed(dag)}


def git_ignored(path: Path) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", str(path)])
    return result.returncode == 0


# ── partitions are discovered, not listed ────────────────────────────


def test_partitions_are_discovered_not_listed(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")

    snapshot_day("2026-10-01")
    snapshot_day("2026-10-02")

    # alcove.yaml still only declares the dataset
    config = load_yaml(Path("alcove.yaml"))
    assert list(config["steps"]) == ["snapshot://gpu/usage/*"]

    # but the catalog knows every partition
    alcove = Alcove()
    assert alcove.versions(StepURI.parse("snapshot://gpu/usage/*")) == [
        "2026-10-01",
        "2026-10-02",
    ]
    assert StepURI.parse("snapshot://gpu/usage/2026-10-02") in alcove.steps

    # and saving the catalog doesn't write them back
    alcove.save()
    assert list(load_yaml(Path("alcove.yaml"))["steps"]) == ["snapshot://gpu/usage/*"]

    # latest resolves among partitions
    latest = alcove.get_latest_version(StepURI.parse("snapshot://gpu/usage/latest"))
    assert latest == StepURI.parse("snapshot://gpu/usage/2026-10-02")


def test_undeclared_datasets_are_still_listed(setup_test_environment):
    alcove = Alcove.init()

    snapshot_day("2026-10-01")

    config = load_yaml(Path("alcove.yaml"))
    assert "snapshot://gpu/usage/2026-10-01" in config["steps"]
    assert "snapshots/gpu/usage/2026-10-01" in Path("data/.gitignore").read_text()
    assert alcove.discovered == set()


def test_partition_must_be_named_by_date(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")

    Path("x.txt").write_text("x")
    for bad in ["latest", "2026-02-30", "2026-10-01-extra"]:
        with pytest.raises(ValueError, match="must be an ISO date"):
            snapshot_to_alcove(Path("x.txt"), f"gpu/usage/{bad}")


def test_stray_metadata_in_partitioned_dataset_raises(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")
    snapshot_day("2026-10-01")

    (SNAPSHOT_DIR / "gpu/usage/notes.meta.yaml").write_text("uri: x\n")

    with pytest.raises(ValueError, match="must be named by an ISO date"):
        Alcove()


def test_new_partition_inherits_dataset_metadata(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")

    first = snapshot_day("2026-10-01")
    first.name = "GPU usage"
    first.source_name = "Mimir"
    first.save()

    second = snapshot_day("2026-10-02")
    assert second.name == "GPU usage"
    assert second.source_name == "Mimir"


# ── one ignore pattern per dataset ───────────────────────────────────


def test_one_gitignore_pattern_per_dataset(setup_test_environment):
    subprocess.run(["git", "init", "-q"], check=True)
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")
    declare(alcove, "snapshot://spend/levels/*")

    for day in ["2026-10-01", "2026-10-02"]:
        snapshot_day(day)
        snapshot_file_day(day)

    lines = Path("data/.gitignore").read_text().splitlines()
    assert lines.count("snapshots/gpu/usage/????-??-??/") == 1
    assert lines.count("snapshots/spend/levels/????-??-??.parquet") == 1
    assert not any("2026-10" in line for line in lines)

    # the data is ignored, but the metadata that indexes it is not
    usage = SNAPSHOT_DIR / "gpu/usage"
    levels = SNAPSHOT_DIR / "spend/levels"
    assert git_ignored(usage / "2026-10-01/usage.parquet")
    assert git_ignored(levels / "2026-10-01.parquet")
    assert not git_ignored(usage / "2026-10-01.meta.yaml")
    assert not git_ignored(levels / "2026-10-01.meta.yaml")


# ── tables over partitions ───────────────────────────────────────────


def test_union_table_over_partitions(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")
    declare(alcove, "table://gpu/usage_all/latest", ["snapshot://gpu/usage/*"])
    write_sql("gpu/usage_all.sql", UNION_SQL)

    snapshot_day("2026-10-01", 1.0)
    snapshot_day("2026-10-02", 2.0)
    plan_and_run(alcove)

    df = pl.read_parquet(TABLE_DIR / "gpu/usage_all/latest.parquet").sort("day")
    assert [str(d) for d in df["day"]] == ["2026-10-01", "2026-10-02"]
    assert df["gpu_hours"].to_list() == [1.0, 2.0]


def test_union_table_rebuilds_when_partition_added(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")
    declare(alcove, "table://gpu/usage_all/latest", ["snapshot://gpu/usage/*"])
    write_sql("gpu/usage_all.sql", UNION_SQL)

    snapshot_day("2026-10-01")
    snapshot_day("2026-10-02")
    plan_and_run(alcove)
    assert dirty_steps(alcove) == set()

    snapshot_day("2026-10-03")
    assert "table://gpu/usage_all/latest" in dirty_steps(alcove)

    plan_and_run(alcove)
    df = pl.read_parquet(TABLE_DIR / "gpu/usage_all/latest.parquet")
    assert df.height == 3


def test_union_table_rebuilds_when_partition_removed(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")
    declare(alcove, "table://gpu/usage_all/latest", ["snapshot://gpu/usage/*"])
    write_sql("gpu/usage_all.sql", UNION_SQL)

    for day in ["2026-10-01", "2026-10-02", "2026-10-03"]:
        snapshot_day(day)
    plan_and_run(alcove)

    # dropping a partition means deleting its metadata and its data
    (SNAPSHOT_DIR / "gpu/usage/2026-10-02.meta.yaml").unlink()
    for f in (SNAPSHOT_DIR / "gpu/usage/2026-10-02").iterdir():
        f.unlink()
    (SNAPSHOT_DIR / "gpu/usage/2026-10-02").rmdir()

    assert "table://gpu/usage_all/latest" in dirty_steps(alcove)
    plan_and_run(alcove)
    df = pl.read_parquet(TABLE_DIR / "gpu/usage_all/latest.parquet")
    assert sorted(str(d) for d in df["day"]) == ["2026-10-01", "2026-10-03"]


def test_union_table_rebuilds_when_partition_revised(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")
    declare(alcove, "table://gpu/usage_all/latest", ["snapshot://gpu/usage/*"])
    write_sql("gpu/usage_all.sql", UNION_SQL)

    snapshot_day("2026-10-01", 1.0)
    snapshot_day("2026-10-02", 2.0)
    plan_and_run(alcove)

    # late-arriving data for an earlier day
    snapshot_day("2026-10-01", 5.0, force=True)
    plan_and_run(alcove)

    df = pl.read_parquet(TABLE_DIR / "gpu/usage_all/latest.parquet")
    assert df["gpu_hours"].sum() == 7.0


def test_union_over_single_file_partitions(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://spend/levels/*")
    declare(alcove, "table://spend/levels_all/latest", ["snapshot://spend/levels/*"])
    write_sql(
        "spend/levels_all.sql",
        "SELECT parse_filename(filename, true)::DATE AS day, usd "
        "FROM read_parquet('{levels}', filename = true)",
    )

    snapshot_file_day("2026-10-01", 1.0)
    snapshot_file_day("2026-10-02", 2.0)
    plan_and_run(alcove)

    df = pl.read_parquet(TABLE_DIR / "spend/levels_all/latest.parquet").sort("day")
    assert [str(d) for d in df["day"]] == ["2026-10-01", "2026-10-02"]
    assert df["usd"].to_list() == [1.0, 2.0]


def test_per_partition_tables_only_build_new_partitions(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")
    declare(alcove, "table://gpu/clean/*", ["snapshot://gpu/usage/*"])
    write_sql("gpu/clean.sql", "SELECT * FROM '{usage}/usage.parquet'")

    snapshot_day("2026-10-01")
    snapshot_day("2026-10-02")
    plan_and_run(alcove)

    # the new partition's data is already local, so only its table is built
    snapshot_day("2026-10-03")
    assert dirty_steps(alcove) == {"table://gpu/clean/2026-10-03"}


def test_empty_partitioned_dataset_does_not_block_other_steps(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")
    declare(alcove, "table://gpu/clean/*", ["snapshot://gpu/usage/*"])
    declare(alcove, "table://other/latest")
    write_sql("other.sql", "SELECT 1 AS x")

    plan_and_run(alcove)

    assert (TABLE_DIR / "other/latest.parquet").exists()


def test_mixed_partition_kinds_raise(setup_test_environment):
    alcove = Alcove.init()
    declare(alcove, "snapshot://gpu/usage/*")
    declare(alcove, "table://gpu/usage_all/latest", ["snapshot://gpu/usage/*"])
    write_sql("gpu/usage_all.sql", UNION_SQL)

    snapshot_day("2026-10-01")
    Path("one.parquet").write_bytes(b"not really parquet")
    snapshot_to_alcove(Path("one.parquet"), "gpu/usage/2026-10-02")

    with pytest.raises(ValueError, match="mix files and directories"):
        plan_and_run(alcove)


# ── `latest` dependencies (#69, #70) ─────────────────────────────────


def test_latest_ignores_sibling_with_shared_prefix(setup_test_environment):
    alcove = Alcove.init()
    Path("a.csv").write_text("src\nmmlu\n")
    Path("b.csv").write_text("src\nmmlu_pro\n")
    snapshot_to_alcove(Path("a.csv"), "t/mmlu/2026-09-29")
    snapshot_to_alcove(Path("b.csv"), "t/mmlu_pro/2026-01-01")
    snapshot_to_alcove(Path("b.csv"), "t/mmlu/extra/2027-01-01")
    alcove.refresh()

    latest = alcove.get_latest_version(StepURI.parse("snapshot://t/mmlu/latest"))
    assert latest == StepURI.parse("snapshot://t/mmlu/2026-09-29")

    with pytest.raises(ValueError, match="no versions of t/nothing"):
        alcove.get_latest_version(StepURI.parse("snapshot://t/nothing/latest"))


def test_table_on_latest_rebuilds_after_new_version(setup_test_environment):
    alcove = Alcove.init()
    Path("a.csv").write_text("n\n1\n")
    snapshot_to_alcove(Path("a.csv"), "t/x/2026-09-01")
    declare(alcove, "table://t/x/latest", ["snapshot://t/x/latest"])
    write_sql("t/x.sql", "SELECT * FROM '{x}'")
    plan_and_run(alcove)

    Path("b.csv").write_text("n\n2\n")
    snapshot_to_alcove(Path("b.csv"), "t/x/2026-09-29")
    plan_and_run(alcove)

    df = pl.read_parquet(TABLE_DIR / "t/x/latest.parquet")
    assert df["n"].to_list() == [2]


COPY_DEP = """#!/usr/bin/env python3
import sys
from pathlib import Path

Path(sys.argv[-1], "out.txt").write_text(Path(sys.argv[1]).read_text())
"""


def test_artifact_on_latest_rebuilds_after_new_version(setup_test_environment):
    alcove = Alcove.init()
    Path("a.txt").write_text("one")
    snapshot_to_alcove(Path("a.txt"), "t/x/2026-09-01")
    declare(alcove, "artifact://t/report/latest", ["snapshot://t/x/latest"])
    script = ARTIFACT_SCRIPT_DIR / "t/report.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(COPY_DEP)
    script.chmod(0o755)
    plan_and_run(alcove)

    Path("b.txt").write_text("two")
    snapshot_to_alcove(Path("b.txt"), "t/x/2026-09-29")
    plan_and_run(alcove)

    out = artifact_path(StepURI.parse("artifact://t/report/latest")) / "out.txt"
    assert out.read_text() == "two"


def test_steps_built_by_older_alcove_rebuild_once(setup_test_environment):
    "A recorded manifest missing a dependency's metadata counts as stale."
    alcove = Alcove.init()
    Path("a.csv").write_text("n\n1\n")
    snapshot_to_alcove(Path("a.csv"), "t/x/2026-09-01")
    declare(alcove, "table://t/x/latest", ["snapshot://t/x/latest"])
    write_sql("t/x.sql", "SELECT * FROM '{x}'")
    plan_and_run(alcove)

    meta_path = TABLE_DIR / "t/x/latest.meta.yaml"
    meta = load_yaml(meta_path)
    meta["input_manifest"] = {
        k: v for k, v in meta["input_manifest"].items() if not k.startswith("data/")
    }
    save_yaml(meta, meta_path)

    assert "table://t/x/latest" in dirty_steps(alcove)
