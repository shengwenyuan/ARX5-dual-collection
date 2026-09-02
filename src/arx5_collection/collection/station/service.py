from __future__ import annotations

import socket
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from arx5_collection.collection.runtime.config import (
    CameraConfig,
    StationConfig,
    set_process_ros_domain_id,
)

from .arm_identifier import ArmIdentifier
from .camera_identifier import CAMERA_BINDING_ORDER, CameraIdentifier
from .inventory import D405Device, StationInventory, StationInventoryProvider
from .pedal_identifier import PedalIdentifier
from .store import StationConfigStore
from arx5_collection.collection.environment import ENVIRONMENT


class StationInteraction(Protocol):
    def choose_station_id(self, default: str) -> str: ...
    def choose_ros_domain_id(self) -> int: ...
    def prompt_left_arm_movement(self) -> None: ...
    def choose_camera(
        self,
        role: str,
        candidates: Sequence[D405Device],
        used_serials: frozenset[str],
    ) -> str: ...
    def prompt_pedal(self, role: str) -> None: ...
    def report(self, message: str) -> None: ...


class StationInitializationError(RuntimeError):
    pass


class StationInitializationService:
    """Run a complete hardware-binding transaction and commit only its result."""

    def __init__(
        self,
        store: StationConfigStore,
        log_dir: Path,
        inventory_provider: StationInventoryProvider | None = None,
        arm_identifier_factory: Callable[[Path], ArmIdentifier] = ArmIdentifier,
        camera_identifier_factory: Callable[
            [Sequence[D405Device]], CameraIdentifier
        ] = CameraIdentifier,
        pedal_identifier_factory: Callable[..., PedalIdentifier] = PedalIdentifier,
    ) -> None:
        self.store = store
        self.log_dir = log_dir
        self.inventory_provider = inventory_provider or StationInventoryProvider()
        self.arm_identifier_factory = arm_identifier_factory
        self.camera_identifier_factory = camera_identifier_factory
        self.pedal_identifier_factory = pedal_identifier_factory

    def configure(self, interaction: StationInteraction) -> StationConfig:
        inventory = self.inventory_provider.collect()
        self._validate_inventory(inventory)
        station_id = interaction.choose_station_id(socket.gethostname())
        if not station_id.strip():
            raise StationInitializationError("station_id must not be empty")
        ros_domain_id = set_process_ros_domain_id(interaction.choose_ros_domain_id())

        interaction.report("ARX5: enabling gravity compensation for role binding")
        arms = self.arm_identifier_factory(self.log_dir).identify(
            inventory.usb2can,
            interaction.prompt_left_arm_movement,
        )
        interaction.report(
            f"ARX5: left={arms[0].usb_serial}, right={arms[1].usb_serial}"
        )

        identifier = self.camera_identifier_factory(inventory.cameras)
        cameras_by_role: dict[str, CameraConfig] = {}
        for role in CAMERA_BINDING_ORDER:
            selected = interaction.choose_camera(
                role,
                inventory.cameras,
                frozenset(camera.serial_number for camera in cameras_by_role.values()),
            ).strip()
            camera = identifier.bind(role, selected)
            cameras_by_role[role] = camera
            interaction.report(f"D405: {role}={camera.serial_number} PASS")

        interaction.report("Pedals: binding activate then abort")
        with self.pedal_identifier_factory(inventory.pedals) as pedal_identifier:
            triggers = pedal_identifier.identify(prompt=interaction.prompt_pedal)
        interaction.report(
            "Pedals: "
            f"activate={triggers.activate.serial_number}, "
            f"abort={triggers.abort.serial_number} PASS"
        )

        station = StationConfig(
            schema_version=3,
            station_id=station_id.strip(),
            ros_domain_id=ros_domain_id,
            sdk_type=ENVIRONMENT.station.sdk_type,
            arms=arms,
            cameras=tuple(
                cameras_by_role[role] for role in ENVIRONMENT.station.camera_roles
            ),
            triggers=triggers,
        )
        self.store.commit(station)
        interaction.report(f"Station configuration committed: {self.store.path}")
        return station

    @staticmethod
    def _validate_inventory(inventory: StationInventory) -> None:
        if len(inventory.usb2can) != len(ENVIRONMENT.station.arm_roles):
            raise StationInitializationError(
                "USB2CAN device count does not match environment arm roles: "
                f"{len(inventory.usb2can)}"
            )
        if len(inventory.cameras) != len(ENVIRONMENT.station.camera_roles):
            raise StationInitializationError(
                "D405 camera count does not match environment camera roles: "
                f"{len(inventory.cameras)}"
            )
        pedal_identities = {
            (pedal.vendor_id, pedal.product_id, pedal.serial_number)
            for pedal in inventory.pedals
        }
        if len(pedal_identities) != len(ENVIRONMENT.station.trigger_roles):
            raise StationInitializationError(
                "stable pedal count does not match environment trigger roles: "
                f"{len(pedal_identities)}"
            )
