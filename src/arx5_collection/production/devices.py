from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from arx5_collection.device_probe import collect

from .checks import CheckPhase, CheckResult
from .config import StationConfig


InventoryProbe = Callable[[], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    id: str
    kind: str
    configured_serial: str
    detected_serial: str | None
    link: str | None
    matched: bool
    detail: str


class DeviceIdentityVerifier:
    def __init__(
        self,
        station: StationConfig,
        inventory_probe: InventoryProbe = collect,
    ) -> None:
        self.station = station
        self.inventory_probe = inventory_probe

    def inspect(self) -> tuple[DeviceIdentity, ...]:
        inventory = self.inventory_probe()
        arx_devices = inventory.get("arx_usb", [])
        realsense = inventory.get("realsense", {})
        camera_devices = realsense.get("devices", [])

        identities: list[DeviceIdentity] = []
        for arm in self.station.arms:
            matches = [
                device for device in arx_devices if device.get("serial") == arm.usb_serial
            ]
            detected = matches[0] if len(matches) == 1 else None
            identities.append(
                DeviceIdentity(
                    id=f"{arm.role}_arm",
                    kind="arm",
                    configured_serial=arm.usb_serial,
                    detected_serial=None if detected is None else detected.get("serial"),
                    link=None if detected is None else detected.get("speed_mbps"),
                    matched=len(matches) == 1,
                    detail=(
                        f"matched {matches[0].get('node')}"
                        if len(matches) == 1
                        else f"expected one USB2CAN, found {len(matches)}"
                    ),
                )
            )

        sdk_available = realsense.get("available") is True
        for camera in self.station.cameras:
            matches = [
                device
                for device in camera_devices
                if device.get("serial") == camera.serial_number
            ]
            detected = matches[0] if len(matches) == 1 else None
            usb_type = None if detected is None else detected.get("usb_type")
            usb3 = isinstance(usb_type, str) and usb_type.startswith("3")
            identities.append(
                DeviceIdentity(
                    id=f"camera_{camera.role}",
                    kind="camera",
                    configured_serial=camera.serial_number,
                    detected_serial=None if detected is None else detected.get("serial"),
                    link=usb_type,
                    matched=sdk_available and len(matches) == 1 and usb3,
                    detail=(
                        f"matched USB {usb_type}"
                        if sdk_available and len(matches) == 1 and usb3
                        else _camera_failure(realsense, len(matches), usb_type)
                    ),
                )
            )
        return tuple(identities)

    def checks(self) -> tuple[CheckResult, ...]:
        return tuple(
            CheckResult(
                name=f"device_{identity.id}",
                phase=CheckPhase.SESSION,
                passed=identity.matched,
                detail=identity.detail,
            )
            for identity in self.inspect()
        )


def _camera_failure(
    realsense: dict[str, Any], match_count: int, usb_type: object
) -> str:
    if realsense.get("available") is not True:
        return f"RealSense SDK unavailable: {realsense.get('error', 'unknown error')}"
    if match_count != 1:
        return f"expected one RealSense, found {match_count}"
    return f"RealSense is not on USB3: usb_type={usb_type}"

