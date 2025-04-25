import json
import jsonschema
from pathlib import Path

SCHEMA_DIR = Path(__file__).parent / "schemas"

SNAPSHOT_SCHEMA_FILE = SCHEMA_DIR / "snapshot-v1.schema.json"
TABLE_SCHEMA_FILE = SCHEMA_DIR / "table-v1.schema.json"
ALCOVE_SCHEMA_FILE = SCHEMA_DIR / "alcove-v1.schema.json"
TABLE_CONFIG_SCHEMA_FILE = SCHEMA_DIR / "table-config-v1.schema.json"


def validate_snapshot(snapshot: dict) -> None:
    # Prune missing values
    pruned_snapshot = {k: v for k, v in snapshot.items() if v is not None}
    jsonschema.validate(pruned_snapshot, SNAPSHOT_SCHEMA)


SNAPSHOT_SCHEMA = json.loads(SNAPSHOT_SCHEMA_FILE.read_text())
TABLE_SCHEMA = json.loads(TABLE_SCHEMA_FILE.read_text())
TABLE_CONFIG_SCHEMA = json.loads(TABLE_CONFIG_SCHEMA_FILE.read_text())
ALCOVE_SCHEMA = json.loads(ALCOVE_SCHEMA_FILE.read_text())
