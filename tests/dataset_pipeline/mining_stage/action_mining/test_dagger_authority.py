from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    AuthorityEventRecord,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.dagger_authority import (
    classify_dagger_episode,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.dagger_authority.classifier import (
    AuthorityAlignmentPolicy,
)


class DaggerPipelineTest(unittest.TestCase):
    def test_reader_failure_handles_non_object_dagger_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            episode = root / "episode-a"
            episode.mkdir()
            (episode / "metadata.json").write_text(
                json.dumps({"episode_id": episode.name, "dagger": "invalid"})
            )
            audit = root / "audit"
            (audit / episode.name).mkdir(parents=True)
            (audit / episode.name / "quality.json").write_text("{}")

            def invalid_reader(_episode: Path) -> tuple[AuthorityEventRecord, ...]:
                raise TypeError("invalid event")

            result, output = classify_dagger_episode(
                episode,
                audit,
                AuthorityAlignmentPolicy(1_000, 2_000_000),
                event_reader=invalid_reader,
            )

            self.assertFalse(result.valid)
            self.assertEqual(result.intervention_count, 0)
            self.assertEqual(result.issues, ("authority reader failed: invalid event",))
            self.assertTrue((output / "quality.json").is_file())


if __name__ == "__main__":
    unittest.main()
