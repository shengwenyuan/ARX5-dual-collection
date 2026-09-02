from __future__ import annotations

import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).parents[1] / "src/ros2/arx5_arm_adapter"
sys.path.insert(0, str(PACKAGE_ROOT))

from arx5_arm_adapter.mapping import map_robot_status_values  # noqa: E402


def test_robot_status_is_split_without_conversion() -> None:
    result = map_robot_status_values(
        [0.1, -0.2, 0.3, -0.4, 0.5, -0.6],
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
        [-1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -7.0],
        [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07],
    )

    assert result.eef_xyzrpy == (0.1, -0.2, 0.3, -0.4, 0.5, -0.6)
    assert result.joint_positions == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    assert result.joint_velocities == (-1.0, -2.0, -3.0, -4.0, -5.0, -6.0)
    assert result.joint_currents == (0.01, 0.02, 0.03, 0.04, 0.05, 0.06)
    assert result.gripper_position == 7.0
    assert result.gripper_velocity == -7.0
    assert result.gripper_current == 0.07


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("end_pos", [0.0] * 5),
        ("joint_pos", [0.0] * 6),
        ("joint_vel", [0.0] * 8),
        ("joint_cur", []),
    ],
)
def test_invalid_vector_width_is_rejected(field: str, values: list[float]) -> None:
    arguments = {
        "end_pos": [0.0] * 6,
        "joint_pos": [0.0] * 7,
        "joint_vel": [0.0] * 7,
        "joint_cur": [0.0] * 7,
    }
    arguments[field] = values

    with pytest.raises(ValueError, match=field):
        map_robot_status_values(**arguments)
