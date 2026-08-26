from __future__ import annotations

from arx5_collection.cleaning.models import ArmSample
from arx5_collection.gripper import GripperCalibration


def make_state(
    left: ArmSample,
    right: ArmSample,
    left_gripper: GripperCalibration,
    right_gripper: GripperCalibration,
) -> tuple[float, ...]:
    return (
        *left.joint_positions,
        left_gripper.normalize(left.gripper_position),
        *right.joint_positions,
        right_gripper.normalize(right.gripper_position),
    )
