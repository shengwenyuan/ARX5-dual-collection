from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from arx5_collection.collection.environment import ENVIRONMENT


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def usb_device(path: Path) -> dict[str, Any] | None:
    vendor = read_text(path / "idVendor")
    product_id = read_text(path / "idProduct")
    if vendor is None or product_id is None:
        return None
    return {
        "node": path.name,
        "vendor_id": vendor,
        "product_id": product_id,
        "manufacturer": read_text(path / "manufacturer"),
        "product": read_text(path / "product"),
        "serial": read_text(path / "serial"),
        "speed_mbps": read_text(path / "speed"),
        "bus": read_text(path / "busnum"),
        "device": read_text(path / "devnum"),
    }


def usb_inventory(
    root: Path = ENVIRONMENT.paths.usb_sysfs_root,
) -> list[dict[str, Any]]:
    return [
        device
        for path in sorted(root.glob("*"))
        if (device := usb_device(path)) is not None
    ]


def can_inventory() -> list[dict[str, Any]]:
    command = ["ip", "-json", "-details", "-statistics", "link", "show", "type", "can"]
    try:
        output = subprocess.run(
            command, check=True, capture_output=True, text=True
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    return [
        entry
        for entry in json.loads(output)
        if entry.get("ifname") and entry.get("linkinfo", {}).get("info_kind") == "can"
    ]


def realsense_inventory() -> dict[str, Any]:
    try:
        import pyrealsense2 as rs
    except ImportError as error:
        return {"available": False, "error": str(error), "devices": []}

    devices = []
    for device in rs.context().query_devices():
        info: dict[str, Any] = {}
        for key, name in (
            (rs.camera_info.serial_number, "serial"),
            (rs.camera_info.name, "name"),
            (rs.camera_info.firmware_version, "firmware"),
            (rs.camera_info.usb_type_descriptor, "usb_type"),
            (rs.camera_info.product_id, "product_id"),
        ):
            try:
                info[name] = device.get_info(key)
            except RuntimeError:
                info[name] = None
        info["sensors"] = [
            sensor_capabilities(rs, sensor) for sensor in device.query_sensors()
        ]
        devices.append(info)
    return {
        "available": True,
        "sdk_version": getattr(rs, "__version__", "unknown"),
        "devices": devices,
    }


def sensor_capabilities(rs: Any, sensor: Any) -> dict[str, Any]:
    options = {}
    for name in (
        "global_time_enabled",
        "inter_cam_sync_mode",
        "output_trigger_enabled",
        "frames_queue_size",
        "host_performance",
    ):
        option = getattr(rs.option, name, None)
        supported = option is not None and sensor.supports(option)
        value = None
        if supported:
            try:
                value = sensor.get_option(option)
            except RuntimeError:
                value = None
        options[name] = {"available": supported, "value": value}

    profiles = []
    for profile in sensor.get_stream_profiles():
        try:
            video = profile.as_video_stream_profile()
        except RuntimeError:
            continue
        if (video.width(), video.height(), profile.fps()) == (
            ENVIRONMENT.camera.width,
            ENVIRONMENT.camera.height,
            ENVIRONMENT.camera.fps,
        ):
            profiles.append(
                {
                    "stream": str(profile.stream_type()),
                    "format": str(profile.format()),
                    "index": profile.stream_index(),
                }
            )
    return {
        "name": sensor.get_info(rs.camera_info.name),
        "options": options,
        "configured_profiles": profiles,
    }


def collect() -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    usb = usb_inventory()
    pedals: list[dict[str, str]] = []
    pedal_error = None
    try:
        from arx5_collection.collection.episode.adapters.pedal import (
            discover_hidraw_pedals,
        )

        pedals = [
            {
                "path": str(pedal.path),
                "vendor_id": pedal.vendor_id,
                "product_id": pedal.product_id,
                "serial_number": pedal.serial_number,
            }
            for pedal in discover_hidraw_pedals()
        ]
    except RuntimeError as error:
        pedal_error = str(error)
    return {
        "host": {
            "platform": platform.platform(),
            "disk_total_bytes": disk.total,
            "disk_free_bytes": disk.free,
        },
        "can": can_inventory(),
        "usb": usb,
        "arx_usb": [device for device in usb if device["manufacturer"] == "ARX"],
        "realsense": realsense_inventory(),
        "pedals": pedals,
        "pedal_error": pedal_error,
    }
