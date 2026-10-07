# Changelog

- `0.5.0` (2026-10-07)
    - Snapshot datasets declared once as `snapshot://foo/*` are date-partitioned: each day is snapshotted as `foo/YYYY-MM-DD`, is discovered from its metadata file rather than listed in `alcove.yaml`, records when it was fetched, and is covered by a single `data/.gitignore` pattern; `alcove run` stops and names any day missing between a dataset's first partition and its last (see [Wildcards](wildcards.md#daily-partitions))
    - `alcove run` moves partition-shaped snapshot data that has no metadata (such as a partition dropped on another clone, or missing from an older branch) into the dataset's `.orphaned/` folder rather than letting its glob read it, restores it when a partition with that content returns, and deletes per-partition tables and artifacts whose version no longer exists; `--dry-run` reports what it would do
    - A wildcard step with wildcard dependencies is built for the versions they all have; previously it took the first dependency's versions (so a lagging second dependency failed with a `KeyError`), and another step referring to one of its versions directly narrowed it to that version
    - A declared partitioned dataset with no partitions yet, and wildcard steps fed by it, expand to no steps; `AlcoveDB` skips union views for wildcard tables with nothing built
    - `alcove audit` warns about snapshot metadata files that no step refers to
    - Fixed tables and artifacts not rebuilding when a `latest` dependency resolves to a newer version, or when a wildcard dependency gains or loses a partition ([#70](https://github.com/larsyencken/alcove/issues/70)); steps that were stale this way rebuild on the first run after upgrading
    - Fixed `latest` resolving to a sibling dataset that shares a name prefix, and failing with a bare `max()` error when there are no versions ([#69](https://github.com/larsyencken/alcove/issues/69))
    - Changed the template value for several single-file snapshot versions to include their extension (`data/snapshots/foo/????-??-??.csv`), matching what a single version already gave, so one recipe (`'{foo}'`) works for both; recipes that appended the extension themselves (`'{foo}.csv'`) need updating
    - Added `Alcove.versions()` to list a dataset's versions

- `0.4.0` (2026-09-22)
    - Added `artifact://` steps for derived outputs that are not tables (e.g. rendered dashboards, models), built by a Python script into `data/artifacts/<path>/`
    - Added `alcove new-artifact <path> [deps...]`
    - Steps can be folders with a `__main__.py` entrypoint; every file in the folder (following symlinks, skipping caches and editor debris) is checksummed as an input, so templates and helper modules trigger rebuilds, and step scripts run with bytecode caching disabled
    - Fixed config validation rejecting ISO-date versions (e.g. `snapshot://foo/2024-09-04`) as dependencies; hyphens are now allowed in the version segment only

- `0.3.0`
    - Added wildcard `*` support in step URIs for date-partitioned tables (e.g. `table://foo/*` expands per snapshot version)
    - `AlcoveDB` now registers union views across all partitions of a wildcard group
    - Fixed DAG mutation bug in `plan_and_run` (steps dict is now copied before modification)
    - Table names now require underscores instead of dashes
    - Added documentation site with Zensical

- `0.2.2`
    - Fixed `snapshot --force` failing with `FileExistsError` when overwriting directory snapshots
    - Added programmatic API: `alcove.connect()` returns an `AlcoveDB` that queries tables as Polars DataFrames
    - Enforced `dim_` columns as composite primary key when new tables are generated

- `0.2.1` (2025-04-28)
    - Fixed gitignore handling by using `data/.gitignore` instead of `.data-files`
    - Always include `tables/` in `data/.gitignore`
    - `alcove audit --fix` now migrates patterns from `.gitignore` and `.data-files` to `data/.gitignore`

- `0.2.0` (2025-04-28)
    - Added `.data-files` file for managing alcove data ignores (#61)
    - `alcove init` now creates empty `.data-files` and ensures it's in `.gitignore`
    - `alcove audit --fix` can move patterns from `.gitignore` to `.data-files`
    - Prevents `.gitignore` from changing frequently with data file updates

- `0.1.2` (2025-04-25)
    - Fixed B2 compatibility with recent boto3 versions by disabling checksum validation (#60)
    - Simplified testing approach by always requiring Docker with MinIO
    - Added PyPI package configuration and installation instructions
    - Improved documentation with quick start guide and command reference

- `0.1.1` (2025-04-25)
    - Renamed project from "shelf" to "alcove"
    - Added automated Docker container management for testing with MinIO
    - Enhanced Docker context support for different environments (Docker Desktop, Colima, OrbStack)
    - Improved S3-compatible storage testing reliability
    - Fixed test fixtures to use consistent credentials

- `0.1.0` (Initial release)
    - Initialise a repo with `shelf.yaml`
    - `shelf snapshot` and `shelf run` with file and directory support
    - Only fetch things that are out of date
    - `shelf list` to see what datasets are available
    - `shelf audit` to ensure your alcove is coherent and correct
    - `shelf db` to enter an interactive DuckDB shell with all your data
