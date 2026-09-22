from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from arx5_collection.infer.unrecorded import run_unrecorded
from arx5_collection.episode.ports import TriggerEvent


@pytest.mark.parametrize("failure", [None, "camera stalled"])
def test_unrecorded_keeps_sensor_guard_and_stops_on_exit(failure):
    session = SimpleNamespace(
        supervisor=Mock(), monitor=Mock(), pre_episode_check=Mock(), home=Mock()
    )
    session.monitor.required_failure.return_value = failure
    controller = Mock()
    with patch("arx5_collection.infer.controller.InferRecordTrigger") as trigger:
        trigger.return_value.wait.side_effect = [
            SimpleNamespace(event=TriggerEvent.ACTIVATE),
            SimpleNamespace(event=TriggerEvent.ACTIVATE),
        ]
        if failure:
            with pytest.raises(RuntimeError, match="camera stalled"):
                run_unrecorded(
                    session, controller, Mock(), 1, Mock(), streams=("camera",)
                )
        else:
            assert (
                run_unrecorded(
                    session, controller, Mock(), 1, Mock(), streams=("camera",)
                )
                == 0
            )
        session.monitor.start.assert_called_once_with(("camera",))
        session.monitor.stop.assert_called_once_with(audit_recording=False)
        session.home.run.assert_called_once()
        assert controller.close.called


@pytest.mark.parametrize("failure", [None, "RTC bootstrap inference timeout"])
def test_real_monitor_never_audits_missing_mcap_and_preserves_policy_error(failure):
    from arx5_collection.ros2_adapters.monitor import RosStreamMonitor
    from arx5_collection.episode.models import StreamSpec

    backend = Mock()
    backend.metrics.side_effect = RuntimeError("no completed MCAP is available")
    monitor = RosStreamMonitor(backend, status_sink=None)
    monitor._context = object()  # No ROS node or devices are created.
    session = SimpleNamespace(
        supervisor=Mock(), monitor=monitor, pre_episode_check=Mock(), home=Mock()
    )
    controller = Mock()
    signal = SimpleNamespace(event=TriggerEvent.ACTIVATE)
    streams = (StreamSpec("camera", "/camera", True, 30.0),)
    with patch("arx5_collection.infer.controller.InferRecordTrigger") as trigger:
        trigger.return_value.wait.side_effect = [
            signal,
            RuntimeError(failure) if failure else signal,
        ]
        if failure:
            with pytest.raises(RuntimeError, match=failure):
                run_unrecorded(session, controller, Mock(), 1, Mock(), streams=streams)
        else:
            assert (
                run_unrecorded(session, controller, Mock(), 1, Mock(), streams=streams)
                == 0
            )
    backend.metrics.assert_not_called()
    assert monitor._health is None
    assert monitor._streams == ()


def test_cleanup_failure_does_not_mask_original_policy_fault():
    session = SimpleNamespace(
        supervisor=Mock(), monitor=Mock(), pre_episode_check=Mock(), home=Mock()
    )
    session.monitor.required_failure.return_value = None
    session.monitor.stop.side_effect = RuntimeError("monitor executor stopped")
    status = Mock()
    with patch("arx5_collection.infer.controller.InferRecordTrigger") as trigger:
        trigger.return_value.wait.side_effect = [
            SimpleNamespace(event=TriggerEvent.ACTIVATE),
            RuntimeError("policy failed"),
        ]
        with pytest.raises(RuntimeError, match="policy failed"):
            run_unrecorded(session, Mock(), Mock(), 1, status, streams=())
    assert any(
        "monitor executor stopped" in call.args[0] for call in status.call_args_list
    )
