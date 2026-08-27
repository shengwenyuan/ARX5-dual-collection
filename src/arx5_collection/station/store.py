from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from arx5_collection.production.config import (
    StationConfig,
    load_station_config,
    validate_ros_domain_id,
)


DEFAULT_STATION_CONFIG = Path("/var/lib/arx5-collection/station.json")


def station_config_payload(station: StationConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": station.schema_version,
        "station_id": station.station_id,
        "sdk_type": station.sdk_type,
        "arms": [
            {
                "name": arm.role,
                "usb_serial": arm.usb_serial,
                "can_interface": arm.can_interface,
            }
            for arm in station.arms
        ],
        "cameras": {
            camera.role: {"serial_number": camera.serial_number}
            for camera in station.cameras
        },
    }
    if station.triggers is not None:
        payload["triggers"] = {
            pedal.role: {
                "vendor_id": pedal.vendor_id,
                "product_id": pedal.product_id,
                "serial_number": pedal.serial_number,
            }
            for pedal in (station.triggers.activate, station.triggers.abort)
        }
    if station.ros_domain_id is not None:
        payload["ros_domain_id"] = station.ros_domain_id
    if station.task_upload_routes is not None:
        payload["task_upload_routes"] = station.task_upload_routes
    return payload


class StationConfigStore:
    """Validate and atomically replace the host-local station configuration."""

    def __init__(self, path: Path = DEFAULT_STATION_CONFIG) -> None:
        self.path = path

    def commit(self, station: StationConfig) -> None:
        payload = station_config_payload(station)
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())

            # Parse the exact bytes that will become active before replacement.
            load_station_config(temporary_path)
            os.replace(temporary_path, self.path)
            directory_descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary_path.unlink(missing_ok=True)
            raise

    def set_ros_domain_id(self, ros_domain_id: int) -> StationConfig:
        if not self.path.is_file():
            raise ValueError(f"station configuration is missing: {self.path}")
        station = load_station_config(self.path)
        if station.triggers is None:
            raise ValueError(
                "station schema v1 cannot be upgraded in place; run "
                "'arx5-collect station configure'"
            )
        updated = replace(
            station,
            schema_version=max(station.schema_version, 3),
            ros_domain_id=validate_ros_domain_id(ros_domain_id),
        )
        self.commit(updated)
        return updated
