#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any

from arx5_collection.metrics import timing_summary


def stream_config(rs: Any, serial: str, width: int, height: int, fps: int) -> Any:
    config = rs.config()
    config.enable_device(serial)
    config.enable_stream(rs.stream.color, width, height, rs.format.yuyv, fps)
    config.enable_stream(rs.stream.depth, width, height, rs.format.z16, fps)
    return config


def run(duration_s: float, width: int, height: int, fps: int) -> dict[str, Any]:
    import pyrealsense2 as rs

    context = rs.context()
    serials = [device.get_info(rs.camera_info.serial_number) for device in context.query_devices()]
    result: dict[str, Any] = {
        "settings": {"width": width, "height": height, "fps": fps},
        "duration_s": duration_s,
        "serials": serials,
        "cameras": [],
        "status": "FAILED",
    }
    if len(serials) != 3:
        result["error"] = f"expected 3 RealSense devices, found {len(serials)}"
        return result

    pipelines: dict[str, Any] = {}
    try:
        for serial in serials:
            pipeline = rs.pipeline(context)
            pipeline.start(stream_config(rs, serial, width, height, fps))
            pipelines[serial] = pipeline

        reports: dict[str, dict[str, Any]] = {}
        threads = [
            threading.Thread(
                target=collect_camera,
                args=(rs, serial, pipeline, duration_s, reports),
                daemon=True,
            )
            for serial, pipeline in pipelines.items()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(duration_s + 15.0)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("camera worker did not stop")
        result["cameras"] = [reports[serial] for serial in serials]
        errors = [report.get("error") for report in result["cameras"] if report.get("error")]
        if errors:
            raise RuntimeError("; ".join(errors))
        result["status"] = "PASSED"
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        for pipeline in pipelines.values():
            try:
                pipeline.stop()
            except RuntimeError:
                pass
    return result


def collect_camera(
    rs: Any,
    serial: str,
    pipeline: Any,
    duration_s: float,
    reports: dict[str, dict[str, Any]],
) -> None:
    align = rs.align(rs.stream.color)
    timestamps_ns: list[int] = []
    frame_numbers: list[int] = []
    device_timestamps_ms: list[float] = []
    dimensions = None
    timestamp_domain = None
    deadline = time.monotonic() + duration_s
    try:
        while time.monotonic() < deadline:
            frames = align.process(pipeline.wait_for_frames(5000))
            color = frames.get_color_frame()
            depth = frames.get_depth_frame()
            if not color or not depth:
                raise RuntimeError(f"{serial} missing aligned color or depth")
            timestamps_ns.append(time.monotonic_ns())
            frame_numbers.append(color.get_frame_number())
            device_timestamps_ms.append(color.get_timestamp())
            timestamp_domain = str(color.get_frame_timestamp_domain())
            dimensions = {
                "color": [color.get_width(), color.get_height()],
                "aligned_depth": [depth.get_width(), depth.get_height()],
            }
        reports[serial] = {
            "serial": serial,
            **timing_summary(timestamps_ns),
            "first_frame_number": frame_numbers[0] if frame_numbers else None,
            "last_frame_number": frame_numbers[-1] if frame_numbers else None,
            "first_device_timestamp_ms": device_timestamps_ms[0] if device_timestamps_ms else None,
            "last_device_timestamp_ms": device_timestamps_ms[-1] if device_timestamps_ms else None,
            "timestamp_domain": timestamp_domain,
            "dimensions": dimensions,
        }
    except BaseException as error:
        reports[serial] = {"serial": serial, "error": f"{type(error).__name__}: {error}"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.duration, args.width, args.height, args.fps)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    raise SystemExit(0 if result["status"] == "PASSED" else 1)


if __name__ == "__main__":
    main()

