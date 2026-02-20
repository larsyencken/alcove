# Getting Started

## Install the package

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

## Starting a new project

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

## Adding to an existing project

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

## Initialise an alcove

From the folder where you want to store your data and metadata, run:

```bash
alcove init
```

This will create a `alcove.yaml` file, which will serve as the catalogue of all the data in your alcove.

## Configure object storage

You will need to configure your S3-compatible storage credentials in a `.env` file, in the same directory as your `alcove.yaml` file. Define:

```
S3_ACCESS_KEY=your_application_key_id
S3_SECRET_KEY=your_application_key
S3_BUCKET_NAME=your_bucket_name
S3_ENDPOINT_URL=your_endpoint_url
```

Now your alcove is ready to use.

## Adding a file or folder

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
