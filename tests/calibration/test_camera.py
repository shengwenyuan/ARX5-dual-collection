"""Process isolation, native profiles and immutable timing contracts."""

import ctypes
from time import monotonic, sleep, time

import numpy as np
import pytest

from arx5_collection.calibration.camera import Camera, frame_timing
from arx5_collection.calibration.profiles import (
    DEFAULT_PROFILE,
    validate_profile,
)
from arx5_collection.calibration.timing import capture_ready, timing_error


class SyntheticSession:
    def __init__(self, serial, profile):
        self.profile = profile
        self.number = 0
        self.started = monotonic()

    def open(self):
        return {"profile": self.profile}

    def read(self):
        sleep(0.02)
        self.number += 1
        image = np.full(
            (self.profile["height"], self.profile["width"], 3),
            self.number % 251,
            np.uint8,
        )
        return image, (self.number, time() - 0.01, time(), monotonic(), 1)

    def close(self):
        pass


class FailedSession(SyntheticSession):
    def read(self):
        if monotonic() - self.started > 2.5:
            raise RuntimeError("USB transfer disconnected")
        return super().read()


class WrongSizeSession(SyntheticSession):
    def read(self):
        image, stamp = super().read()
        return image[:, :-1], stamp


@pytest.mark.parametrize(
    "global_time,source", [(1, 1000.03), (0, 5), (1, float("nan"))]
)
def test_invalid_clock_can_preview_but_not_capture(global_time, source):
    frame = frame_timing([10, source, 1000, 3, global_time])
    assert timing_error(frame)
    assert not capture_ready(frame, 3)
    valid = frame_timing([11, 999.99, 1000, 3, 1])
    assert not timing_error(valid)
    assert capture_ready(valid, 3)
    assert not capture_ready(valid, 3.3)
    valid["source_monotonic_s"] += 0.1
    assert timing_error(valid) == "inconsistent camera clock mapping"


def test_process_keeps_acquiring_while_parent_gil_is_blocked():
    profile = DEFAULT_PROFILE
    camera = Camera("synthetic", profile=profile, session_factory=SyntheticSession)
    camera.open()
    child = camera.process
    try:
        before = camera.latest()
        # Unlike time.sleep, PyDLL keeps the calling interpreter's GIL held.
        ctypes.PyDLL(None).usleep(1_200_000)
        after = camera.latest()
        assert after["number"] > before["number"] + 20
        assert after["image"].shape == (profile["height"], profile["width"], 3)
        assert np.all(after["image"] == after["number"] % 251)
        assert capture_ready(after, monotonic())
        assert camera.metadata["profile"] == profile
    finally:
        camera.close()
        camera.close()
    assert child._closed and camera.process is None


def test_failure_cause_logged_and_no_process_leaked(tmp_path):
    log = tmp_path / "camera.log"
    camera = Camera("synthetic", log, session_factory=FailedSession)
    camera.open()
    try:
        sleep(0.7)
        with pytest.raises(RuntimeError, match="USB transfer disconnected"):
            camera.latest()
    finally:
        camera.close()
    assert "USB transfer disconnected" in log.read_text()
    assert camera.process is None


def test_bad_native_image_fails_before_hardware_start():
    camera = Camera("synthetic", session_factory=WrongSizeSession)
    with pytest.raises(RuntimeError, match="dimensions/format differ"):
        camera.open()
    assert camera.process is None


def test_profile_is_explicit_and_not_mutable_alias():
    p = validate_profile(DEFAULT_PROFILE)
    p["width"] = 12
    assert DEFAULT_PROFILE["width"] == 848
    with pytest.raises(ValueError, match="native RGB8"):
        validate_profile({**DEFAULT_PROFILE, "width": 1920})


def test_calibration_matches_collection_native_profile_and_rejects_720p():
    from pathlib import Path
    import re
    source = (Path(__file__).resolve().parents[2] / 'ros2_ws/src/arx5_d405_source_cpp/src/multi_d405_source.cpp').read_text()
    for key, constant in (("width", "kRequiredWidth"), ("height", "kRequiredHeight"), ("fps", "kRequiredFps")):
        assert DEFAULT_PROFILE[key] == int(re.search(rf"{constant} = (\d+)", source).group(1))
    assert DEFAULT_PROFILE["format"] == "rgb8"
    with pytest.raises(ValueError, match="re-teach old 720p"):
        Camera("unused", profile={**DEFAULT_PROFILE, "width": 1280, "height": 720})
