from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from arx5_collection.streaming_conversion.recipe import Pi05ConversionRecipe


RECIPE = Path("config/conversion.pi05-equal-eef-v2.toml")


class Pi05ConversionRecipeTest(unittest.TestCase):
    def test_loads_frozen_v2_recipe(self) -> None:
        recipe = Pi05ConversionRecipe.load(RECIPE)

        self.assertEqual(recipe.name, "pi05-equal-eef-v2")
        self.assertEqual(recipe.builder_backend, "lerobot-v2.1")
        self.assertEqual(recipe.gripper_normalization, "linear_open_closed_0_1")
        self.assertEqual(recipe.cleaning.cross_camera_tolerance_ns, 16_700_000)
        self.assertEqual(recipe.selection.eef_distance_m, 0.005)
        self.assertEqual(recipe.selection.action_horizon, 50)
        w3 = recipe.calibration_for("w3")
        w4 = recipe.calibration_for("w4")
        self.assertEqual(w3.left.open_value, -2.7309837341)
        self.assertEqual(w3.right.open_value, -2.4361028671)
        self.assertEqual(w3.left.tolerance, 0.001)
        self.assertEqual(w4.left.open_value, -3.061722755432129)
        self.assertEqual(w4.right.open_value, -3.3894100189208984)
        self.assertEqual(w4.left.tolerance, 0.05)

    def test_rejects_unknown_station(self) -> None:
        recipe = Pi05ConversionRecipe.load(RECIPE)
        with self.assertRaisesRegex(ValueError, "no gripper calibration"):
            recipe.calibration_for("w5")

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
