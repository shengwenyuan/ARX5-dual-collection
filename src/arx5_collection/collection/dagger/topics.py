from __future__ import annotations

import tomllib

from arx5_collection.config import config_path


with config_path("specs/dagger-interface.toml").open("rb") as _stream:
    _value = tomllib.load(_stream)
if (
    set(_value)
    != {
        "schema_version",
        "authority_topic",
        "authority_message_type",
    }
    or _value["schema_version"] != 1
):
    raise ValueError("DAgger interface spec must use schema_version 1 and exact keys")
AUTHORITY_TOPIC = str(_value["authority_topic"])
AUTHORITY_TYPE = str(_value["authority_message_type"])
if not AUTHORITY_TOPIC.startswith("/") or not AUTHORITY_TYPE:
    raise ValueError("DAgger interface spec values are invalid")
DAGGER_RECORDING_TOPICS = (AUTHORITY_TOPIC,)
