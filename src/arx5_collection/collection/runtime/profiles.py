from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
import tomllib

from arx5_collection.config import config_path

if TYPE_CHECKING:
    from arx5_collection.adapters.ros2.reset import ArmResetSpec


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


def load_arm_profiles(
    path: Path | None = None,
) -> MappingProxyType[str, ArmRuntimeProfile]:
    source = path or config_path("specs/arm-profiles.toml")
    with source.open("rb") as stream:
        value = tomllib.load(stream)
    if set(value) != {"schema_version", "profiles"} or value["schema_version"] != 1:
        raise ValueError("arm profile spec must use schema_version 1 and exact keys")
    profiles_value = value["profiles"]
    if not isinstance(profiles_value, dict) or not profiles_value:
        raise ValueError("arm profiles must be a non-empty table")
    profiles = {}
    keys = {
        "controller_launch",
        "left_controller_name",
        "right_controller_name",
        "left_input_topic",
        "right_input_topic",
    }
    for name, item in profiles_value.items():
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError(f"arm profile {name} keys are invalid")
        profile = ArmRuntimeProfile(
            name=str(name), **{key: str(item[key]) for key in keys}
        )
        profiles[profile.name] = profile
    return MappingProxyType(profiles)


ARM_PROFILES = load_arm_profiles()
TEACHING_ARM_PROFILE = ARM_PROFILES["teaching"]
DAGGER_ARM_PROFILE = ARM_PROFILES["dagger"]


def resolve_arm_profile(name: str) -> ArmRuntimeProfile:
    try:
        return ARM_PROFILES[name]
    except KeyError as error:
        supported = ", ".join(sorted(ARM_PROFILES))
        raise ValueError(
            f"unknown arm profile {name!r}; supported profiles: {supported}"
        ) from error


def reset_specs_for(profile: ArmRuntimeProfile) -> tuple[ArmResetSpec, ...]:
    from arx5_collection.adapters.ros2.reset import ArmResetSpec

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
