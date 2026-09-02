from __future__ import annotations

from arx5_collection.collection.capture import CaptureProfile
from arx5_collection.collection.capture import RGBD_STREAMS
from arx5_collection.collection.capture import RGB_ONLY_STREAMS
from arx5_collection.collection.capture import metadata_extensions
from arx5_collection.collection.capture import load_capture_profiles
from arx5_collection.collection.capture import profile_for_streams
from arx5_collection.collection.capture import profile_from_metadata


def test_default_capture_profiles_are_exact_contracts() -> None:
    assert profile_for_streams(RGBD_STREAMS) is CaptureProfile.RGBD
    assert profile_for_streams(RGB_ONLY_STREAMS) is CaptureProfile.RGB_ONLY


def test_rgb_only_metadata_truthfully_marks_three_omitted_depth_streams() -> None:
    extensions = metadata_extensions(CaptureProfile.RGB_ONLY)
    capture = extensions["capture"]

    assert capture["profile"] == "rgb_only"
    assert len(capture["omitted_streams"]) == 3
    assert {item["status"] for item in capture["omitted_streams"]} == {
        "intentionally_omitted"
    }
    assert profile_from_metadata({"extensions": extensions}) is CaptureProfile.RGB_ONLY


def test_historical_metadata_defaults_to_rgbd() -> None:
    assert profile_from_metadata({}) is CaptureProfile.RGBD


def test_capture_profile_names_are_loaded_from_config(tmp_path) -> None:
    path = tmp_path / "capture.toml"
    path.write_text(
        """
schema_version = 1
default_profile = "custom"

[profiles.custom]
omitted_reason = ""

[[profiles.custom.streams]]
id = "custom_stream"
topic = "/custom/stream"
message_type = "example/msg/Custom"
required = true
expected_hz = 12.5
"""
    )

    default, profiles = load_capture_profiles(path)

    assert default.value == "custom"
    assert profiles[default].streams[0].expected_hz == 12.5
