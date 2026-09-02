from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from arx5_collection.dataset_pipeline.persistence.artifacts import (
    message_ref_from_artifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import (
    message_ref_to_artifact,
)
from arx5_collection.dataset_pipeline.persistence.artifacts import read_jsonl
from arx5_collection.dataset_pipeline.source.models import MessageRef


class ArtifactCodecTest(unittest.TestCase):
    def test_message_ref_round_trip_preserves_both_timestamps(self) -> None:
        reference = MessageRef("/topic", 7, 100, 105)

        self.assertEqual(
            message_ref_from_artifact(message_ref_to_artifact(reference)),
            reference,
        )

    def test_jsonl_rejects_non_object_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rows.jsonl"
            path.write_text("[]\n")

            with self.assertRaisesRegex(ValueError, "expected a JSON object"):
                read_jsonl(path)


if __name__ == "__main__":
    unittest.main()
