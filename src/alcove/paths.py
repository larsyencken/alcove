from pathlib import Path

# it's important that these are relative paths, which makes their
# evaluation lazy for unit testing
BASE_DIR = Path(".")
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
TABLE_DIR = DATA_DIR / "tables"
TABLE_SCRIPT_DIR = BASE_DIR / "src" / "steps" / "tables"
