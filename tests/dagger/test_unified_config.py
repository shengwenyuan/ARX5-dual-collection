from copy import deepcopy
import json
from pathlib import Path
import runpy
import sys
import tomllib
from unittest.mock import patch

import pytest

from arx5_collection.dagger.config import DaggerCollectorSettings
from arx5_collection.dagger.policy_server import (
    PolicyServerSettings,
    create_pi05_joint_policy,
)
from arx5_collection.dagger.unified_config import (
    prepare_policy,
    resolve_config,
    write_toml,
)
from arx5_collection.production.config import load_station_config

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "config/dagger.pi05-stacking-v3-rtc.toml"


def source_config():
    return {
        "policy": {
            "mode": "training_time_rtc",
            "model_variant": "arx5_base",
            "repo_id": "local/example",
            "instruction": "slot 4",
            "action_chunk_size": 50,
            "execution_steps": 10,
        },
        "rtc": {
            "prefix_attention_schedule": "hard_prefix",
            "num_flow_steps": 10,
            "minimum_execution_horizon": 10,
            "initial_delay_steps": 3,
            "delay_history_size": 10,
        },
        "robot": {"rate_hz": 50.0},
        "cameras": {"width": 640, "height": 360},
        "safety": {"max_joint_step_rad": 0.25, "max_joint_departure_rad": 1.5},
        "gripper": {
            "opening_offset": 0.02,
            "closing_offset": 0.02,
            "left_open_raw": -3.4,
            "right_open_raw": -3.4,
            "left_closed_raw": 0.0,
            "right_closed_raw": 0.0,
        },
    }


def resolve(source):
    return resolve_config(
        source,
        tomllib.loads(TEMPLATE.read_text()),
        checkpoint_metadata={"policy_type": "training_time_rtc", "max_delay": 10},
        checkpoint_sha256="a" * 64,
        checkpoint_path="/checkpoints/test/29999",
    )


@pytest.mark.parametrize(
    "rate,wait,snapshot", [(50, 0.15, 0.1), (25, 0.35, 0.2), (20, 0.35, 0.2)]
)
def test_one_config_drives_both_sides_without_scheduler_change(
    tmp_path, rate, wait, snapshot
):
    source = source_config()
    source["robot"]["rate_hz"] = rate
    original = deepcopy(source)
    output = tmp_path / "policy.toml"
    write_toml(output, resolve(source))
    collector = DaggerCollectorSettings.load(output)
    server = PolicyServerSettings.load(output)
    assert collector.checkpoint_profile == server.checkpoint_profile
    assert collector.rtc_rollout == server.rtc_rollout
    assert collector.execution.control_rate_hz == rate
    assert collector.control.policy_wait_timeout_s == pytest.approx(wait)
    assert collector.snapshot_timeout_s == pytest.approx(snapshot)
    assert collector.gripper_action_offset == 0.02
    assert collector.control.safety.max_normalized_gripper == 1.0
    assert server.model_variant == "arx5_base"
    assert source == original


@pytest.mark.parametrize(
    "table,key,value,match",
    [
        ("policy", "model_variant", "tipcrop", "unsupported model_variant"),
        ("policy", "mode", "vanilla_pi05", "training_time_rtc"),
        ("gripper", "closing_offset", 0.1, "direction-dependent"),
        ("gripper", "left_open_raw", -2.0, "calibration"),
        ("rtc", "prefix_attention_schedule", "soft_prefix", "hard_prefix"),
        ("robot", "rate_hz", 1000, "no room"),
    ],
)
def test_unsupported_contracts_rejected(table, key, value, match):
    source = source_config()
    source[table][key] = value
    with pytest.raises(ValueError, match=match):
        resolve(source)


