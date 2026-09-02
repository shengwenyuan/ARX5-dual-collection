from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from arx5_collection.dataset_pipeline.configuration.recipe import DatasetPipelineRecipe
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.registry import (
    DATASET_UNIT_RUNNERS,
)
from arx5_collection.dataset_pipeline.execution.episode_pipeline import (
    EPISODE_UNIT_RUNNERS,
)


RECIPE = "builtin:pi05-equal-eef-v3"
SVT_RECIPE = "builtin:pi05-equal-eef-v3-svt-p8"
RECIPE_ROOT = Path(__file__).parents[2] / "config/specs/recipes"
PACKAGE_ROOT = Path("src/arx5_collection/dataset_pipeline")
UNIT_ROOT = Path("src/arx5_collection/dataset_pipeline/mining_stage")
RECIPE_TEXT = RECIPE_ROOT.joinpath("pi05-equal-eef-v3.toml").read_text()
SVT_RECIPE_TEXT = RECIPE_ROOT.joinpath("pi05-equal-eef-v3-svt-p8.toml").read_text()


class DatasetPipelineRecipeTest(unittest.TestCase):
    def test_package_root_only_contains_public_entrypoints(self) -> None:
        self.assertEqual(
            {path.name for path in PACKAGE_ROOT.glob("*.py")},
            {"__init__.py", "application.py", "cli.py"},
        )

    def test_mining_stage_contains_every_configured_stage(self) -> None:
        recipe = DatasetPipelineRecipe.load(RECIPE)

        self.assertEqual(
            {
                path.name
                for path in UNIT_ROOT.iterdir()
                if path.is_dir() and path.name != "__pycache__"
            },
            {stage.name for stage in recipe.pipeline.stages},
        )

    def test_loads_frozen_v3_recipe(self) -> None:
        recipe = DatasetPipelineRecipe.load(RECIPE)

        self.assertEqual(recipe.name, "pi05-equal-eef-v3")
        self.assertEqual(recipe.builder_backend, "lerobot-v2.1")
        self.assertEqual(recipe.gripper_normalization, "linear_open_closed_0_1")
        self.assertEqual(recipe.cleaning.cross_camera_tolerance_ns, 16_700_000)
        self.assertEqual(recipe.selection.eef_distance_m, 0.005)
        self.assertEqual(recipe.selection.action_horizon, 50)
        self.assertEqual(recipe.gripper_contract, "arx5-gripper-v1")
        self.assertEqual(recipe.gripper.open_value, -3.4)
        self.assertEqual(recipe.gripper.closed_value, 0.0)
        self.assertEqual(recipe.gripper.open_tolerance, 0.05)
        self.assertEqual(recipe.gripper.closed_tolerance, 0.10)
        self.assertIsNone(recipe.video)
        self.assertEqual(
            tuple(stage.name for stage in recipe.pipeline.stages),
            (
                "episode_sanitycheck",
                "action_mining",
                "dataset_generator",
            ),
        )
        self.assertEqual(
            tuple(
                unit.type for unit in recipe.pipeline.stage("episode_sanitycheck").units
            ),
            (
                "metadata_check",
                "mcap_check",
                "timeline_check",
                "arm_signal_check",
                "frame_alignment",
                "alignment_report",
            ),
        )
        self.assertEqual(
            tuple(unit.type for unit in recipe.pipeline.stage("action_mining").units),
            (
                "dagger_authority",
                "episode_filter",
                "training_interval",
                "equal_eef_action_sampler",
                "motion_segmenter",
                "trajectory_labeler",
            ),
        )

    def test_loads_explicit_svt_policy(self) -> None:
        recipe = DatasetPipelineRecipe.load(SVT_RECIPE)

        self.assertEqual(recipe.schema_version, 3)
        self.assertIsNotNone(recipe.video)
        self.assertEqual(recipe.video.codec, "libsvtav1")
        self.assertEqual(recipe.video.pixel_format, "yuv420p")
        self.assertEqual(recipe.video.gop, 2)
        self.assertEqual(recipe.video.crf, 30)
        self.assertEqual(recipe.video.preset, 8)
        self.assertEqual(recipe.video.threads, 0)

    def test_every_configured_unit_has_a_registered_runner(self) -> None:
        recipe = DatasetPipelineRecipe.load(RECIPE)
        registered = set(EPISODE_UNIT_RUNNERS) | set(DATASET_UNIT_RUNNERS)
        configured = {
            unit.type for stage in recipe.pipeline.stages for unit in stage.units
        }

        self.assertLessEqual(configured, registered)

    def test_recipe_selects_units_without_code_level_required_sets(self) -> None:
        omitted = """[[stages.episode_sanitycheck.units]]
type = "alignment_report"
params = { grade_a_coverage = 0.99, grade_b_coverage = 0.95 }

"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.toml"
            path.write_text(RECIPE_TEXT.replace(omitted, ""))

            recipe = DatasetPipelineRecipe.load(path)

        self.assertNotIn(
            "alignment_report",
            {unit.type for unit in recipe.pipeline.stage("episode_sanitycheck").units},
        )
        with self.assertRaisesRegex(ValueError, "does not configure unit"):
            _ = recipe.cleaning

    def test_every_configured_unit_has_its_own_implementation(self) -> None:
        recipe = DatasetPipelineRecipe.load(RECIPE)

        for stage in recipe.pipeline.stages:
            for unit in stage.units:
                unit_module = UNIT_ROOT / stage.name / f"{unit.type}.py"
                unit_package = UNIT_ROOT / stage.name / unit.type
                with self.subTest(stage=stage.name, unit=unit.type):
                    self.assertNotEqual(unit_module.is_file(), unit_package.is_dir())
                    if unit_package.is_dir():
                        implementations = [
                            path
                            for path in unit_package.glob("*.py")
                            if path.name != "__init__.py"
                        ]
                        self.assertGreater(len(implementations), 1)

    def test_stage_modules_do_not_use_private_filenames(self) -> None:
        private_modules = sorted(
            path
            for stage in DatasetPipelineRecipe.load(RECIPE).pipeline.stages
            for path in (UNIT_ROOT / stage.name).rglob("_*.py")
            if path.name != "__init__.py"
        )

        self.assertEqual(private_modules, [])

    def test_rejects_invalid_svt_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.toml"
            path.write_text(SVT_RECIPE_TEXT.replace("preset = 8", "preset = 20"))
            with self.assertRaisesRegex(ValueError, "preset"):
                DatasetPipelineRecipe.load(path)

    def test_rejects_unknown_builtin_recipe(self) -> None:
        with self.assertRaisesRegex(ValueError, "configuration file does not exist"):
            DatasetPipelineRecipe.load("builtin:missing")

    def test_rejects_unknown_gripper_contract(self) -> None:
        self._reject_replacement(
            'gripper_contract = "arx5-gripper-v1"',
            'gripper_contract = "station-w3"',
            "gripper_contract",
        )

    def test_rejects_unknown_backend(self) -> None:
        self._reject_replacement(
            'builder_backend = "lerobot-v2.1"',
            'builder_backend = "lerobot-v3"',
            "builder_backend",
        )

    def test_rejects_unknown_field(self) -> None:
        self._reject_replacement(
            "params = { grade_a_coverage = 0.99, grade_b_coverage = 0.95 }",
            "params = { grade_a_coverage = 0.99, grade_b_coverage = 0.95, extra = 1 }",
            "keys must be exactly",
        )

    def test_rejects_implicit_numeric_coercion(self) -> None:
        self._reject_replacement(
            "action_horizon = 50",
            'action_horizon = "50"',
            "must be an integer",
        )

    def test_rejects_unknown_unit(self) -> None:
        self._reject_replacement(
            'type = "timeline_check"',
            'type = "unknown_check"',
            "unsupported episode_sanitycheck unit",
        )

    def _reject_replacement(self, old: str, new: str, message: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.toml"
            path.write_text(RECIPE_TEXT.replace(old, new))
            with self.assertRaisesRegex(ValueError, message):
                DatasetPipelineRecipe.load(path)


if __name__ == "__main__":
    unittest.main()
