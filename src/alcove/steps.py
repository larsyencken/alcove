import re
from typing import List

import graphlib

from alcove import artifacts, snapshots, tables
from alcove.types import Dag, StepURI


def prune_with_regex(dag: Dag, regex: str, descendents: bool = True) -> Dag:
    "Reduce to regex."
    step_to_upstream = dag
    step_to_downstream = {}
    for step, deps in dag.items():
        for dep in deps:
            step_to_downstream.setdefault(dep, []).append(step)

    queue = []
    for step in step_to_upstream:
        if re.search(regex, str(step)):
            queue.append(step)

    include = set()
    while queue:
        step = queue.pop()
        if step in include:
            continue

        include.add(step)

        queue.extend(step_to_upstream.get(step, []))
        if descendents:
            queue.extend(step_to_downstream.get(step, []))

    sub_dag = {step: dag[step] for step in include}
    assert len(sub_dag) == len(include)
    return sub_dag


def prune_completed(dag: Dag) -> Dag:
    "Remove steps that do not need executing."
    is_dirty = {}

    # walk the graph in topological order
    for step in graphlib.TopologicalSorter(dag).static_order():
        # a step needs re-running if any of its deps are dirty
        deps = dag[step]
        is_dirty[step] = any(is_dirty[dep] for dep in deps) or not is_completed(
            step, deps
        )

    include = {step for step, dirty in is_dirty.items() if dirty}
    sub_dag = {step: dag[step] for step in include}
    return sub_dag


def is_completed(step: StepURI, deps: list[StepURI]) -> bool:
    if step.scheme == "snapshot":
        return snapshots.is_completed(step)

    elif step.scheme == "table":
        return tables.is_completed(step, deps)

    elif step.scheme == "artifact":
        return artifacts.is_completed(step, deps)

    raise ValueError(f"Unknown scheme {step.scheme}")


def execute_dag(
    dag: Dag, dry_run: bool = False, jobs: int = snapshots.DEFAULT_FETCH_JOBS
) -> None:
    "Execute the DAG, downloading up to `jobs` snapshot files at once."
    to_execute = in_topological_order(dag)
    print(f"Executing {len(to_execute)} steps")
    if dry_run:
        for step in to_execute:
            print(step)
        return

    # snapshots depend on nothing, so every one can be fetched up front, many
    # files at a time, instead of one file at a time in step order
    to_fetch = [step for step in to_execute if step.scheme == "snapshot"]
    for step in to_fetch:
        print(step)
    if to_fetch:
        snapshots.fetch_snapshots(
            [snapshots.Snapshot.load(step.path) for step in to_fetch], jobs
        )

    for step in to_execute:
        if step.scheme != "snapshot":
            print(step)
            execute_step(step, dag[step])


def execute_step(step: StepURI, dependencies: List[StepURI]) -> None:
    "Execute a single step."
    if step.scheme == "snapshot":
        return snapshots.Snapshot.load(step.path).fetch()

    elif step.scheme == "table":
        return tables.build_table(step, dependencies)

    elif step.scheme == "artifact":
        return artifacts.build_artifact(step, dependencies)

    else:
        raise ValueError(f"Unknown scheme {step.scheme}")


def in_topological_order(dag: Dag) -> List[StepURI]:
    # we need to retain dependencies, but not consider them as steps
    # included for execution
    return [
        step for step in graphlib.TopologicalSorter(dag).static_order() if step in dag
    ]


def in_dataset_order(dag: Dag) -> List[StepURI]:
    """Steps with each dataset after the datasets it depends on, and every
    version of a dataset together, e.g. for `alcove list`.

    A dataset's depth is the longest chain of datasets that feeds it, so
    snapshots come first. Ties are broken by URI. If datasets feed each other
    in a cycle, this falls back to sorting by URI.
    """
    feeds: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for step, deps in dag.items():
        key = _dataset_key(step)
        feeds.setdefault(key, set())
        for dep in deps:
            dep_key = _dataset_key(dep)
            feeds.setdefault(dep_key, set())
            # one version of a dataset may be built from an earlier one
            if dep_key != key:
                feeds[key].add(dep_key)

    try:
        order = list(graphlib.TopologicalSorter(feeds).static_order())
    except graphlib.CycleError:
        # two datasets can each be built from a version of the other without
        # any step depending on itself; there's no dataset order to give then
        return sorted(dag)

    depth: dict[tuple[str, str], int] = {}
    for key in order:
        depth[key] = 1 + max((depth[d] for d in feeds[key]), default=-1)

    return sorted(dag, key=lambda s: (depth[_dataset_key(s)], s.uri))


def _dataset_key(step: StepURI) -> tuple[str, str]:
    "Every version of a dataset, `latest` and `*` included, shares this key."
    return (step.scheme, step.base_path)
