from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arx5_collection.ros2_adapters.reset import ArmResetSpec


@dataclass(frozen=True, slots=True)
class ArmRuntimeProfile:
    name: str
    controller_launch: str
    left_controller_name: str
    right_controller_name: str
    left_input_topic: str
    right_input_topic: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("arm state profile name must not be empty")
        if not self.controller_launch.startswith("/"):
            raise ValueError("ARX5 controller launch path must be absolute")
        if not self.left_controller_name or not self.right_controller_name:
            raise ValueError("ARX5 controller names must not be empty")
        if not self.left_input_topic.startswith("/"):
            raise ValueError("left arm input topic must be absolute")
        if not self.right_input_topic.startswith("/"):
            raise ValueError("right arm input topic must be absolute")


TEACHING_ARM_PROFILE = ArmRuntimeProfile(
    name="teaching",
    controller_launch=(
        "/opt/arx_ws/install/share/arx_x5_controller/launch/"
        "x5_v2/v2_collect.launch.py"
    ),
    left_controller_name="arm_master_l",
    right_controller_name="arm_master_r",
    left_input_topic="/arm_master_l_status",
    right_input_topic="/arm_master_r_status",
)
DAGGER_ARM_PROFILE = ArmRuntimeProfile(
    name="dagger",
    controller_launch=(
        "/opt/arx_ws/install/share/arx_x5_controller/launch/"
        "x5_v2/v2_joint_control.launch.py"
    ),
    left_controller_name="arm_slave_l",
    right_controller_name="arm_slave_r",
    left_input_topic="/arm_slave_l_status",
    right_input_topic="/arm_slave_r_status",
)

ARM_PROFILES = MappingProxyType(
    {
        TEACHING_ARM_PROFILE.name: TEACHING_ARM_PROFILE,
        DAGGER_ARM_PROFILE.name: DAGGER_ARM_PROFILE,
    }
)


def resolve_arm_profile(name: str) -> ArmRuntimeProfile:
    try:
        return ARM_PROFILES[name]
    except KeyError as error:
        supported = ", ".join(sorted(ARM_PROFILES))
        raise ValueError(
            f"unknown arm profile {name!r}; supported profiles: {supported}"
        ) from error


def reset_specs_for(profile: ArmRuntimeProfile) -> tuple[ArmResetSpec, ...]:
    from arx5_collection.ros2_adapters.reset import ArmResetSpec

    return tuple(
        ArmResetSpec(
            name=side,
            status_topic=input_topic,
            go_home_service=f"/{controller_name}/go_home",
            gravity_service=f"/{controller_name}/gravity_compensation",
        )
        for side, controller_name, input_topic in (
            (
                "left",
                profile.left_controller_name,
                profile.left_input_topic,
            ),
            (
                "right",
                profile.right_controller_name,
                profile.right_input_topic,
            ),
        )
    )
