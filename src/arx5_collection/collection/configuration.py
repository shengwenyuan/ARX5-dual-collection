from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from arx5_collection.collection.capture import CAPTURE_PROFILES
from arx5_collection.collection.capture import CaptureProfile
from arx5_collection.collection.episode.models import EpisodeRequest
from arx5_collection.collection.episode.models import StreamSpec


DIRECTORY_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class CollectionConfig:
    schema_version: int
    task_id: str
    task_description: str
    upload_directory: str
    capture_profile: CaptureProfile

    @classmethod
    def load(cls, path: Path) -> CollectionConfig:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
        if set(value) != {
            "schema_version",
            "task_id",
            "task_description",
            "upload_directory",
            "capture_profile",
        }:
            raise ValueError("collection config keys are invalid")
        if value["schema_version"] != 1:
            raise ValueError("collection schema_version must be 1")
        result = cls(
            schema_version=1,
            task_id=_text(value["task_id"], "task_id"),
            task_description=_text(value["task_description"], "task_description"),
            upload_directory=_text(value["upload_directory"], "upload_directory"),
            capture_profile=CaptureProfile(
                _text(value["capture_profile"], "capture_profile")
            ),
        )
        if DIRECTORY_NAME.fullmatch(result.upload_directory) is None:
            raise ValueError("collection upload_directory is invalid")
        return result

    def request(self, output_root: Path, station_config: Path) -> EpisodeRequest:
        streams = tuple(
            StreamSpec(
                id=item.id,
                topic=item.topic,
                required=item.required,
                expected_hz=item.expected_hz,
            )
            for item in CAPTURE_PROFILES[self.capture_profile].streams
        )
        return EpisodeRequest(
            task_id=self.task_id,
            task_description=self.task_description,
            output_root=output_root,
            station_config=station_config,
            streams=streams,
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"collection {label} must be a non-empty string")
    return value.strip()
