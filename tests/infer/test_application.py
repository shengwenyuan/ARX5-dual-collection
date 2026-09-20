from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from arx5_collection.episode.adapters.pedal import PedalUnavailable
from arx5_collection.episode.models import RecordingStarted
from arx5_collection.infer.application import InferApplication
from tests.infer.test_collection import Gateway, spec


@pytest.mark.parametrize("loop_error", [False, True])
def test_application_wires_hooks_and_closes_policy_and_executor_on_exit(tmp_path, loop_error):
    app = InferApplication.build(spec(tmp_path))
    original = app.session
    app.session = MagicMock(station=original.station, camera_snapshot=original.camera_snapshot)
    events = []
    actions = SimpleNamespace(gateway=Gateway(events), executor=Mock())
    module = "arx5_collection.infer.application."
    with ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(module + name))
            for name in (
                "open_configured_pedals", "RosDualArmControlPort", "OpenPiDaggerTransport",
                "LocalVlaSnapshotClient", "RosCommandPublisher", "AsyncPi05PolicyClient",
                "open_takeover_action_runtime", "run_episode_loop",
            )
        }
        mocks["open_takeover_action_runtime"].return_value.__enter__.return_value = actions
        def loop(*args, **kwargs):
            wiring = app.session.create_runtime.call_args.kwargs
            assert wiring["metadata_context_provider"].__self__ is wiring["recording_stopping_hook"].__self__
            wiring["recording_started_hook"](RecordingStarted("ep", 0))
            if loop_error:
                raise RuntimeError("loop failure")
            return 0
        mocks["run_episode_loop"].side_effect = loop
        if loop_error:
            with pytest.raises(RuntimeError, match="loop failure"):
                app.run()
        else:
            assert app.run() == 0
        actions.executor.start.assert_called_once()
        actions.executor.close.assert_called_once()
        mocks["AsyncPi05PolicyClient"].return_value.close.assert_called_once()
        app.session.home_controller.enable_gravity_compensation.assert_called_once()
        assert "gate_closed" in events


def test_missing_pedals_stop_before_session_hardware_starts(tmp_path):
    app = InferApplication.build(spec(tmp_path))
    app.session = MagicMock(camera_snapshot=app.session.camera_snapshot)
    with patch("arx5_collection.infer.application.open_configured_pedals", side_effect=PedalUnavailable("missing pedals")):
        with pytest.raises(PedalUnavailable, match="missing pedals"):
            app.run()
    app.session.__enter__.assert_not_called()
