from __future__ import annotations

from datetime import date
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from arx5_collection.streaming_conversion.alignment import AlignmentCancelled
from arx5_collection.streaming_conversion.alignment import build_alignment_report
from arx5_collection.streaming_conversion.alignment import render_alignment
from arx5_collection.streaming_conversion.alignment import require_enter_confirmation
from arx5_collection.streaming_conversion.config import OutputConfig
from arx5_collection.streaming_conversion.config import RecipeConfig
from arx5_collection.streaming_conversion.config import RuntimeConfig
from arx5_collection.streaming_conversion.config import SourceConfig
from arx5_collection.streaming_conversion.config import StreamingConversionConfig
from arx5_collection.streaming_conversion.models import DiscoveryResult


class _Input(StringIO):
    def __init__(self, value: str, *, tty: bool = True) -> None:
        super().__init__(value)
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class AlignmentTest(unittest.TestCase):
    def test_only_empty_tty_line_confirms(self) -> None:
        output = StringIO()
        require_enter_confirmation(_Input("\n"), output)
        self.assertIn("ENTER", output.getvalue())

        for stream, reason in (
            (_Input("yes\n"), "non-empty"),
            (_Input(""), "EOF"),
            (_Input("\n", tty=False), "interactive TTY"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(AlignmentCancelled, reason):
                    require_enter_confirmation(stream, StringIO())

    def test_renders_deterministic_read_only_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = StreamingConversionConfig(
                schema_version=1,
                source=SourceConfig(root, (Path("task/date"),), ("logs",)),
                runtime=RuntimeConfig(Path("/tmp/streaming"), 25),
                output=OutputConfig(Path("/tmp/lerobot"), "fold_cloth", "local/fold"),
                recipe=RecipeConfig(
                    "pi05-equal-eef-v2",
                    "recipe.toml",
                    "folding the cloth",
                ),
            )
            discovery = DiscoveryResult(root, (root,), (), (Path("task/date/logs"),))
            report = build_alignment_report(config, discovery, today=date(2026, 8, 25))

            rendered = render_alignment(report)

        self.assertIn("workers: 25", rendered)
        self.assertIn("training_task: folding the cloth", rendered)
        self.assertIn("episodes: 0", rendered)
        self.assertIn("fold_cloth_2026-08-25", rendered)
        self.assertIn("blocked_dirs: 1", rendered)

    def test_rejects_relative_output_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = StreamingConversionConfig(
                1,
                SourceConfig(root, (Path("task"),), ()),
                RuntimeConfig(Path("/tmp/streaming"), 1),
                OutputConfig(Path("/tmp/out"), "dataset", "local/data"),
                RecipeConfig("recipe", "recipe.toml", "folding the cloth"),
            )
            discovery = DiscoveryResult(root, (root,), (), ())

            with self.assertRaisesRegex(ValueError, "absolute"):
                build_alignment_report(config, discovery, Path("relative"))


if __name__ == "__main__":
    unittest.main()