def fixture_bundle(tmp_path):
    source = source_config()
    checkpoint = tmp_path / "checkpoints/run/29999"
    assets = checkpoint / "assets/local/example"
    assets.mkdir(parents=True)
    (assets / "norm_stats.json").write_text("{}")
    (checkpoint / "assets/policy_type.json").write_text(
        '{"policy_type":"training_time_rtc","max_delay":10}'
    )
    (checkpoint / "params").mkdir()
    (checkpoint / "params/weights").write_bytes(b"checkpoint")
    openpi = tmp_path / "openpi"
    model = openpi / "src/openpi/experiments/arx5_base/model_config.py"
    model.parent.mkdir(parents=True)
    model.write_text("# fixture")
    (openpi / "packages/openpi-client/src/openpi_client").mkdir(parents=True)
    source["policy"].update(checkpoint=str(checkpoint), openpi_root=str(openpi))
    station = ROOT / "config/station.example.json"
    keys = {
        "overview": "cam_high",
        "left": "cam_left_wrist",
        "right": "cam_right_wrist",
    }
    for camera in load_station_config(station).cameras:
        source["cameras"][keys[camera.role]] = camera.serial_number
    path = tmp_path / "inference.toml"
    write_toml(path, source)
    return dict(
        inference_config=path,
        runtime_config=TEMPLATE,
        output_dir=tmp_path / "resolved",
        checkpoint_root=tmp_path / "checkpoints",
        station_config=station,
    )


def test_prepared_paths_identity_and_readonly_source_mount(tmp_path):
    args = fixture_bundle(tmp_path)
    config, compose = prepare_policy(**args)
    source = tomllib.loads(config.read_text())
    assert source["policy"]["checkpoint"] == "/checkpoints/run/29999"
    assert len(source["policy"]["checkpoint_sha256"]) == 64
    assert source["config_source"]["inference_config"] == str(args["inference_config"])
    mount = json.loads(compose.read_text())["services"]["policy-server"]["volumes"][0]
    assert mount["read_only"] and mount["bind"]["create_host_path"] is False
    assert mount["source"] == str(tmp_path / "openpi")


def test_camera_mismatch_rejected_before_output(tmp_path):
    args = fixture_bundle(tmp_path)
    source = tomllib.loads(args["inference_config"].read_text())
    source["cameras"]["cam_high"] = "another station"
    write_toml(args["inference_config"], source)
    with pytest.raises(ValueError, match="camera serial mismatch"):
        prepare_policy(**args)
    assert not args["output_dir"].exists()


def test_source_must_be_selected_before_any_openpi_import(tmp_path):
    args = fixture_bundle(tmp_path)
    config, _ = prepare_policy(**args)
    payload = tomllib.loads(config.read_text())
    payload["policy"]["openpi_root"] = str(tmp_path / "openpi")
    write_toml(config, payload)
    settings = PolicyServerSettings.load(config)
    with patch.dict(sys.modules, {"openpi": object()}):
        with pytest.raises(RuntimeError, match="before importing"):
            create_pi05_joint_policy(settings)


@pytest.mark.parametrize("mode", ["infer", "takeover", "shadow"])
def test_host_entry_passes_resolved_bundle_to_existing_compose(tmp_path, mode):
    args = fixture_bundle(tmp_path)
    station = tmp_path / "station"
    station.mkdir()
    (station / "dagger.env").write_text(
        f"ARX5_CHECKPOINT_ROOT={args['checkpoint_root']}\nARX5_DAGGER_POLICY_CONFIG={TEMPLATE}\n"
    )
    (station / "station.json").write_bytes(args["station_config"].read_bytes())
    module = runpy.run_path(str(ROOT / "scripts/arx5"))
    main = module["main"]
    main.__globals__["STATION_DIR"] = station
    command = ["infer"] if mode == "infer" else ["dagger", "--mode", mode]
    command += ["--rgb-only", "--inference-config", str(args["inference_config"])]
    with patch.dict(
        "os.environ",
        {
            "ARX5_OUTPUT_ROOT": str(tmp_path / "episodes"),
            "ARX5_TASK_DESCRIPTION": "slot 4",
        },
        clear=True,
    ):
        with patch("sys.argv", ["arx5", *command]):
            with patch("subprocess.call", return_value=0) as call:
                assert main() == 0
    argv = call.call_args.args[0]
    env = call.call_args.kwargs["env"]
    assert (str(ROOT / "docker/compose.infer.yaml") in argv) == (mode == "infer")
    assert env["ARX5_DAGGER_MODE"] == mode
    assert str(tmp_path / "episodes/.policy/compose.policy.json") in argv
    assert env["ARX5_DAGGER_POLICY_CONFIG"] == str(
        tmp_path / "episodes/.policy/policy.toml"
    )
    assert env["ARX5_TASK_CONFIG"] == str(ROOT / "config/task.rgb-only.json")
    assert "--no-build" in argv
