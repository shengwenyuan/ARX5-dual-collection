from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from arx5_collection.infer.unrecorded import run_unrecorded
from arx5_collection.episode.ports import TriggerEvent


@pytest.mark.parametrize('failure',[None,'camera stalled'])
def test_unrecorded_keeps_sensor_guard_and_stops_on_exit(failure):
    session=SimpleNamespace(supervisor=Mock(),monitor=Mock(),pre_episode_check=Mock(),home=Mock())
    session.monitor.required_failure.return_value=failure
    controller=Mock()
    with patch('arx5_collection.infer.controller.InferRecordTrigger') as trigger:
        trigger.return_value.wait.side_effect=[SimpleNamespace(event=TriggerEvent.ACTIVATE),SimpleNamespace(event=TriggerEvent.ACTIVATE)]
        if failure:
            with pytest.raises(RuntimeError,match='camera stalled'):
                run_unrecorded(session,controller,Mock(),1,Mock(),streams=('camera',))
        else:
            assert run_unrecorded(session,controller,Mock(),1,Mock(),streams=('camera',))==0
        session.monitor.start.assert_called_once_with(('camera',))
        session.monitor.stop.assert_called_once()
        session.home.run.assert_called_once()
        assert controller.close.called
