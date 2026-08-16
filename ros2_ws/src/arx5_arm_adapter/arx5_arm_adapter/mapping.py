from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ArmStateValues:
    eef_xyzrpy: tuple[float, ...]
    joint_positions: tuple[float, ...]
    joint_velocities: tuple[float, ...]
    joint_currents: tuple[float, ...]
    gripper_position: float
    gripper_velocity: float
    gripper_current: float


def _fixed_vector(values: Iterable[float], width: int, field: str) -> tuple[float, ...]:
    result = tuple(values)
    if len(result) != width:
        raise ValueError(f"{field} must contain {width} values, found {len(result)}")
    return result


def map_robot_status_values(
    end_pos: Iterable[float],
    joint_pos: Iterable[float],
    joint_vel: Iterable[float],
    joint_cur: Iterable[float],
) -> ArmStateValues:
    eef = _fixed_vector(end_pos, 6, "end_pos")
    positions = _fixed_vector(joint_pos, 7, "joint_pos")
    velocities = _fixed_vector(joint_vel, 7, "joint_vel")
    currents = _fixed_vector(joint_cur, 7, "joint_cur")
    return ArmStateValues(
        eef_xyzrpy=eef,
        joint_positions=positions[:6],
        joint_velocities=velocities[:6],
        joint_currents=currents[:6],
        gripper_position=positions[6],
        gripper_velocity=velocities[6],
        gripper_current=currents[6],
    )
