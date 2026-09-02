from __future__ import annotations

from dataclasses import dataclass

from arx5_collection.collection.station.device_probe import realsense_inventory
from arx5_collection.collection.episode.adapters.pedal import (
    HidrawPedalIdentity,
    discover_hidraw_pedals,
)
from arx5_collection.collection.runtime.system import Usb2CanDevice, Usb2CanResolver


class StationInventoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class D405Device:
    serial_number: str
    name: str
    firmware: str | None
    usb_type: str


@dataclass(frozen=True, slots=True)
class StationInventory:
    usb2can: tuple[Usb2CanDevice, ...]
    cameras: tuple[D405Device, ...]
    pedals: tuple[HidrawPedalIdentity, ...]


class StationInventoryProvider:
    def __init__(self, usb2can_resolver: Usb2CanResolver | None = None) -> None:
        self.usb2can_resolver = usb2can_resolver or Usb2CanResolver()

    def collect(self) -> StationInventory:
        return StationInventory(
            usb2can=self.usb2can_resolver.discover(),
            cameras=discover_d405_devices(),
            pedals=discover_hidraw_pedals(),
        )


def discover_d405_devices() -> tuple[D405Device, ...]:
    inventory = realsense_inventory()
    if not inventory.get("available"):
        raise StationInventoryError(
            f"librealsense unavailable: {inventory.get('error', 'unknown error')}"
        )
    devices = []
    for value in inventory.get("devices", []):
        name = str(value.get("name") or "")
        if "D405" not in name.upper():
            continue
        serial_number = str(value.get("serial") or "").strip()
        usb_type = str(value.get("usb_type") or "").strip()
        if not serial_number:
            raise StationInventoryError("D405 without a stable serial number")
        devices.append(
            D405Device(
                serial_number=serial_number,
                name=name,
                firmware=value.get("firmware"),
                usb_type=usb_type,
            )
        )
    return tuple(devices)
