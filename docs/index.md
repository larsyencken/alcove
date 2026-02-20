# Alcove

[![CI](https://github.com/larsyencken/alcove/actions/workflows/ci.yml/badge.svg)](https://github.com/larsyencken/alcove/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

_A personal ETL and data lake._

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

## Bugs

Please report any issues at: [github.com/larsyencken/alcove/issues](https://github.com/larsyencken/alcove/issues)
