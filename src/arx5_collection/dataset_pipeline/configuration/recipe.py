from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from arx5_collection.config import config_path
from arx5_collection.common.gripper import ARX5_GRIPPER_CALIBRATION
from arx5_collection.common.gripper import ARX5_GRIPPER_CONTRACT_ID
from arx5_collection.common.gripper import GripperCalibration
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    EqualEefPolicy,
)
from arx5_collection.dataset_pipeline.mining_stage.dataset_generator.models import (
    VideoEncodingConfig,
)
from arx5_collection.dataset_pipeline.source.models import CleaningPolicy


STAGE_NAMES = (
    "episode_sanitycheck",
    "action_mining",
    "dataset_generator",
)

BUILTIN_RECIPE_PREFIX = "builtin:"
RECIPE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class UnitSpec:
    type: str
    params: dict[str, object]


@dataclass(frozen=True, slots=True)
class StageSpec:
    name: str
    units: tuple[UnitSpec, ...]


@dataclass(frozen=True, slots=True)
class PipelineSpec:
    stages: tuple[StageSpec, ...]

    def stage(self, name: str) -> StageSpec:
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class DatasetPipelineRecipe:
    schema_version: int
    name: str
    builder_backend: str
    gripper_normalization: str
    gripper_contract: str
    gripper: GripperCalibration
    pipeline: PipelineSpec

    @property
    def cleaning(self) -> CleaningPolicy:
        timeline = _configured_unit(
            self.pipeline,
            "episode_sanitycheck",
            "timeline_check",
        )
        frame_alignment = _configured_unit(
            self.pipeline,
            "episode_sanitycheck",
            "frame_alignment",
        )
        alignment_report = _configured_unit(
            self.pipeline,
            "episode_sanitycheck",
            "alignment_report",
        )
        return CleaningPolicy(
            cross_camera_tolerance_ns=_int(
                frame_alignment.params["cross_camera_tolerance_ns"],
                "frame_alignment.cross_camera_tolerance_ns",
            ),
            arm_max_age_ns=_int(
                frame_alignment.params["arm_max_age_ns"],
                "frame_alignment.arm_max_age_ns",
            ),
            camera_gap_warning_ns=_int(
                timeline.params["camera_gap_warning_ns"],
                "timeline_check.camera_gap_warning_ns",
            ),
            arm_gap_warning_ns=_int(
                timeline.params["arm_gap_warning_ns"],
                "timeline_check.arm_gap_warning_ns",
            ),
            grade_a_coverage=_number(
                alignment_report.params["grade_a_coverage"],
                "alignment_report.grade_a_coverage",
            ),
            grade_b_coverage=_number(
                alignment_report.params["grade_b_coverage"],
                "alignment_report.grade_b_coverage",
            ),
        )

    @property
    def selection(self) -> EqualEefPolicy:
        training_interval = _configured_unit(
            self.pipeline,
            "action_mining",
            "training_interval",
        )
        action_sampler = _configured_unit(
            self.pipeline,
            "action_mining",
            "equal_eef_action_sampler",
        )
        motion_segmenter = _configured_unit(
            self.pipeline,
            "action_mining",
            "motion_segmenter",
        )
        return _selection_policy(
            {
                **action_sampler.params,
                **motion_segmenter.params,
                **training_interval.params,
            },
            "action_mining",
        )

    @property
    def video(self) -> VideoEncodingConfig | None:
        for unit in self.pipeline.stage("dataset_generator").units:
            if unit.type == "lerobot_fragment_generator":
                value = unit.params.get("video")
                if self.schema_version == 2 and value is not None:
                    raise ValueError(
                        "schema_version 2 dataset generator must not configure video"
                    )
                if self.schema_version == 3 and value is None:
                    raise ValueError(
                        "schema_version 3 dataset generator requires video parameters"
                    )
                return (
                    None
                    if value is None
                    else _video_config_value(
                        value,
                        "lerobot_fragment_generator.video",
                    )
                )
        raise ValueError(
            "dataset_generator does not configure lerobot_fragment_generator"
        )

    @classmethod
    def load(cls, reference: str | Path) -> DatasetPipelineRecipe:
        if isinstance(reference, str) and reference.startswith(BUILTIN_RECIPE_PREFIX):
            name = reference.removeprefix(BUILTIN_RECIPE_PREFIX)
            if RECIPE_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid dataset pipeline recipe name: {name}")
            stream_context = config_path(f"specs/recipes/{name}.toml").open("rb")
        else:
            stream_context = Path(reference).open("rb")
        with stream_context as stream:
            payload = tomllib.load(stream)
        schema_version = payload.get("schema_version")
        if schema_version not in {2, 3}:
            raise ValueError("dataset pipeline recipe schema_version must be 2 or 3")
        return _load_unit_recipe(payload, schema_version)


