# alcove

[![CI](https://github.com/larsyencken/alcove/actions/workflows/ci.yml/badge.svg)](https://github.com/larsyencken/alcove/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

_A personal ETL and data lake._

Status: in alpha, changing often

## Overview

Alcove is an opinionated small-scale ETL framework for managing data files and directories in a content-addressable way.

## Core principles

- **A reusable framework.** Alcove provides a structured way of managing data files, scripts and their interdependencies that can be used across multiple projects.
- **First class metadata.** Every data file has an accompanying metadata sidecar that can be used to store provenance, licensing and other information.
- **Content addressed.** An `alcove` DAG is a Merkle tree of checksums that includes data, metadata and scripts, used to lazily rebuild only what is out of date.
- **Data versioning.** Every step in the DAG has a URI that includes a version, which can be an ISO date or `latest`, to encourage a reproducible workflow that still allows for change.
- **SQL support.** Alcove is a Python framework, but allows you to write steps in SQL which will be executed by DuckDB.
- **Parquet interchange.** All derived tables are generated as Parquet, which makes reuse easier.

## Quick Start

```bash
# Install alcove
pip install alcove  # or: uv add alcove

# Initialize a new alcove
mkdir my-data-project && cd my-data-project
alcove init

# Add a data file to your alcove
alcove snapshot ~/Downloads/countries.csv countries/latest

# Create a derived table
alcove new-table derived/population.sql countries/latest

# Build all tables
alcove run

# Explore your data with DuckDB
alcove db
```

## Usage

### Install the package

You can install the `alcove` package from PyPI using pip, uv, or any other Python package manager:

```bash
# Using pip
pip install alcove

# Using uv (recommended)
uv add alcove

# For development
uv add --dev alcove
```

You can also install directly from GitHub for the latest development version:

```bash
pip install git+https://github.com/larsyencken/alcove
```

### Using Alcove in your project

#### Starting a new project

To start a new project with alcove:

```bash
# Create and navigate to your project directory
mkdir my-data-project
cd my-data-project

# Set up your Python environment (optional, but recommended)
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install alcove
pip install alcove

# Or with uv
uv add alcove

# Initialize the alcove
alcove init
```

#### Adding to an existing project

To add alcove to an existing project:

```bash
# Navigate to your project directory
cd your-project

# Install alcove
uv add alcove   # Or pip install alcove

# Initialize alcove in a subdirectory (optional)
mkdir data
cd data
alcove init
```

### Initialise an alcove

From the folder where you want to store your data and metadata, run:

```bash
alcove init
```

This will create a `alcove.yaml` file, which will serve as the catalogue of all the data in your alcove.

### Configure object storage

You will need to configure your S3-compatible storage credentials in a `.env` file, in the same directory as your `alcove.yaml` file. Define:

```
S3_ACCESS_KEY=your_application_key_id
S3_SECRET_KEY=your_application_key
S3_BUCKET_NAME=your_bucket_name
S3_ENDPOINT_URL=your_endpoint_url
```

Now your alcove is ready to use.

### Adding a file or folder

From within your alcove folder, use the `snapshot` command to add a file or folder to your alcove:

```bash
alcove snapshot path/to/your/file_or_folder dataset_name
```

For example:

```bash
alcove snapshot ~/Downloads/countries.csv countries/latest
```

This will upload the file to your S3-compatible storage, and create a metadata file at `data/<dataset_name>.meta.yaml` directory for you to complete.

The metadata format has some minimum fields, but is meant for you to extend as needed for your own purposes. Best practice would be to retain the provenance and licence information of any data you add to your alcove, especially if it originates from a third party.

### Creating a new table

To create a new table, use the `new-table` command:

```bash
alcove new-table <table-path> [dep1 [dep2 [...]]]
```

This creates a placeholder executable script that generates an example data file based on the file extension (.parquet or .sql).

#### Creating a Parquet table

```bash
alcove new-table path/to/your/table
```

This creates a placeholder Python script that generates an example Parquet file:

```python
#!/usr/bin/env python3
import sys
import polars as pl

data = {
    "a": [1, 1, 3],
    "b": [2, 3, 5],
    "c": [3, 4, 6]
}

df = pl.DataFrame(data)

output_file = sys.argv[-1]
df.write_parquet(output_file)
```

#### Creating a SQL table

```bash
alcove new-table path/to/your/table.sql
```

This creates a placeholder SQL script:

```sql
-- SQL script to create a table
CREATE TABLE example_table AS
SELECT
    1 AS a,
    2 AS b,
    3 AS c
```

#### Opening in your editor

The command also supports the `--edit` option to open the metadata file in your editor:

```bash
alcove new-table path/to/your/table --edit
```

### Executing SQL step definitions

If a `.sql` step definition is detected, it will be executed using DuckDB with an in-memory database. The SQL file can use `{variable}` to interpolate template variables. The following template variables are available:

- `{output_file}`: The path to the output file.
- `{dependency}`: The path of each dependency, simplified to a semantic name.

### Command Reference

Alcove provides the following commands:

- `alcove init` - Initialize a new alcove workspace
- `alcove snapshot <path> <dataset>` - Add a file or directory to your alcove
- `alcove run` - Build all tables and fetch outdated data
- `alcove list` - List all datasets in alphabetical order
- `alcove audit` - Validate the alcove metadata
- `alcove new-table <path> [deps...]` - Create a new derived table
- `alcove db [query]` - Open a DuckDB shell or execute a query
- `alcove export-duckdb <file>` - Export tables to a DuckDB file

### Building your alcove

Run the `run` command to fetch any data that's out of date and build any derived tables:

```bash
alcove run
```

## Development

### Testing with MinIO

For testing with S3-compatible storage, this project uses automatically managed containers:

```bash
# Run tests with Docker-based MinIO
make test
```

All tests require Docker with MinIO container to be available.

### Docker Context Support

The testing framework automatically detects your current Docker context and uses it for container operations. This ensures tests work properly with:
- Docker Desktop
- Colima
- OrbStack
- Remote Docker contexts

### MinIO Configuration

With Docker, these credentials are automatically used:
- Access Key: minioadmin
- Secret Key: minioadmin
- Bucket: test-bucket
- Endpoint: http://localhost:9000

Containers are automatically managed and kept running between test runs for performance.
MinIO's health is verified before tests run to ensure proper S3 compatibility.

## Bugs

Please report any issues at: https://github.com/larsyencken/alcove/issues

## Changelog

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
