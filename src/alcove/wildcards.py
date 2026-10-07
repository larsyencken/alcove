"""Expand wildcard steps (e.g. table://foo/*) into concrete per-version steps."""

import graphlib

from alcove.types import Dag, StepURI


def expand_wildcards(dag: Dag) -> tuple[Dag, dict[str, list[str]]]:
    """Expand wildcard step URIs into concrete per-version steps.

    Returns (expanded_dag, wildcard_groups) where wildcard_groups maps
    scheme://base_path to the list of discovered versions.
    """
    expanded: Dag = dict(dag)
    wildcard_groups: dict[str, list[str]] = {}

    # Wildcard groups known to be empty on purpose: a partitioned snapshot
    # dataset declared as `snapshot://foo/*` before its first partition
    # exists, and wildcard steps with nothing to build yet. They expand to no
    # steps, where an undeclared group with no versions is an error.
    empty_groups = {
        _group_key(s)
        for s in dag
        if s.scheme == "snapshot" and s.is_wildcard and not dag[s]
    }

    # Process wildcard step keys in topological order so chained wildcards work
    topo_order = list(graphlib.TopologicalSorter(expanded).static_order())
    wildcard_keys = [s for s in topo_order if s in expanded and s.is_wildcard]

    for wc_step in wildcard_keys:
        wc_deps = expanded[wc_step]
        group_key = _group_key(wc_step)
        wildcard_deps = [d for d in wc_deps if d.is_wildcard]

        # Versions come from the step's own concrete siblings or, failing that,
        # from its wildcard deps: one step per version that every one of them
        # has, so a step fed by two daily datasets waits for a day until both
        # have it, instead of depending on a version that doesn't exist.
        versions = set(_discover_versions(wc_step, expanded))
        if not versions and wildcard_deps:
            dep_versions = []
            for dep in wildcard_deps:
                found = set(_discover_versions(dep, expanded))
                if not found and _group_key(dep) not in empty_groups:
                    raise ValueError(
                        f"Wildcard dependency {dep} of {wc_step} "
                        "matched zero concrete versions"
                    )
                dep_versions.append(found)

            versions = set.intersection(*dep_versions)

        if not versions:
            if group_key in empty_groups or wildcard_deps:
                # nothing to build until the data it reads arrives
                empty_groups.add(group_key)
                del expanded[wc_step]
                continue

            raise ValueError(f"Wildcard step {wc_step} matched zero concrete versions")

        wildcard_groups[group_key] = sorted(versions)

        # Also record wildcard groups for wildcard deps
        for dep in wc_deps:
            if dep.is_wildcard:
                dep_key = f"{dep.scheme}://{dep.base_path}"
                if dep_key not in wildcard_groups:
                    dep_versions = _discover_versions(dep, expanded)
                    if dep_versions:
                        wildcard_groups[dep_key] = sorted(dep_versions)

        # Create one concrete step per version
        for v in versions:
            concrete = wc_step.with_version(v)
            concrete_deps = [
                dep.with_version(v) if dep.is_wildcard else dep for dep in wc_deps
            ]
            expanded[concrete] = concrete_deps

        del expanded[wc_step]

    # Expand wildcard deps on concrete steps (e.g. summary/2025-02 depends on foo/*)
    for step in list(expanded):
        if step.is_wildcard:
            continue
        new_deps = []
        for dep in expanded[step]:
            if dep.is_wildcard:
                dep_key = f"{dep.scheme}://{dep.base_path}"
                if dep_key not in wildcard_groups:
                    versions = _discover_versions(dep, expanded)
                    if not versions:
                        raise ValueError(
                            f"Wildcard dependency {dep} on step {step} "
                            f"matched zero concrete versions"
                        )
                    wildcard_groups[dep_key] = sorted(versions)

                for v in wildcard_groups[dep_key]:
                    new_deps.append(dep.with_version(v))
            else:
                new_deps.append(dep)
        expanded[step] = new_deps

    # Validate no wildcards remain
    for step in expanded:
        if step.is_wildcard:
            raise ValueError(f"Wildcard step {step} was not expanded")
        for dep in expanded[step]:
            if dep.is_wildcard:
                raise ValueError(f"Wildcard dep {dep} on {step} was not expanded")

    return expanded, wildcard_groups


def _group_key(step: StepURI) -> str:
    return f"{step.scheme}://{step.base_path}"


def _discover_versions(wc_step: StepURI, dag: Dag) -> list[str]:
    """Find all versions for a wildcard step by scanning the DAG for concrete
    steps that share the same scheme and base_path."""
    base = wc_step.base_path
    scheme = wc_step.scheme
    versions = set()

    # Look at all steps (keys) in the DAG
    for step in dag:
        if step == wc_step:
            continue
        if step.scheme == scheme and step.base_path == base and not step.is_wildcard:
            versions.add(step.version)

    # Also look at deps for matching concrete URIs
    for deps in dag.values():
        for dep in deps:
            if dep.scheme == scheme and dep.base_path == base and not dep.is_wildcard:
                versions.add(dep.version)

    return list(versions)
