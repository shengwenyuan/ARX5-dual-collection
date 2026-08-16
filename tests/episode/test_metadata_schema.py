from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError


SCHEMA_PATH = Path(__file__).parents[2] / "schemas" / "episode-metadata-v1.json"


def valid_metadata() -> dict[str, object]:
    return {
        "schema_version": 1,
        "episode_id": "episode-001",
        "task": {"id": "pick", "description": "Pick the object"},
        "outcome": "success",
        "timing": {
            "started_at": "2026-08-16T01:00:00Z",
            "ended_at": "2026-08-16T01:01:30Z",
            "duration_s": 90.0,
        },
        "station": {
            "id": None,
            "config_schema_version": 1,
            "devices": [
                {
                    "id": "left_arm",
                    "kind": "arm",
                    "serial_number": "0045002B5330530320323656",
                    "configuration": {"can_interface": "can1"},
                },
                {
                    "id": "camera_left",
                    "kind": "camera",
                    "serial_number": None,
                    "configuration": {},
                },
            ],
        },
        "streams": [
            {
                "id": "camera_left_color",
                "topic": "/sensors/camera_left/color/image_raw",
                "required": True,
                "expected_hz": 30.0,
                "message_count": 2700,
                "observed_hz": 30.0,
                "max_gap_ms": 35.0,
                "warnings": [],
            }
        ],
        "calibration": {"intrinsics": None, "extrinsics": None},
        "software": {"name": "arx5-dual-collection", "version": "0.1.0"},
        "errors": [],
        "extensions": {},
    }


class MetadataSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = Draft202012Validator(
            cls.schema,
            format_checker=FormatChecker(),
        )

    def test_schema_is_valid_draft_2020_12(self) -> None:
        Draft202012Validator.check_schema(self.schema)

    def test_all_outcomes_are_valid(self) -> None:
        for outcome in ("success", "fail", "aborted"):
            with self.subTest(outcome=outcome):
                metadata = valid_metadata()
                metadata["outcome"] = outcome
                self.validator.validate(metadata)

    def test_unknown_device_values_and_open_extensions_are_valid(self) -> None:
        metadata = valid_metadata()
        metadata["extensions"] = {"future_feature": {"enabled": True}}
        self.validator.validate(metadata)

    def test_invalid_core_values_are_rejected(self) -> None:
        cases: list[dict[str, object]] = []

        missing_task = valid_metadata()
        del missing_task["task"]
        cases.append(missing_task)

        invalid_outcome = valid_metadata()
        invalid_outcome["outcome"] = "complete"
        cases.append(invalid_outcome)

        negative_count = valid_metadata()
        negative_count["streams"][0]["message_count"] = -1  # type: ignore[index]
        cases.append(negative_count)

        local_time = valid_metadata()
        local_time["timing"]["started_at"] = "2026-08-16T09:00:00+08:00"  # type: ignore[index]
        cases.append(local_time)

        unknown_core_field = valid_metadata()
        unknown_core_field["frame_count"] = 2700
        cases.append(unknown_core_field)

        for metadata in cases:
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValidationError):
                    self.validator.validate(metadata)

    def test_calibration_is_a_required_null_stub(self) -> None:
        metadata = valid_metadata()
        metadata["calibration"] = {"intrinsics": "calibration.json", "extrinsics": None}
        with self.assertRaises(ValidationError):
            self.validator.validate(metadata)

    def test_device_configuration_is_the_only_open_device_object(self) -> None:
        metadata = copy.deepcopy(valid_metadata())
        metadata["station"]["devices"][0]["configuration"]["sdk_type"] = 2  # type: ignore[index]
        self.validator.validate(metadata)

        metadata["station"]["devices"][0]["firmware"] = "unknown"  # type: ignore[index]
        with self.assertRaises(ValidationError):
            self.validator.validate(metadata)


if __name__ == "__main__":
    unittest.main()
