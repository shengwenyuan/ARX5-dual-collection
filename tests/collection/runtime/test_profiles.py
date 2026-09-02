from __future__ import annotations

import unittest

from arx5_collection.collection.runtime.profiles import (
    reset_specs_for,
    resolve_arm_profile,
)


class ArmStateProfileTest(unittest.TestCase):
    def test_resolves_teaching_and_dagger_vendor_topics(self) -> None:
        teaching = resolve_arm_profile("teaching")
        dagger = resolve_arm_profile("dagger")

        self.assertEqual(teaching.left_input_topic, "/arm_master_l_status")
        self.assertEqual(teaching.right_input_topic, "/arm_master_r_status")
        self.assertEqual(dagger.left_input_topic, "/arm_slave_l_status")
        self.assertEqual(dagger.right_input_topic, "/arm_slave_r_status")
        self.assertTrue(teaching.controller_launch.endswith("v2_collect.launch.py"))
        self.assertTrue(dagger.controller_launch.endswith("v2_joint_control.launch.py"))
        self.assertEqual(
            [spec.status_topic for spec in reset_specs_for(dagger)],
            ["/arm_slave_l_status", "/arm_slave_r_status"],
        )
        self.assertEqual(
            [spec.gravity_service for spec in reset_specs_for(dagger)],
            [
                "/arm_slave_l/gravity_compensation",
                "/arm_slave_r/gravity_compensation",
            ],
        )

    def test_rejects_unknown_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "supported profiles"):
            resolve_arm_profile("unknown")


if __name__ == "__main__":
    unittest.main()
