import pytest

from alcove.types import StepURI
from alcove.wildcards import expand_wildcards


# ── StepURI wildcard properties ──────────────────────────────────────


def test_is_wildcard():
    assert StepURI("table", "foo/*").is_wildcard
    assert not StepURI("table", "foo/2025-02-17").is_wildcard


def test_base_path():
    assert StepURI("table", "foo/*").base_path == "foo"
    assert StepURI("table", "ns/foo/*").base_path == "ns/foo"
    assert StepURI("table", "foo/2025-02-17").base_path == "foo"


def test_version():
    assert StepURI("table", "foo/*").version == "*"
    assert StepURI("table", "foo/2025-02-17").version == "2025-02-17"


def test_with_version():
    uri = StepURI("table", "foo/*")
    concrete = uri.with_version("2025-02-17")
    assert concrete == StepURI("table", "foo/2025-02-17")
    assert concrete.scheme == "table"


# ── expand_wildcards ─────────────────────────────────────────────────


def test_passthrough_no_wildcards():
    """DAG without wildcards is returned unchanged."""
    dag = {
        StepURI("snapshot", "a/v1"): [],
        StepURI("table", "b/v1"): [StepURI("snapshot", "a/v1")],
    }
    expanded, groups = expand_wildcards(dag)
    assert expanded == dag
    assert groups == {}


def test_simple_expansion():
    """table://foo/* with snapshot://bar/* expands to per-version steps."""
    dag = {
        StepURI("snapshot", "bar/2025-02-17"): [],
        StepURI("snapshot", "bar/2025-02-18"): [],
        StepURI("table", "foo/*"): [StepURI("snapshot", "bar/*")],
    }
    expanded, groups = expand_wildcards(dag)

    # Wildcard key removed
    assert StepURI("table", "foo/*") not in expanded

    # Concrete keys created
    assert StepURI("table", "foo/2025-02-17") in expanded
    assert StepURI("table", "foo/2025-02-18") in expanded

    # Dependencies are concrete
    assert expanded[StepURI("table", "foo/2025-02-17")] == [
        StepURI("snapshot", "bar/2025-02-17")
    ]
    assert expanded[StepURI("table", "foo/2025-02-18")] == [
        StepURI("snapshot", "bar/2025-02-18")
    ]

    # Groups recorded
    assert "table://foo" in groups
    assert sorted(groups["table://foo"]) == ["2025-02-17", "2025-02-18"]


def test_chained_wildcards():
    """a/* → b/* → c/* all expand correctly."""
    dag = {
        StepURI("snapshot", "c/v1"): [],
        StepURI("snapshot", "c/v2"): [],
        StepURI("table", "b/*"): [StepURI("snapshot", "c/*")],
        StepURI("table", "a/*"): [StepURI("table", "b/*")],
    }
    expanded, groups = expand_wildcards(dag)

    # All wildcards removed
    for step in expanded:
        assert not step.is_wildcard

    # Check chain
    assert expanded[StepURI("table", "a/v1")] == [StepURI("table", "b/v1")]
    assert expanded[StepURI("table", "a/v2")] == [StepURI("table", "b/v2")]
    assert expanded[StepURI("table", "b/v1")] == [StepURI("snapshot", "c/v1")]
    assert expanded[StepURI("table", "b/v2")] == [StepURI("snapshot", "c/v2")]


def test_concrete_to_wildcard():
    """A concrete step depending on foo/* gets all partitions as deps."""
    dag = {
        StepURI("snapshot", "bar/v1"): [],
        StepURI("snapshot", "bar/v2"): [],
        StepURI("table", "foo/*"): [StepURI("snapshot", "bar/*")],
        StepURI("table", "summary/2025-02"): [StepURI("table", "foo/*")],
    }
    expanded, groups = expand_wildcards(dag)

    summary_deps = expanded[StepURI("table", "summary/2025-02")]
    assert StepURI("table", "foo/v1") in summary_deps
    assert StepURI("table", "foo/v2") in summary_deps


def test_mixed_deps():
    """Wildcard + concrete deps on the same step."""
    dag = {
        StepURI("snapshot", "bar/v1"): [],
        StepURI("snapshot", "bar/v2"): [],
        StepURI("table", "foo/*"): [StepURI("snapshot", "bar/*")],
        StepURI("table", "report/latest"): [
            StepURI("table", "foo/*"),
            StepURI("snapshot", "bar/v1"),
        ],
    }
    expanded, groups = expand_wildcards(dag)

    report_deps = expanded[StepURI("table", "report/latest")]
    # Should have foo/v1, foo/v2, and bar/v1
    assert StepURI("table", "foo/v1") in report_deps
    assert StepURI("table", "foo/v2") in report_deps
    assert StepURI("snapshot", "bar/v1") in report_deps


def test_wildcard_zero_versions_raises():
    """Wildcard with no matching versions raises an error."""
    dag = {
        StepURI("table", "foo/*"): [StepURI("snapshot", "bar/*")],
    }
    with pytest.raises(ValueError, match="zero concrete versions"):
        expand_wildcards(dag)


def test_wildcard_versions_from_deps():
    """Versions discovered from deps of other steps, not just step keys."""
    dag = {
        StepURI("snapshot", "data/v1"): [],
        StepURI("snapshot", "data/v2"): [],
        StepURI("table", "clean/*"): [StepURI("snapshot", "data/*")],
    }
    expanded, groups = expand_wildcards(dag)

    assert StepURI("table", "clean/v1") in expanded
    assert StepURI("table", "clean/v2") in expanded
    assert sorted(groups["table://clean"]) == ["v1", "v2"]