def _load_unit_recipe(
    payload: dict[str, object],
    schema_version: int,
) -> DatasetPipelineRecipe:
    _exact_keys(
        payload,
        {
            "schema_version",
            "name",
            "builder_backend",
            "gripper_normalization",
            "gripper_contract",
            "stages",
        },
        "dataset pipeline recipe",
    )
    name = _string(payload["name"], "name")
    if RECIPE_NAME.fullmatch(name) is None:
        raise ValueError("dataset pipeline recipe name is invalid")
    backend = _string(payload["builder_backend"], "builder_backend")
    if backend != "lerobot-v2.1":
        raise ValueError("unsupported conversion builder_backend")
    normalization = _string(payload["gripper_normalization"], "gripper_normalization")
    if normalization != "linear_open_closed_0_1":
        raise ValueError("unsupported gripper_normalization")
    gripper_contract = _string(payload["gripper_contract"], "gripper_contract")
    if gripper_contract != ARX5_GRIPPER_CONTRACT_ID:
        raise ValueError("unsupported gripper_contract")
    pipeline = _pipeline_spec(payload["stages"], schema_version)
    result = DatasetPipelineRecipe(
        schema_version=schema_version,
        name=name,
        builder_backend=backend,
        gripper_normalization=normalization,
        gripper_contract=gripper_contract,
        gripper=ARX5_GRIPPER_CALIBRATION,
        pipeline=pipeline,
    )
    return result


def _pipeline_spec(value: object, schema_version: int) -> PipelineSpec:
    stages = _mapping(value, "stages")
    _exact_keys(stages, set(STAGE_NAMES), "stages")
    result = []
    for stage_name in STAGE_NAMES:
        stage = _mapping(stages[stage_name], f"stages.{stage_name}")
        _exact_keys(stage, {"units"}, f"stages.{stage_name}")
        rows = stage["units"]
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"stages.{stage_name}.units must be a non-empty array")
        units = tuple(
            _unit_spec(stage_name, row, index, schema_version)
            for index, row in enumerate(rows)
        )
        types = [unit.type for unit in units]
        if len(types) != len(set(types)):
            raise ValueError(f"stages.{stage_name} contains duplicate unit types")
        result.append(StageSpec(stage_name, units))
    return PipelineSpec(tuple(result))


_UNIT_PARAMS = {
    "episode_sanitycheck": {
        "metadata_check": set(),
        "mcap_check": set(),
        "timeline_check": {"camera_gap_warning_ns", "arm_gap_warning_ns"},
        "arm_signal_check": set(),
        "frame_alignment": {"cross_camera_tolerance_ns", "arm_max_age_ns"},
        "alignment_report": {"grade_a_coverage", "grade_b_coverage"},
    },
    "action_mining": {
        "dagger_authority": {
            "monotonic_anchor_tolerance_ns",
            "bag_anchor_tolerance_ns",
        },
        "episode_filter": set(),
        "training_interval": {"max_episode_duration_s"},
        "equal_eef_action_sampler": {
            "eef_distance_m",
            "gripper_delta_threshold",
            "max_sample_interval_ns",
            "image_max_age_ns",
            "arm_max_age_ns",
            "action_horizon",
            "nominal_fps",
        },
        "motion_segmenter": {
            "idle_delta_threshold",
            "min_idle_frames",
            "min_motion_frames",
            "trim_segment_end_frames",
        },
        "trajectory_labeler": set(),
    },
    "dataset_generator": {
        "lerobot_fragment_generator": {"video"},
        "lerobot_fragment_validator": set(),
        "lerobot_dataset_merge": set(),
        "lerobot_dataset_validator": set(),
    },
}


def _unit_spec(
    stage_name: str,
    value: object,
    index: int,
    schema_version: int,
) -> UnitSpec:
    label = f"stages.{stage_name}.units[{index}]"
    row = _mapping(value, label)
    _exact_keys(row, {"type", "params"}, label)
    unit_type = _string(row["type"], f"{label}.type")
    try:
        expected_params = _UNIT_PARAMS[stage_name][unit_type]
    except KeyError as error:
        raise ValueError(f"unsupported {stage_name} unit: {unit_type}") from error
    params = _mapping(row["params"], f"{label}.params")
    if unit_type == "lerobot_fragment_generator":
        allowed = {"video"}
        if set(params) - allowed:
            raise ValueError(f"{label}.params keys must be within {sorted(allowed)}")
        if schema_version == 2 and "video" in params:
            raise ValueError(
                "schema_version 2 dataset generator must not configure video"
            )
        if schema_version == 3 and "video" not in params:
            raise ValueError(
                "schema_version 3 dataset generator requires video parameters"
            )
        if "video" in params:
            _video_config_value(params["video"], f"{label}.params.video")
    else:
        _exact_keys(params, expected_params, f"{label}.params")
        _validate_unit_params(unit_type, params, f"{label}.params")
    return UnitSpec(unit_type, dict(params))


