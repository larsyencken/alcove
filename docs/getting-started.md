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

## Choose where data is stored

By default, alcove keeps snapshot data in an S3-compatible bucket. You can choose where when you initialise the alcove:

```bash
alcove init                   # an S3-compatible bucket (the default)
alcove init --storage local   # a directory on this machine, .alcove/blobs by default
alcove init --storage git     # the git repo the alcove is in
```

The choice is saved as `storage` in `alcove.yaml`, so everyone working on the alcove uses the same store. An `alcove.yaml` without a `storage` key uses S3.

**S3.** Configure your S3-compatible storage credentials in a `.env` file, in the same directory as your `alcove.yaml` file. Define:

```
S3_ACCESS_KEY=your_application_key_id
S3_SECRET_KEY=your_application_key
S3_BUCKET_NAME=your_bucket_name
S3_ENDPOINT_URL=your_endpoint_url
```

Files downloaded from S3 are also cached in `~/.cache/alcove`.

**Local.** Data is kept in a directory, laid out as in the bucket, with no credentials or network needed. `--path` picks another directory, relative to `alcove.yaml`. The default `.alcove/` is added to `.gitignore`; a directory you choose isn't. To keep the store elsewhere on just one machine, such as on an encrypted volume, set `ALCOVE_LOCAL_STORE=/path/to/store` in `.env`. Local storage doesn't use the `~/.cache/alcove` cache.

**Git.** Data is kept in the repo's own object store, byte for byte, under the ref `refs/alcove/blobs`; no branch is touched. Plain `git clone` and `git push` don't copy that ref, so the data leaves the machine only if you ask for it:

```bash
git push origin 'refs/alcove/*:refs/alcove/*'    # share the data
git fetch origin 'refs/alcove/*:refs/alcove/*'   # get it in another clone
git bundle create incident.bundle --all          # code and data in one file
```

Git storage suits small to medium data. Git compresses each file as it's stored, which is slow for large ones (about 18 s per GB), and reading a file back needs memory about the size of the file, for each of the files `alcove run` fetches at once (`--jobs`). Hosts also limit file size (100 MB on GitHub). Git storage doesn't use the `~/.cache/alcove` cache either.

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

This will add the file to your alcove's store, and create a metadata file at `data/<dataset_name>.meta.yaml` directory for you to complete.

The metadata format has some minimum fields, but is meant for you to extend as needed for your own purposes. Best practice would be to retain the provenance and licence information of any data you add to your alcove, especially if it originates from a third party.
