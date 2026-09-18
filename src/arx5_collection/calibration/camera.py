"""D405 acquisition in a spawned process, independent of HighGUI and its GIL.

One shared latest-frame slot bounds memory and prevents a preview backlog. Images
and timestamps are copied under the same lock. The pipe carries startup/error
messages only. No ROS or robot controller is initialized in this process.
"""

import multiprocessing as mp
import os
import traceback
from pathlib import Path
from time import monotonic, sleep, time

import numpy as np

from .profiles import DEFAULT_PROFILE, validate_profile
from .timing import MAX_RECEIPT_AGE_S, timing_error


class RealSenseSession:
    def __init__(self, serial, profile):
        self.serial, self.profile = serial, validate_profile(profile)
        self.pipeline = None
        self.started = False

    def open(self):
        import pyrealsense2 as rs

        self.rs = rs
        context = rs.context()
        devices = [
            d
            for d in context.query_devices()
            if d.get_info(rs.camera_info.serial_number) == self.serial
        ]
        if len(devices) != 1 or "D405" not in devices[0].get_info(rs.camera_info.name):
            raise RuntimeError("configured D405 not uniquely found")
        device = devices[0]
        if not device.get_info(rs.camera_info.usb_type_descriptor).startswith("3"):
            raise RuntimeError("calibration camera requires USB3")
        supported, enabled = set(), False
        for sensor in device.query_sensors():
            for p in sensor.get_stream_profiles():
                if p.is_video_stream_profile():
                    v = p.as_video_stream_profile()
                    supported.add(
                        (p.stream_type(), p.format(), v.width(), v.height(), p.fps())
                    )
            if sensor.supports(rs.option.global_time_enabled):
                sensor.set_option(rs.option.global_time_enabled, 1)
                enabled |= sensor.get_option(rs.option.global_time_enabled) == 1
            if sensor.supports(rs.option.frames_queue_size):
                sensor.set_option(rs.option.frames_queue_size, 1)
        if not enabled:
            raise RuntimeError("camera does not support enabled Global Time")
        w, h, fps = (self.profile[k] for k in ("width", "height", "fps"))
        for stream, fmt in (
            (rs.stream.color, rs.format.rgb8),
            (rs.stream.depth, rs.format.z16),
        ):
            if (stream, fmt, w, h, fps) not in supported:
                raise RuntimeError(
                    f"{self.serial}: unsupported native {stream} {w}x{h}@{fps}; no resolution fallback"
                )
        self.pipeline = rs.pipeline(context)
        config = rs.config()
        config.enable_device(self.serial)
        config.enable_stream(rs.stream.color, w, h, rs.format.rgb8, fps)
        config.enable_stream(rs.stream.depth, w, h, rs.format.z16, fps)
        actual = self.pipeline.start(config)
        self.started = True
        color = actual.get_stream(rs.stream.color).as_video_stream_profile()
        depth = actual.get_stream(rs.stream.depth).as_video_stream_profile()
        for p, fmt in ((color, rs.format.rgb8), (depth, rs.format.z16)):
            if (p.width(), p.height(), p.fps(), p.format()) != (w, h, fps, fmt):
                raise RuntimeError("actual camera profile differs from requested route")

        def intrinsics(p):
            k = p.get_intrinsics()
            return {
                "width": k.width,
                "height": k.height,
                "fx": k.fx,
                "fy": k.fy,
                "ppx": k.ppx,
                "ppy": k.ppy,
                "model": str(k.model),
                "coeffs": list(k.coeffs),
            }

        extrinsic = depth.get_extrinsics_to(color)
        return {
            "serial": self.serial,
            "profile": dict(self.profile),
            "factory_color": intrinsics(color),
            "factory_depth": intrinsics(depth),
            "depth_scale": actual.get_device().first_depth_sensor().get_depth_scale(),
            "depth_to_color": {
                "rotation_column_major": list(extrinsic.rotation),
                "translation_m": list(extrinsic.translation),
            },
            "alignment": "native RGB; depth is not used in RGB calibration",
            "acquisition": "spawned process, single shared latest-frame slot",
        }

    def read(self):
        arrived, frames = self.pipeline.try_wait_for_frames(100)
        if not arrived:
            return None
        color = frames.get_color_frame()
        if not color:
            return None
        received, wall = monotonic(), time()
        image = np.asanyarray(color.get_data())[:, :, ::-1].copy()
        return image, (
            color.get_frame_number(),
            color.get_timestamp() / 1000,
            wall,
            received,
            float(
                color.get_frame_timestamp_domain()
                == self.rs.timestamp_domain.global_time
            ),
        )

    def close(self):
        if self.started:
            self.pipeline.stop()
            self.started = False
        self.pipeline = None


def frame_timing(values):
    number, source, wall, received, global_time = values
    finite = source if np.isfinite(source) else None
    frame = {
        "number": int(number),
        "source_time_s": finite,
        "received_wall_s": wall,
        "received_monotonic_s": received,
        "source_monotonic_s": received - (wall - source)
        if finite is not None
        else None,
        "clock": "global_time" if global_time else "unmapped_device_time",
    }
    frame["clock_error"] = timing_error(frame)
    return frame


