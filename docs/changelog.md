# Changelog

- `dev`
    - Added wildcard `*` support in step URIs for date-partitioned tables (e.g. `table://foo/*` expands per snapshot version)
    - `AlcoveDB` now registers union views across all partitions of a wildcard group
    - Fixed DAG mutation bug in `plan_and_run` (steps dict is now copied before modification)

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
