from types import SimpleNamespace

import numpy as np
import pytest

from arx5_collection.calibration import hardware


@pytest.mark.parametrize(
    "domain,stamp", [("global", 1000030), ("device", 5000), ("global", float("nan"))]
)
def test_clock_settling_preserves_preview_but_marks_frame_invalid(
    monkeypatch, domain, stamp
):
    camera = hardware.Camera("fake")
    camera.rs = SimpleNamespace(timestamp_domain=SimpleNamespace(global_time="global"))
    monkeypatch.setattr(hardware, "time", lambda: 1000.0)
    ticks = iter([0, 3, 3, 3, 3, 3])
    monkeypatch.setattr(hardware, "monotonic", lambda: next(ticks))
    observed = []

    def frame(number, domain, stamp):
        return SimpleNamespace(
            get_frame_number=lambda: number,
            get_frame_timestamp_domain=lambda: domain,
            get_timestamp=lambda: stamp,
            get_data=lambda: np.full((2, 3, 3), 125, np.uint8),
        )

    frames = iter([frame(1, domain, stamp), frame(2, "global", 999990)])

    def wait(_):
        if camera.frame is not None:
            observed.append(camera.frame)
            camera.stop_event.set()
        result = next(frames)
        return SimpleNamespace(get_color_frame=lambda: result)

    camera.pipeline = SimpleNamespace(wait_for_frames=wait)
    camera._run()
    assert camera.error is None
    assert observed[0]["clock_error"]
    assert observed[0]["image"].mean() == 125
    assert camera.frame["clock_error"] == ""
    assert camera.frame["source_monotonic_s"] == pytest.approx(2.99)


def test_stream_failure_retains_cause_and_writes_log(tmp_path):
    camera = hardware.Camera("fake", tmp_path / "camera.log")

    def failed(_):
        raise RuntimeError("USB transfer disconnected")

    camera.pipeline = SimpleNamespace(wait_for_frames=failed)
    camera._run()
    with pytest.raises(
        RuntimeError, match="camera worker failed: USB transfer disconnected"
    ):
        camera.latest()
    assert "USB transfer disconnected" in (tmp_path / "camera.log").read_text()
