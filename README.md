# alcove

[![CI](https://github.com/larsyencken/alcove/actions/workflows/ci.yml/badge.svg)](https://github.com/larsyencken/alcove/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

_A reproducible data pipeline for research and analysis._

## Overview

Alcove is a data pipeline framework designed for reproducible research, policy analysis, and data journalism. Think of it as a middle ground between ad-hoc Jupyter notebooks and enterprise ETL tools—providing the structure needed for reliable, citable analysis without the complexity of production data platforms.

Inspired by projects like Our World In Data's ETL system, Alcove helps researchers and analysts create transparent, versionable data workflows that can be shared, reproduced, and built upon.

## Why Alcove?

**For Researchers and Analysts:**
- Transform scattered scripts into a coherent, reproducible pipeline
- Ensure your analysis can be recreated months or years later
- Build on existing datasets with confidence in provenance
- Share methodology transparently with colleagues and reviewers

**Built for Research Workflows:**
- **Reproducible by Design:** Content-addressable storage ensures exact reproducibility
- **Rich Metadata:** Track data sources, licenses, access dates, and methodology notes
- **Version Control for Data:** Every dataset has explicit versions (ISO dates or semantic tags)
- **Flexible Execution:** Write transformations in SQL or Python as needed
- **Local-First:** Work offline, sync to cloud storage when ready
- **Open Standards:** Parquet output for maximum interoperability

## Core Principles

- **Transparency:** Every step in your pipeline is explicit and auditable
- **Provenance:** Track the complete lineage from raw data to final analysis  
- **Reproducibility:** Content-addressable storage prevents silent data changes
- **Collaboration:** Share datasets and methodologies with clear metadata
- **Simplicity:** Focus on analysis, not infrastructure management

## Quick Start

```bash
# Install alcove
pip install alcove  # or: uv add alcove

# Initialize a new research project
mkdir world-population-analysis && cd world-population-analysis
alcove init

# Add a dataset with proper metadata
alcove snapshot ~/Downloads/world-population-2024.csv population/2024-01-15

# Create a transformation pipeline
alcove new-table analysis/population-density.sql population/2024-01-15

# Build your analysis pipeline
alcove run

# Explore results interactively
alcove db
```

## Use Cases

**Research Publications:** Ensure your paper's analysis can be reproduced exactly
**Policy Analysis:** Track data sources and methodology for government reports  
**Data Journalism:** Build transparent, auditable data stories
**Academic Datasets:** Create shareable, well-documented research datasets
**Collaborative Research:** Share data pipelines with clear provenance

## Usage

## Installation and Setup

### Install from PyPI

```bash
# Using pip
pip install alcove

# Using uv (recommended)
uv add alcove
```

For the latest development version:
```bash
pip install git+https://github.com/larsyencken/alcove
```

### Initialize Your Research Project

```bash
# Create your project directory
mkdir my-research-project && cd my-research-project

# Initialize alcove
alcove init

# Configure S3-compatible storage (see below)
```

### Storage Configuration

Alcove uses S3-compatible storage for dataset sharing and backup. Create a `.env` file in your project directory:

```
S3_ACCESS_KEY=your_application_key_id
S3_SECRET_KEY=your_application_key
S3_BUCKET_NAME=your_bucket_name
S3_ENDPOINT_URL=your_endpoint_url
```

This enables sharing datasets with collaborators and ensures your analysis remains reproducible even if source files change.

## Working with Datasets

### Adding Source Data

Use semantic, dated versions for reproducibility:

```bash
# Add with explicit date version
alcove snapshot ~/Downloads/world-bank-gdp.csv gdp/2024-03-15

# Add the latest version of evolving data  
alcove snapshot ~/Documents/survey-responses.csv survey/latest
```

This creates a metadata file at `data/<dataset>.meta.yaml` where you should document:
- **Data source and URL**
- **Access date and method**
- **License information** 
- **Known limitations or caveats**

Proper metadata is crucial for research integrity and collaboration.

### Creating Analysis Steps

Transform your data with SQL or Python:

```bash
# Create a SQL transformation
alcove new-table analysis/cleaned-gdp.sql gdp/2024-03-15

# Create a Python analysis step  
alcove new-table analysis/correlation-matrix gdp/2024-03-15 survey/latest
```

This creates executable scripts with dependency tracking built-in.

#### SQL Transformations

SQL files are executed with DuckDB and support template variables:

```sql
-- analysis/cleaned-gdp.sql
SELECT 
    country,
    year,
    gdp_usd,
    population,
    gdp_usd / population as gdp_per_capita
FROM '{gdp}'  -- References the dependency
WHERE year >= 2020
    AND gdp_usd IS NOT NULL
```

#### Python Analysis Scripts  

Python scripts receive dependency file paths as arguments:

```python
#!/usr/bin/env python3
import sys
import polars as pl

# Load dependencies  
gdp_file = sys.argv[1]
survey_file = sys.argv[2]
output_file = sys.argv[-1]

# Your analysis here
gdp_df = pl.read_parquet(gdp_file)
survey_df = pl.read_parquet(survey_file)

result = analyze_correlation(gdp_df, survey_df)
result.write_parquet(output_file)
```

### Running Your Analysis Pipeline

Execute your complete analysis with dependency resolution:

```bash
# Build everything that's out of date
alcove run

# Explore your results interactively  
alcove db

# List all datasets in your project
alcove list

# Validate metadata and dependencies
alcove audit
```

## Command Reference

| Command | Purpose |
|---------|---------|  
| `alcove init` | Initialize a new research project workspace |
| `alcove snapshot <path> <dataset>` | Add a dataset with version tracking |
| `alcove run` | Execute analysis pipeline (only out-of-date steps) |
| `alcove list` | List all datasets and their versions |
| `alcove audit` | Validate metadata and data integrity |
| `alcove new-table <path> [deps...]` | Create new analysis step |
| `alcove db [query]` | Interactive DuckDB shell with all data loaded |
| `alcove export-duckdb <file>` | Export complete dataset to DuckDB file |

## Reproducibility Features

**Content Addressing:** Every dataset is identified by its content hash, preventing silent data corruption

**Explicit Versioning:** Use ISO dates (`gdp/2024-03-15`) or semantic versions (`survey/v2`) instead of ambiguous "latest" 

**Dependency Tracking:** Changes cascade automatically through your analysis pipeline

**Metadata Validation:** Rich schemas ensure complete provenance documentation

**Shareable Pipelines:** S3 storage enables collaboration while preserving exact reproducibility

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
- Endpoint: <http://localhost:9000>

Containers are automatically managed and kept running between test runs for performance.
MinIO's health is verified before tests run to ensure proper S3 compatibility.

## Bugs

Please report any issues at: <https://github.com/larsyencken/alcove/issues>

## Changelog

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
