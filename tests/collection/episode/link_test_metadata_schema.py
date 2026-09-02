from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from test_metadata_schema import valid_metadata


def main() -> None:
    schema_path = Path(sys.argv[1])
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        valid_metadata()
    )
    print(f"schema={schema_path}")
    print("episode_metadata_schema_link=ok")


if __name__ == "__main__":
    main()
