from __future__ import annotations

from dataclasses import dataclass
import re
import tomllib
from pathlib import Path
from typing import ClassVar, Mapping

from arx5_collection.config import config_path


class CaptureProfile:
    _instances: ClassVar[dict[str, CaptureProfile]] = {}
    RGBD: ClassVar[CaptureProfile]
    RGB_ONLY: ClassVar[CaptureProfile]

    def __new__(cls, value: object) -> CaptureProfile:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value) is None
        ):
            raise ValueError("capture profile name is invalid")
        current = cls._instances.get(value)
        if current is not None:
            return current
        current = super().__new__(cls)
        current.value = value
        cls._instances[value] = current
        return current

    def __repr__(self) -> str:
        return f"CaptureProfile({self.value!r})"


CaptureProfile.RGBD = CaptureProfile("rgbd")
CaptureProfile.RGB_ONLY = CaptureProfile("rgb_only")


@dataclass(frozen=True, slots=True)
class CaptureStream:
    id: str
    topic: str
    message_type: str
    required: bool
    expected_hz: float


@dataclass(frozen=True, slots=True)
class CaptureProfileSpec:
    name: CaptureProfile
    omitted_reason: str
    streams: tuple[CaptureStream, ...]


def load_capture_profiles(
    path: Path | None = None,
) -> tuple[CaptureProfile, dict[CaptureProfile, CaptureProfileSpec]]:
    source = path or config_path("specs/capture-profiles.toml")
    with source.open("rb") as stream:
        value = tomllib.load(stream)
    if set(value) != {"schema_version", "default_profile", "profiles"}:
        raise ValueError("capture profile spec keys are invalid")
    if value["schema_version"] != 1:
        raise ValueError("capture profile schema_version must be 1")
    profiles_value = value["profiles"]
    if not isinstance(profiles_value, dict):
        raise ValueError("capture profiles must be a table")
    profiles = {}
    for name, profile_value in profiles_value.items():
        profile = CaptureProfile(name)
        if not isinstance(profile_value, dict) or set(profile_value) != {
            "omitted_reason",
            "streams",
        }:
            raise ValueError(f"capture profile {profile.value} keys are invalid")
        rows = profile_value["streams"]
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"capture profile {profile.value} streams are required")
        streams = tuple(_load_stream(row, profile.value) for row in rows)
        ids = [item.id for item in streams]
        topics = [item.topic for item in streams]
        if len(ids) != len(set(ids)) or len(topics) != len(set(topics)):
            raise ValueError(f"capture profile {profile.value} streams must be unique")
        profiles[profile] = CaptureProfileSpec(
            profile,
            str(profile_value["omitted_reason"]),
            streams,
        )
    stream_definitions: dict[str, tuple[str, str]] = {}
    for spec in profiles.values():
        for item in spec.streams:
            definition = (item.topic, item.message_type)
            existing = stream_definitions.setdefault(item.id, definition)
            if existing != definition:
                raise ValueError(
                    f"capture stream {item.id} changes topic or type across profiles"
                )
    default = CaptureProfile(value["default_profile"])
    if default not in profiles:
        raise ValueError("default capture profile is not configured")
    return default, profiles


def _load_stream(value: object, profile: str) -> CaptureStream:
    keys = {"id", "topic", "message_type", "required", "expected_hz"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"capture profile {profile} stream keys are invalid")
    result = CaptureStream(
        id=str(value["id"]),
        topic=str(value["topic"]),
        message_type=str(value["message_type"]),
        required=value["required"] is True,
        expected_hz=float(value["expected_hz"]),
    )
    if not result.id or not result.topic.startswith("/") or not result.message_type:
        raise ValueError(f"capture profile {profile} stream values are invalid")
    if value["required"] is not True or result.expected_hz <= 0:
        raise ValueError(f"capture profile {profile} stream contract is invalid")
    return result


DEFAULT_CAPTURE_PROFILE, CAPTURE_PROFILES = load_capture_profiles()
RGBD_STREAMS = {
    stream.id: stream.topic for stream in CAPTURE_PROFILES[CaptureProfile.RGBD].streams
}
RGB_ONLY_STREAMS = {
    stream.id: stream.topic
    for stream in CAPTURE_PROFILES[CaptureProfile.RGB_ONLY].streams
}


def stream_contract(profile: CaptureProfile) -> dict[str, str]:
    return {stream.id: stream.topic for stream in CAPTURE_PROFILES[profile].streams}


def topic_type_contract(profile: CaptureProfile) -> dict[str, str]:
    return {
        stream.id: stream.message_type for stream in CAPTURE_PROFILES[profile].streams
    }


def expected_rate_contract(profile: CaptureProfile) -> dict[str, float]:
    return {
        stream.id: stream.expected_hz for stream in CAPTURE_PROFILES[profile].streams
    }


def configured_stream_contract() -> dict[str, tuple[str, str]]:
    result = {}
    for spec in CAPTURE_PROFILES.values():
        for stream in spec.streams:
            result[stream.id] = (stream.topic, stream.message_type)
    return result


def profile_for_streams(streams: Mapping[str, str]) -> CaptureProfile:
    actual = dict(streams)
    for profile in CAPTURE_PROFILES:
        if actual == stream_contract(profile):
            return profile
    raise ValueError("streams do not match a configured capture profile")


def profile_from_metadata(metadata: Mapping[str, object]) -> CaptureProfile:
    extensions = metadata.get("extensions")
    if extensions is None:
        return DEFAULT_CAPTURE_PROFILE
    if not isinstance(extensions, dict):
        raise ValueError("metadata extensions must be an object")
    capture = extensions.get("capture")
    if capture is None:
        return DEFAULT_CAPTURE_PROFILE
    if not isinstance(capture, dict):
        raise ValueError("metadata extensions.capture must be an object")
    try:
        profile = CaptureProfile(capture.get("profile"))
    except (TypeError, ValueError) as error:
        raise ValueError("metadata capture profile is unsupported") from error
    if profile not in CAPTURE_PROFILES:
        raise ValueError("metadata capture profile is unsupported")
    return profile


def metadata_extensions(profile: CaptureProfile) -> dict[str, object]:
    if profile is DEFAULT_CAPTURE_PROFILE:
        return {}
    default_streams = stream_contract(DEFAULT_CAPTURE_PROFILE)
    current_streams = stream_contract(profile)
    return {
        "capture": {
            "profile": profile.value,
            "omitted_streams": [
                {
                    "id": stream_id,
                    "topic": topic,
                    "status": "intentionally_omitted",
                    "reason": CAPTURE_PROFILES[profile].omitted_reason,
                }
                for stream_id, topic in default_streams.items()
                if stream_id not in current_streams
            ],
        }
    }
