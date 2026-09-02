from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence
from xml.etree.ElementTree import parse as parse_xml

import yaml


CONFIG_CATEGORIES = {
    "collection",
    "dataset_pipeline",
    "specs",
    "runner",
    "environment",
}


def validate(root: Path) -> tuple[Path, ...]:
    resolved = root.expanduser().resolve(strict=True)
    categories = {path.name for path in resolved.iterdir() if path.is_dir()}
    files = {path.name for path in resolved.iterdir() if path.is_file()}
    if categories != CONFIG_CATEGORIES or files:
        raise ValueError(
            "config root must contain exactly collection, dataset_pipeline, specs, "
            f"runner, and environment directories; directories={sorted(categories)} "
            f"files={sorted(files)}"
        )
    os.environ["ARX5_CONFIG_ROOT"] = str(resolved)

    from arx5_collection.adapters.bos.upload import UploadPolicy, UploadRoute
    from arx5_collection.collection.capture import load_capture_profiles
    from arx5_collection.collection.configuration import CollectionConfig
    from arx5_collection.collection.dagger.config import DaggerCollectorSettings
    from arx5_collection.collection.dagger.policy_server import PolicyServerSettings
    from arx5_collection.collection.environment import EnvironmentConfig
    from arx5_collection.collection.runtime.config import load_station_config
    from arx5_collection.collection.runtime.profiles import load_arm_profiles
    from arx5_collection.common.specs import Pi05Arx5Spec, RuntimeInterfaceSpec
    from arx5_collection.dataset_pipeline.application import load_pipeline_recipe
    from arx5_collection.dataset_pipeline.execution.bucketlink import (
        load_bucketlink_config,
    )
    from arx5_collection.dataset_pipeline.configuration.recipe import (
        DatasetPipelineRecipe,
    )
    from arx5_collection.dataset_pipeline.configuration.run import (
        DatasetPipelineConfig,
    )
    from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.recomposition.config import (
        load_config as load_composition_config,
    )

    validated = []
    environment = resolved / "environment/default.toml"
    EnvironmentConfig.load(environment)
    validated.append(environment)
    station = resolved / "environment/station.example.json"
    load_station_config(station)
    validated.append(station)
    fastdds = resolved / "environment/fastdds-shm.xml"
    parse_xml(fastdds)
    validated.append(fastdds)
    controller = resolved / "environment/v2_joint_control.yaml"
    _yaml_object(controller, "controller environment")
    validated.append(controller)

    capture = resolved / "specs/capture-profiles.toml"
    load_capture_profiles(capture)
    validated.append(capture)
    load_arm_profiles(resolved / "specs/arm-profiles.toml")
    validated.append(resolved / "specs/arm-profiles.toml")
    Pi05Arx5Spec.load()
    validated.append(resolved / "specs/pi05-arx5.toml")
    RuntimeInterfaceSpec.load(resolved / "specs/runtime-interface.toml")
    validated.append(resolved / "specs/runtime-interface.toml")
    validated.append(resolved / "specs/dagger-interface.toml")

    for path in sorted((resolved / "specs/recipes").glob("*.toml")):
        DatasetPipelineRecipe.load(path)
        validated.append(path)
    for path in sorted((resolved / "specs/schemas").glob("*.json")):
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or "$schema" not in value:
            raise ValueError(f"schema is not a JSON Schema object: {path}")
        validated.append(path)

    dagger_paths = set((resolved / "collection").glob("dagger.*.toml"))
    for path in sorted((resolved / "collection").glob("*.toml")):
        if path in dagger_paths:
            DaggerCollectorSettings.load(path)
            PolicyServerSettings.load(path)
        else:
            CollectionConfig.load(path)
        validated.append(path)

    for path in sorted((resolved / "dataset_pipeline").glob("streaming.*.toml")):
        config = DatasetPipelineConfig.load(path)
        load_pipeline_recipe(path, config)
        validated.append(path)
    for path in sorted((resolved / "dataset_pipeline").glob("bucketlink.*.toml")):
        _, config = load_bucketlink_config(path)
        load_pipeline_recipe(path, config)
        validated.append(path)
    for path in sorted((resolved / "dataset_pipeline").glob("composition.*.toml")):
        load_composition_config(path)
        validated.append(path)

    upload = resolved / "runner/bos-upload-validation-v1.toml"
    UploadPolicy.load(upload)
    UploadRoute.load(upload)
    validated.append(upload)
    viewer = resolved / "runner/viewer.toml"
    with viewer.open("rb"):
        from arx5_collection.adapters.viewer import cli as viewer_cli

    if viewer_cli.THUMBNAIL_WIDTH <= 0 or not 1 <= viewer_cli.JPEG_QUALITY <= 31:
        raise ValueError("viewer thumbnail settings are invalid")
    validated.append(viewer)
    for path in sorted((resolved / "runner").glob("compose.*.yaml")):
        value = _yaml_object(path, "Compose runner")
        if set(value) != {"name", "services"} or not isinstance(
            value["services"], dict
        ):
            raise ValueError(f"Compose runner keys are invalid: {path}")
        validated.append(path)
    for path in sorted((resolved / "runner").glob("*.env.example")):
        _validate_env(path)
        validated.append(path)
    return tuple(validated)


def _yaml_object(path: Path, label: str) -> dict[str, object]:
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} must be a non-empty YAML object: {path}")
    return value


def _validate_env(path: Path) -> None:
    names = set()
    for line_number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if (
            not separator
            or not name
            or not name.replace("_", "A").isalnum()
            or name != name.upper()
            or not value
        ):
            raise ValueError(f"invalid env assignment at {path}:{line_number}")
        if name in names:
            raise ValueError(f"duplicate env assignment {name} in {path}")
        names.add(name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arx5-config")
    parser.add_argument("command", choices=("validate",))
    parser.add_argument("--config-root", type=Path, default=Path("config"))
    args = parser.parse_args(argv)
    try:
        paths = validate(args.config_root)
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    for path in paths:
        print(f"PASS {path}")
    print(f"CONFIG VALID files={len(paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
