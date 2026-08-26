from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from arx5_collection.streaming_conversion.recipe import Pi05ConversionRecipe


RECIPE = Path("config/conversion.pi05-equal-eef-v3.toml")


class Pi05ConversionRecipeTest(unittest.TestCase):
    def test_loads_frozen_v3_recipe(self) -> None:
        recipe = Pi05ConversionRecipe.load(RECIPE)

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
            "grade_b_coverage = 0.95",
            "grade_b_coverage = 0.95\nextra = 1",
            "keys must be exactly",
        )

    def test_rejects_implicit_numeric_coercion(self) -> None:
        self._reject_replacement(
            "action_horizon = 50",
            'action_horizon = "50"',
            "must be an integer",
        )

    def _reject_replacement(self, old: str, new: str, message: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.toml"
            path.write_text(RECIPE.read_text().replace(old, new))
            with self.assertRaisesRegex(ValueError, message):
                Pi05ConversionRecipe.load(path)


if __name__ == "__main__":
    unittest.main()