def _acquire(
    serial, profile, log_path, connection, stop, lock, pixels, timestamps, factory
):
    session = None
    parent_pid = os.getppid()

    def log(message):
        if log_path:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as stream:
                stream.write(f"{time():.6f} {message}\n")

    try:
        session = factory(serial, profile)
        connection.send(("metadata", session.open()))
        image_slot = np.frombuffer(pixels, dtype=np.uint8).reshape(
            profile["height"], profile["width"], 3
        )
        started = last_frame = monotonic()
        previous_status = None
        while not stop.is_set() and os.getppid() == parent_pid:
            data = session.read()
            if data is None:
                if monotonic() - last_frame > 3:
                    raise TimeoutError("camera stream produced no frames for 3 s")
                continue
            image, values = data
            last_frame = monotonic()
            if image.shape != image_slot.shape or image.dtype != np.uint8:
                raise RuntimeError("camera frame dimensions/format differ from route")
            if last_frame - started < 2:
                continue  # exposure/global-clock warmup; GUI is already initialized
            info = frame_timing(values)
            if info["clock_error"] != previous_status:
                log(
                    f"frame={info['number']} age_ms={(values[2] - values[1]) * 1000:.3f} clock={info['clock_error'] or 'ready'}"
                )
                previous_status = info["clock_error"]
            if lock.acquire(timeout=0.1):
                try:
                    np.copyto(image_slot, image)
                    timestamps[:] = values
                finally:
                    lock.release()
    except BaseException as error:  # noqa: BLE001 - report every child-process failure to its owner
        detail = traceback.format_exc()
        try:
            log(detail)
        finally:
            connection.send(("error", f"{type(error).__name__}: {error}"))
    finally:
        try:
            if session is not None:
                session.close()
        except BaseException as error:  # noqa: BLE001 - preserve shutdown failures across the process boundary
            connection.send(("error", f"camera shutdown failed: {error}"))
        connection.close()


class Camera:
    def __init__(
        self, serial, log_path=None, profile=None, *, session_factory=RealSenseSession
    ):
        self.serial, self.log_path = serial, log_path
        self.profile = validate_profile(DEFAULT_PROFILE if profile is None else profile)
        self.factory = session_factory
        self.metadata = {}
        self.process = self.connection = self.stop = self.lock = None
        self.pixels = self.timestamps = None
        self.error = None

    def _messages(self):
        while self.connection.poll():
            try:
                kind, value = self.connection.recv()
            except EOFError:
                break
            if kind == "error":
                self.error = value
            else:
                self.metadata = value
        if self.error:
            raise RuntimeError(f"camera worker failed: {self.error}")
        if not self.process.is_alive():
            raise RuntimeError(f"camera worker exited (code {self.process.exitcode})")

    def open(self):
        if self.process is not None:
            raise RuntimeError("camera already open")
        context = mp.get_context("spawn")
        self.stop, self.lock = context.Event(), context.Lock()
        self.pixels = context.RawArray(
            "B", self.profile["width"] * self.profile["height"] * 3
        )
        self.timestamps = context.RawArray("d", [-1, 0, 0, 0, 0])
        self.connection, child = context.Pipe(duplex=False)
        self.error, self.metadata = None, {}
        self.process = context.Process(
            target=_acquire,
            args=(
                self.serial,
                self.profile,
                self.log_path,
                child,
                self.stop,
                self.lock,
                self.pixels,
                self.timestamps,
                self.factory,
            ),
            name="calibration-camera",
            daemon=True,
        )
        try:
            self.process.start()
            child.close()
            self.latest(timeout=20)
            if self.metadata.get("profile") != self.profile:
                raise RuntimeError("camera metadata profile does not match route")
        except BaseException:
            child.close()
            self.close()
            raise

    def latest(self, timeout=1.0):
        if self.process is None:
            raise RuntimeError("camera is not open")
        deadline = monotonic() + timeout
        while True:
            self._messages()
            if self.lock.acquire(timeout=0.02):
                try:
                    values = list(self.timestamps)
                    if (
                        values[0] >= 0
                        and 0 <= monotonic() - values[3] <= MAX_RECEIPT_AGE_S
                    ):
                        frame = frame_timing(values)
                        frame["image"] = (
                            np.frombuffer(self.pixels, dtype=np.uint8)
                            .reshape(self.profile["height"], self.profile["width"], 3)
                            .copy()
                        )
                        return frame
                finally:
                    self.lock.release()
            if monotonic() >= deadline:
                raise TimeoutError(
                    f"camera frame is stale (no fresh frame within {timeout:g} s)"
                )
            sleep(0.005)

    def close(self):
        if self.process is None:
            return
        self.stop.set()
        if self.process.pid is not None:
            self.process.join(3)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(2)
            if self.process.is_alive():
                raise RuntimeError("camera acquisition process failed to stop")
        self.connection.close()
        self.process.close()
        self.process = self.connection = None
        self.pixels = self.timestamps = None
