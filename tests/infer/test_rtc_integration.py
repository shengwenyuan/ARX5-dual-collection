from unittest.mock import Mock

import pytest

from arx5_collection.dagger.action_gateway import JointActionSafety, Pi05JointActionContract
from arx5_collection.dagger.rtc_scheduler import RtcActionScheduler
from arx5_collection.episode.models import EpisodeOutcome, RecordingStarted, RecordingStopping
from arx5_collection.episode.ports import TriggerEvent, TriggerSignal
from arx5_collection.gripper import ARX5_GRIPPER_CALIBRATION
from arx5_collection.infer.controller import InferController
from arx5_collection.infer.recording import RecordedControl
from tests.dagger.test_rtc_scheduler import CHECKPOINT, ROLLOUT, SHA, Clock, Mode, Policy, State, ticket


def setup_rtc(sink_error=False):
    policy, clock, mode = Policy(), Clock(), Mode()
    control = Mock()
    control.read.side_effect = State().read
    control.enable_policy_control.side_effect = mode.enable_policy_control
    records = []
    record_sink = Mock(side_effect=RuntimeError("record publish failed")) if sink_error else records.append
    commands = RecordedControl(control, record_sink, lambda: round(clock() * 1e9), lambda: 100_000_000_000 + round(clock() * 1e9))
    scheduler = RtcActionScheduler(
        policy, commands, commands,
        Pi05JointActionContract(SHA, ARX5_GRIPPER_CALIBRATION, JointActionSafety(0.25, 1.5, 0.0, 1.0), gripper_action_offset=0.1),
        commands, CHECKPOINT, ROLLOUT, 1.0, 0.1, clock=clock,
    )
    human = Mock()
    controller = InferController(scheduler, human, commands, {}, sleeper=lambda seconds: None)
    return controller, scheduler, policy, clock, control, commands, records, human


def test_real_scheduler_records_transformed_targets_and_discards_late_response():
    c, scheduler, policy, clock, control, commands, records, human = setup_rtc()
    c.start_episode(RecordingStarted("episode-one", 0))
    policy.futures[0].set_result(ticket(0))
    assert c.poll()
    for _ in range(2):
        scheduler.step()
        clock.advance()
    assert len(records) == 2 and len(policy.futures) == 2
    assert [r.step_index for r in records] == [0, 1]
    # The RTC prefix still contains model-space zero; the recorded target includes offset + denormalization.
    assert policy.calls[1][3].action_prefix[0][6] == 0.0
    assert records[0].action[6] == pytest.approx(-3.06)
    assert records[0].action[13] == pytest.approx(-3.06)
    sent = control.publish.call_args_list[0].args[0]
    assert records[0].action == (*sent.left, *sent.right)
    assert c.label(TriggerSignal(TriggerEvent.ABORT, 80_000_000)).event is TriggerEvent.TASK_FAIL
    policy.futures[1].set_result(ticket(0, value=0.1, fixed_prefix=1))
    for _ in range(5):
        scheduler.step()
        clock.advance()
    assert len(records) == 2 and control.publish.call_count == 2
    assert not scheduler.gate_open and commands.episode_id is None
    c.stop_episode(RecordingStopping(EpisodeOutcome.FAIL, 80_000_000))
    human.enable_gravity_compensation.assert_called_once()
    c.start_episode(RecordingStarted("episode-two", 300_000_000))
    policy.futures[-1].set_result(ticket(c.control_epoch))
    assert c.poll()
    scheduler.step()
    assert (records[-1].episode_id, records[-1].step_index) == ("episode-two", 0)
    c.close()


def test_actual_scheduler_propagates_record_publish_failure_and_closes_gate():
    c, scheduler, policy, clock, control, commands, records, human = setup_rtc(sink_error=True)
    c.start_episode(RecordingStarted("episode", 0))
    policy.futures[0].set_result(ticket(0))
    assert c.poll()
    scheduler.step()
    assert not scheduler.gate_open
    with pytest.raises(RuntimeError, match="record publish failed"):
        c.poll()
    c.stop_episode(RecordingStopping(EpisodeOutcome.FAIL, 1))
    assert commands.command_count == 1 and control.publish.call_count == 1
    human.enable_gravity_compensation.assert_called_once()
