from __future__ import annotations

from enum import Enum
from typing import Mapping


class CaptureProfile(str, Enum):
    RGBD = "rgbd"
    RGB_ONLY = "rgb_only"


RGBD_STREAMS = {
    "left_arm_state": "/embodiments/left_arm/state",
    "right_arm_state": "/embodiments/right_arm/state",
    "camera_left_color": "/sensors/camera_left/color/image_raw",
    "camera_left_aligned_depth": "/sensors/camera_left/aligned_depth/image_raw",
    "camera_right_color": "/sensors/camera_right/color/image_raw",
    "camera_right_aligned_depth": "/sensors/camera_right/aligned_depth/image_raw",
    "camera_overview_color": "/sensors/camera_overview/color/image_raw",
    "camera_overview_aligned_depth": "/sensors/camera_overview/aligned_depth/image_raw",
}
RGB_ONLY_STREAMS = {
    stream_id: topic
    for stream_id, topic in RGBD_STREAMS.items()
    if not stream_id.endswith("_aligned_depth")
}


def stream_contract(profile: CaptureProfile) -> dict[str, str]:
    return RGBD_STREAMS if profile is CaptureProfile.RGBD else RGB_ONLY_STREAMS


def profile_for_streams(streams: Mapping[str, str]) -> CaptureProfile:
    actual = dict(streams)
    for profile in CaptureProfile:
        if actual == stream_contract(profile):
            return profile
    raise ValueError(
        "production task must contain the fixed RGB-D or RGB-only stream contract"
    )


def profile_from_metadata(metadata: Mapping[str, object]) -> CaptureProfile:
    extensions = metadata.get("extensions")
    if extensions is None:
        return CaptureProfile.RGBD
    if not isinstance(extensions, dict):
        raise ValueError("metadata extensions must be an object")
    capture = extensions.get("capture")
    if capture is None:
        return CaptureProfile.RGBD
    if not isinstance(capture, dict):
        raise ValueError("metadata extensions.capture must be an object")
    try:
        return CaptureProfile(capture.get("profile"))
    except (TypeError, ValueError) as error:
        raise ValueError("metadata capture profile is unsupported") from error


def metadata_extensions(profile: CaptureProfile) -> dict[str, object]:
    if profile is CaptureProfile.RGBD:
        return {}
    return {
        "capture": {
            "profile": profile.value,
            "omitted_streams": [
                {
                    "id": stream_id,
                    "topic": topic,
                    "status": "intentionally_omitted",
                    "reason": "local_storage_pressure",
                }
                for stream_id, topic in RGBD_STREAMS.items()
                if stream_id not in RGB_ONLY_STREAMS
            ],
        }
    }
