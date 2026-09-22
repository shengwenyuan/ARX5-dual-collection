from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arx5_collection.dagger.action_runtime import open_takeover_action_runtime
from arx5_collection.dagger.config import DaggerCollectorSettings
from arx5_collection.dagger.models import PolicyExecutionProfile, RtcRolloutProfile
from tests.dagger.test_config import CONFIG


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "13"])
def test_invalid_expo_bootstrap_timeout_is_rejected(tmp_path, value):
    path = tmp_path / "policy.toml"
    path.write_text(
        CONFIG
        + f'\n[gateway]\nexpo_bootstrap_timeout_s = {value}\n[expo]\nbundle_id = "test"\n'
    )
    with pytest.raises(ValueError, match="bootstrap timeout"):
        DaggerCollectorSettings.load(path)


def test_runtime_uses_separate_timeout_only_for_expo(tmp_path):
    root = Path(__file__).resolve().parents[2]
    settings = DaggerCollectorSettings.load(
        root / "config/dagger.pi05-stacking-v3-rtc.toml"
    )
    cp = replace(
        settings.checkpoint_profile, execution=PolicyExecutionProfile(30, 14, 9, 30.0)
    )
    settings = replace(
        settings,
        checkpoint_profile=cp,
        execution=cp.execution,
        rtc_rollout=RtcRolloutProfile(1, 8, 10, "rolling_max"),
        control=replace(settings.control, policy_wait_timeout_s=0.15),
    )
    for expo in (True, False):
        policy = Mock(expo_two_phase=expo)
        with open_takeover_action_runtime(
            settings, policy, Mock(), tmp_path
        ) as runtime:
            assert runtime.gateway.bootstrap_timeout_s == (2.0 if expo else 0.15)
            assert runtime.gateway.policy_wait_timeout_s == 0.15
            assert not runtime.gateway.gate_open


def test_config_can_override_expo_bootstrap_timeout(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_text(CONFIG + "\n[gateway]\nexpo_bootstrap_timeout_s = 3.0\n")
    assert DaggerCollectorSettings.load(path).control.expo_bootstrap_timeout_s == 3.0
