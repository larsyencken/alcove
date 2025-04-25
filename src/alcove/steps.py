import re
from typing import List

import graphlib

from alcove import snapshots, tables
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

    raise ValueError(f"Unknown scheme {step.scheme}")


def execute_dag(dag: Dag, dry_run: bool = False) -> None:
    "Execute the DAG."
    to_execute = in_topological_order(dag)
    print(f"Executing {len(to_execute)} steps")
    for step in to_execute:
        print(step)
        if not dry_run:
            execute_step(step, dag[step])


def execute_step(step: StepURI, dependencies: List[StepURI]) -> None:
    "Execute a single step."
    if step.scheme == "snapshot":
        return snapshots.Snapshot.load(step.path).fetch()

    elif step.scheme == "table":
        return tables.build_table(step, dependencies)

    else:
        raise ValueError(f"Unknown scheme {step.scheme}")


def in_topological_order(dag: Dag) -> List[StepURI]:
    # we need to retain dependencies, but not consider them as steps
    # included for execution
    return [
        step for step in graphlib.TopologicalSorter(dag).static_order() if step in dag
    ]
