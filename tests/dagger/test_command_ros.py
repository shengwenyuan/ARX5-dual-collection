from __future__ import annotations

import unittest

from arx5_collection.dagger.command_ros import RosDualArmControlPort


class RosDualArmControlPortTest(unittest.TestCase):
    def test_vendor_topics_require_explicit_authorization(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit authorization"):
            RosDualArmControlPort(
                ("/arm_master_l_status", "/arm_master_r_status")
            )

        port = RosDualArmControlPort(
            ("/arm_master_l_status", "/arm_master_r_status"),
            allow_vendor_commands=True,
        )
        self.assertEqual(port.command_topics[0], "/arm_master_l_status")

    def test_loopback_topics_do_not_enable_vendor_output(self) -> None:
        port = RosDualArmControlPort(
            ("/test/dagger/left_command", "/test/dagger/right_command")
        )
        self.assertEqual(
            port.command_topics,
            ("/test/dagger/left_command", "/test/dagger/right_command"),
        )

    def test_rejects_relative_topics_and_invalid_timeout(self) -> None:
        with self.assertRaises(ValueError):
            RosDualArmControlPort(("left", "/test/right"))
        with self.assertRaises(ValueError):
            RosDualArmControlPort(
                ("/test/left", "/test/right"),
                state_timeout_s=0,
            )


if __name__ == "__main__":
    unittest.main()