def _validate_unit_params(
    unit_type: str,
    params: dict[str, object],
    label: str,
) -> None:
    if unit_type == "timeline_check":
        values = tuple(
            _int(params[name], f"{label}.{name}")
            for name in ("camera_gap_warning_ns", "arm_gap_warning_ns")
        )
        if min(values) < 0:
            raise ValueError("timeline warning thresholds must not be negative")
    elif unit_type == "frame_alignment":
        values = tuple(
            _int(params[name], f"{label}.{name}")
            for name in ("cross_camera_tolerance_ns", "arm_max_age_ns")
        )
        if min(values) < 0:
            raise ValueError("frame alignment tolerances must not be negative")
    elif unit_type == "alignment_report":
        grade_a = _number(params["grade_a_coverage"], f"{label}.grade_a_coverage")
        grade_b = _number(params["grade_b_coverage"], f"{label}.grade_b_coverage")
        if not 0 <= grade_b <= grade_a <= 1:
            raise ValueError("grade coverage thresholds must satisfy 0 <= B <= A <= 1")
    elif unit_type == "dagger_authority":
        values = tuple(
            _int(params[name], f"{label}.{name}")
            for name in (
                "monotonic_anchor_tolerance_ns",
                "bag_anchor_tolerance_ns",
            )
        )
        if min(values) <= 0:
            raise ValueError("dagger authority anchor tolerances must be positive")
    elif unit_type == "training_interval":
        if (
            _number(
                params["max_episode_duration_s"],
                f"{label}.max_episode_duration_s",
            )
            < 0
        ):
            raise ValueError("max_episode_duration_s must not be negative")
    elif unit_type == "equal_eef_action_sampler":
        positive_numbers = tuple(
            _number(params[name], f"{label}.{name}")
            for name in ("eef_distance_m", "gripper_delta_threshold")
        )
        positive_integers = tuple(
            _int(params[name], f"{label}.{name}")
            for name in ("max_sample_interval_ns", "action_horizon", "nominal_fps")
        )
        ages = tuple(
            _int(params[name], f"{label}.{name}")
            for name in ("image_max_age_ns", "arm_max_age_ns")
        )
        if min((*positive_numbers, *positive_integers)) <= 0 or min(ages) < 0:
            raise ValueError("equal EEF sampling parameters are invalid")
    elif unit_type == "motion_segmenter":
        values = (
            _number(
                params["idle_delta_threshold"],
                f"{label}.idle_delta_threshold",
            ),
            *(
                _int(params[name], f"{label}.{name}")
                for name in (
                    "min_idle_frames",
                    "min_motion_frames",
                    "trim_segment_end_frames",
                )
            ),
        )
        if min(values) < 0:
            raise ValueError("motion segmentation parameters must not be negative")


def _configured_unit(
    pipeline: PipelineSpec, stage_name: str, unit_type: str
) -> UnitSpec:
    for unit in pipeline.stage(stage_name).units:
        if unit.type == unit_type:
            return unit
    raise ValueError(f"stages.{stage_name} does not configure unit {unit_type}")


def _selection_policy(params: dict[str, object], label: str) -> EqualEefPolicy:
    return EqualEefPolicy(
        eef_distance_m=_number(params["eef_distance_m"], f"{label}.eef_distance_m"),
        gripper_delta_threshold=_number(
            params["gripper_delta_threshold"], f"{label}.gripper_delta_threshold"
        ),
        max_sample_interval_ns=_int(
            params["max_sample_interval_ns"], f"{label}.max_sample_interval_ns"
        ),
        image_max_age_ns=_int(params["image_max_age_ns"], f"{label}.image_max_age_ns"),
        arm_max_age_ns=_int(params["arm_max_age_ns"], f"{label}.arm_max_age_ns"),
        action_horizon=_int(params["action_horizon"], f"{label}.action_horizon"),
        nominal_fps=_int(params["nominal_fps"], f"{label}.nominal_fps"),
        idle_delta_threshold=_number(
            params["idle_delta_threshold"], f"{label}.idle_delta_threshold"
        ),
        min_idle_frames=_int(params["min_idle_frames"], f"{label}.min_idle_frames"),
        min_motion_frames=_int(
            params["min_motion_frames"], f"{label}.min_motion_frames"
        ),
        trim_segment_end_frames=_int(
            params["trim_segment_end_frames"], f"{label}.trim_segment_end_frames"
        ),
        max_episode_duration_s=_number(
            params["max_episode_duration_s"], f"{label}.max_episode_duration_s"
        ),
    )


def _video_config_value(value: object, label: str) -> VideoEncodingConfig:
    video = _mapping(value, label)
    _exact_keys(
        video,
        {"codec", "pixel_format", "gop", "crf", "preset", "threads"},
        label,
    )
    return VideoEncodingConfig(
        codec=_string(video["codec"], f"{label}.codec"),
        pixel_format=_string(video["pixel_format"], f"{label}.pixel_format"),
        gop=_int(video["gop"], f"{label}.gop"),
        crf=_int(video["crf"], f"{label}.crf"),
        preset=_int(video["preset"], f"{label}.preset"),
        threads=_int(video["threads"], f"{label}.threads"),
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a table")
    return value


def _exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} keys must be exactly {sorted(expected)}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    return float(value)
