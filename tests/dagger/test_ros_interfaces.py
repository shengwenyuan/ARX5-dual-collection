from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
INTERFACES = ROOT / "ros2_ws" / "src" / "arx5_collection_interfaces"


class DaggerRosInterfaceContractTest(unittest.TestCase):
    def test_only_sparse_authority_event_is_added_to_mcap_contract(self) -> None:
        message = (INTERFACES / "msg" / "AuthorityEvent.msg").read_text()
        for event in (
            "TAKEOVER_REQUESTED",
            "HUMAN_ACTIVE",
            "RESUME_REQUESTED",
            "POLICY_ACTIVE",
            "FAULT_HOLD",
        ):
            self.assertIn(event, message)
        self.assertIn("uint64 intervention_id", message)
        self.assertIn("uint64 control_epoch", message)
        self.assertIn("uint64 monotonic_time_ns", message)
        self.assertNotIn("observation", message.lower())
        self.assertNotIn("inference", message.lower())

    def test_cmake_has_generic_snapshot_service_but_no_policy_events(self) -> None:
        cmake = (INTERFACES / "CMakeLists.txt").read_text()
        self.assertIn('"msg/AuthorityEvent.msg"', cmake)
        self.assertIn('"srv/GetVlaSnapshot.srv"', cmake)
        self.assertNotIn("GetPi05Observation", cmake)
        self.assertNotIn("PolicyInference", cmake)
        self.assertNotIn("PolicyAction", cmake)

        service = (INTERFACES / "srv" / "GetVlaSnapshot.srv").read_text()
        self.assertIn("sensor_msgs/Image camera_left", service)
        self.assertIn("arx5_collection_interfaces/ArmState left_arm", service)
        self.assertNotIn("pi05", service.lower())
        self.assertNotIn("inference", service.lower())


if __name__ == "__main__":
    unittest.main()
