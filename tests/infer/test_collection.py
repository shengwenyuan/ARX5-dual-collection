from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from jsonschema import Draft202012Validator
import pytest

from arx5_collection.collection_metadata import CollectionType, MetadataContext
from arx5_collection.dagger.action_gateway import DualArmJointCommand, DualArmJointState
from arx5_collection.dagger.application import DaggerRunSpec
from arx5_collection.episode.cli import run_episode_loop
from arx5_collection.episode.models import EpisodeOutcome, EpisodeRequest, RecordingStarted, RecordingStopping, StreamMetrics, StreamSpec
from arx5_collection.episode.ports import TriggerEvent, TriggerSignal
from arx5_collection.episode.runtime import EpisodeRuntime
from arx5_collection.episode.store import EpisodeStore
from arx5_collection.infer.application import InferApplication
from arx5_collection.infer.controller import InferController, InferRecordTrigger
from arx5_collection.infer.recording import CommandRecord, RecordedControl, RosCommandPublisher
from arx5_collection.production.cli import build_parser
from tests.episode.fakes import FakeBackend, FakeMonitor, FakeTrigger

ROOT = Path(__file__).parents[2]
STREAM = StreamSpec("left_arm", "/embodiments/left_arm/state", True, 60.0)
METRICS = (StreamMetrics(STREAM.id, 120, 2.0, 60.0, 18.0),)
ACTION = DualArmJointCommand((0.1,) * 6 + (-3.4,), (0.2,) * 6 + (0.0,))


def spec(tmp_path):
    return DaggerRunSpec(
        ROOT / "config/station.example.json", ROOT / "config/task.rgb-only.json",
        "fold cloth", ROOT / "config/dagger.pi05-fold-cloth-20260828-train-rtc.toml",
        tmp_path, 1, 1, 30.0, "test", "session-test", compression_enabled=False,
    )


class Gateway:
    def __init__(self, events):
        self.events = events
        self.ready = True
        self.fault = None
        self.action_output_enabled = False

    def clear_pending(self, epoch):
        self.events.append("clear")
        self.action_output_enabled = False

    def prepare_policy(self, episode_id, epoch):
        self.events.append("prepare")

    def policy_ready(self, episode_id, epoch):
        self.action_output_enabled = self.ready
        return self.ready

    def close_gate(self, epoch):
        self.events.append("gate_closed")
        self.action_output_enabled = False

    def take_fault(self):
        fault, self.fault = self.fault, None
        return fault


def setup_controller(configuration=None):
    events, records = [], []
    control = Mock()
    control.publish.side_effect = lambda command: events.append("publish")
    control.read.return_value = DualArmJointState((0.0,) * 6, (0.0,) * 6)
    commands = RecordedControl(control, records.append, lambda: 1_500_000_000, lambda: 9_500_000_000)
    gateway = Gateway(events)
    human = Mock()
    human.enable_gravity_compensation.side_effect = lambda: events.append("gravity")
    controller = InferController(
        gateway, human, commands, configuration or {}, clock=lambda: 3_000_000_000,
        sleeper=lambda seconds: events.append("tail"),
    )
    return controller, commands, gateway, control, human, events, records


def test_build_reuses_session_and_adds_only_command_topic(tmp_path):
    app = InferApplication.build(spec(tmp_path))
    assert app.session.fail_directory == "infer_fail"
    assert app.session.finalizer.enabled is False
    assert app.session.backend.additional_topics == ("/infer/command",)
    assert len(app.configuration["action_order"]) == 14
    assert app.configuration["action_units"][6] == "vendor_raw"
    assert app.configuration["checkpoint_profile"]["policy_type"] == "training_time_rtc"
    assert len(app.request.streams) == 5


def test_rejects_non_rtc_and_invalid_tail_before_hardware(tmp_path):
    with pytest.raises(ValueError, match="training_time_rtc"):
        InferApplication.build(replace(spec(tmp_path), policy_config=ROOT / "config/dagger.policy.example.toml"))
    for value in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite and positive"):
            InferApplication.build(spec(tmp_path), value)


def test_cli_infer_defaults_and_legacy_compression(tmp_path):
    common = ["--output-root", str(tmp_path), "--task-config", "task.json", "--task-description", "fold"]
    infer = build_parser().parse_args(["infer", *common, "--policy-config", "policy.toml"])
    assert infer.no_compress and infer.post_stop_recording_s == 0.1
    assert not build_parser().parse_args(["run", *common]).no_compress


@pytest.mark.parametrize("raw_event,outcome,directory,reason,source", [
    (TriggerEvent.ACTIVATE, "success", "", "human_label", "right_pedal"),
    (TriggerEvent.ABORT, "fail", "infer_fail", "human_label", "left_pedal"),
    (TriggerEvent.CONFLICT, "aborted", "abort", "label_conflict", "both_pedals"),
])
def test_full_fake_episode_commits_and_stops_before_recorder(tmp_path, raw_event, outcome, directory, reason, source):
    app = InferApplication.build(spec(tmp_path))
    c, commands, gateway, control, human, events, records = setup_controller(app.configuration)

    class Backend(FakeBackend):
        def start(self, *args):
            events.append("recorder_start")
            super().start(*args)

        def stop(self):
            assert not gateway.action_output_enabled
            events.append("recorder_stop")
            super().stop()

    class Pedals(FakeTrigger):
        def wait(self, timeout_s):
            signal = super().wait(timeout_s)
            if c.active and gateway.action_output_enabled:
                commands.publish(ACTION)
            return signal

    trigger = InferRecordTrigger(Pedals([True, raw_event], clock_ns=lambda: 2_000_000_000), c)
    runtime = EpisodeRuntime(
        EpisodeStore(tmp_path, fail_directory="infer_fail"), trigger, Backend(), FakeMonitor(METRICS), "test",
        episode_id_factory=lambda: "episode-1", monotonic_clock=lambda: 1.0,
        pre_episode_check=lambda: events.append("home"),
        recording_started_hook=c.start_episode, recording_stopping_hook=c.stop_episode,
        metadata_context_provider=c.metadata_context,
    )
    request = EpisodeRequest("task", "fold", tmp_path, spec(tmp_path).station_config, (STREAM,))
    result = runtime.run_once(request)
    assert result.committed and result.outcome.value == outcome
    assert result.mcap_path.parent == tmp_path / directory / "episode-1"
    metadata = json.loads(result.metadata_path.read_text())
    Draft202012Validator(json.loads((ROOT / "schemas/episode-metadata-v1.json").read_text())).validate(metadata)
    assert metadata["collection_type"] == "infer" and "dagger" not in metadata
    if outcome != "aborted":
        assert result.errors == () and not result.session_blocked
    infer = metadata["extensions"]["infer"]
    assert infer["termination_reason"] == reason and infer["label_source"] == source
    assert infer["command_count"] == 1 and infer["recording_completed"] is True
    assert records[0].action == (*ACTION.left, *ACTION.right)
    assert records[0].step_index == 0 and records[0].episode_id == result.episode_id
    assert records[0].monotonic_time_ns == 1_500_000_000
    assert records[0].ros_time_ns == 9_500_000_000
    assert events.index("home") < events.index("recorder_start") < events.index("prepare")
    assert events.index("gate_closed") < events.index("gravity") < events.index("tail") < events.index("recorder_stop")
    with pytest.raises(RuntimeError, match="outside"):
        commands.publish(ACTION)


def test_bootstrap_discards_labels_and_ready_requires_new_right():
    c, commands, gateway, _, _, events, _ = setup_controller()
    raw = FakeTrigger([TriggerEvent.ABORT, TriggerEvent.CONFLICT, True, TriggerEvent.ABORT, False, TriggerEvent.ABORT])
    trigger = InferRecordTrigger(raw, c)
    trigger.arm()
    assert trigger.wait(0) is None  # Idle left.
    assert trigger.wait(0) is None  # Idle conflict.
    assert trigger.wait(0).event is TriggerEvent.ACTIVATE
    c.start_episode(RecordingStarted("episode", 1))
    gateway.ready = False
    trigger.arm()
    assert trigger.wait(0) is None  # Bootstrap left is discarded.
    gateway.ready = True
    assert trigger.wait(0) is None
    assert raw.lifecycle.count("arm") == 3
    assert trigger.wait(0).event is TriggerEvent.TASK_FAIL
    assert not c.active and events.count("gravity") == 1


@pytest.mark.parametrize("cause", ["policy", "monitor", "interrupt", "record_publish"])
def test_faults_and_abort_never_become_normal_human_fail(tmp_path, cause):
    app = InferApplication.build(spec(tmp_path))
    c, commands, gateway, control, _, events, _ = setup_controller(app.configuration)
    def start(started):
        c.start_episode(started)
        if cause == "policy":
            gateway.fault = RuntimeError("policy offline")
        if cause == "record_publish":
            commands.sink = Mock(side_effect=RuntimeError("record lost"))
            try:
                commands.publish(ACTION)
            except RuntimeError as error:
                gateway.fault = error
    raw = FakeTrigger([True, KeyboardInterrupt()] if cause == "interrupt" else [True])
    runtime = EpisodeRuntime(
        EpisodeStore(tmp_path, fail_directory="infer_fail"), InferRecordTrigger(raw, c),
        FakeBackend(), FakeMonitor(METRICS, "sensor missing" if cause == "monitor" else None), "test",
        recording_started_hook=start, recording_stopping_hook=c.stop_episode,
        metadata_context_provider=c.metadata_context,
    )
    request = EpisodeRequest("task", "fold", tmp_path, spec(tmp_path).station_config, (STREAM,))
    result = runtime.run_once(request)
    assert result.errors and result.committed and not c.active
    assert result.outcome is (EpisodeOutcome.ABORTED if cause == "interrupt" else EpisodeOutcome.FAIL)
    infer = json.loads(result.metadata_path.read_text())["extensions"]["infer"]
    assert infer["termination_reason"] == ("operator_abort" if cause == "interrupt" else "runtime_fault")
    assert infer["label_source"] is None
    assert infer["command_count"] == (1 if cause == "record_publish" else 0)
    assert events.index("gravity") < events.index("tail")


@pytest.mark.parametrize("fault", ["recorder", "gravity", "commit"])
def test_finalize_failure_leaves_partial_and_does_not_start_next(tmp_path, fault):
    app = InferApplication.build(spec(tmp_path))
    c, _, gateway, _, human, _, _ = setup_controller(app.configuration)
    if fault == "gravity":
        human.enable_gravity_compensation.side_effect = RuntimeError("gravity failed")
    backend = FakeBackend(RuntimeError("recorder failed") if fault == "recorder" else None)
    store = EpisodeStore(tmp_path, fail_directory="infer_fail")
    if fault == "commit":
        store.commit = Mock(side_effect=OSError("commit failed"))
    runtime = EpisodeRuntime(
        store, InferRecordTrigger(FakeTrigger([True, True]), c), backend, FakeMonitor(METRICS), "test",
        recording_started_hook=c.start_episode, recording_stopping_hook=c.stop_episode,
        metadata_context_provider=c.metadata_context,
    )
    with pytest.raises((RuntimeError, OSError), match=fault):
        runtime.run_once(EpisodeRequest("task", "fold", tmp_path, spec(tmp_path).station_config, (STREAM,)))
    assert len(store.list_partials()) == 1 and not gateway.action_output_enabled
    assert backend.stop_count == 1


def test_several_episodes_need_new_start_and_reset_step_indices(tmp_path):
    app = InferApplication.build(spec(tmp_path))
    c, commands, gateway, _, _, events, records = setup_controller(app.configuration)
    class Raw(FakeTrigger):
        def wait(self, timeout_s):
            signal = super().wait(timeout_s)
            if c.active:
                commands.publish(ACTION)
            return signal
    raw = Raw([True, TriggerEvent.ABORT, TriggerEvent.ABORT, True, True])
    ids = iter(["one", "two"])
    runtime = EpisodeRuntime(
        EpisodeStore(tmp_path, fail_directory="infer_fail"), InferRecordTrigger(raw, c),
        FakeBackend(), FakeMonitor(METRICS), "test", episode_id_factory=lambda: next(ids),
        pre_episode_check=lambda: events.append("home"),
        recording_started_hook=c.start_episode, recording_stopping_hook=c.stop_episode,
        metadata_context_provider=c.metadata_context,
    )
    request = EpisodeRequest("task", "fold", tmp_path, spec(tmp_path).station_config, (STREAM,))
    assert run_episode_loop(runtime, request, 2, io.StringIO(), io.StringIO()) == 0
    assert events.count("home") == 2 and events.count("prepare") == 2
    assert [(r.episode_id, r.step_index) for r in records] == [("one", 0), ("two", 0)]
    assert (tmp_path / "infer_fail/one/metadata.json").exists()
    assert (tmp_path / "two/metadata.json").exists()


def test_failed_send_has_no_record_and_record_sink_failure_keeps_send_count():
    c, commands, _, control, _, _, records = setup_controller()
    commands.start("one")
    control.publish.side_effect = RuntimeError("send failed")
    with pytest.raises(RuntimeError, match="send failed"):
        commands.publish(ACTION)
    assert commands.command_count == 0 and not records
    control.publish.side_effect = None
    commands.sink = Mock(side_effect=RuntimeError("record failed"))
    with pytest.raises(RuntimeError, match="record failed"):
        commands.publish(ACTION)
    assert commands.command_count == 1


def test_ros_message_carries_send_time_not_later_publish_time():
    publisher = RosCommandPublisher()
    publisher._publisher = Mock()
    publisher._message_type = lambda: SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace()))
    publisher(CommandRecord("ep", 5, 42, 2_000_000_007, (*ACTION.left, *ACTION.right)))
    message = publisher._publisher.publish.call_args.args[0]
    assert (message.header.stamp.sec, message.header.stamp.nanosec) == (2, 7)
    assert message.monotonic_time_ns == 42 and message.step_index == 5
    assert message.action == list((*ACTION.left, *ACTION.right))


def test_infer_context_does_not_admit_dagger_or_miss_extension():
    with pytest.raises(ValueError, match="infer metadata"):
        MetadataContext(CollectionType.INFER)
    with pytest.raises(ValueError, match="infer metadata"):
        MetadataContext(CollectionType.DEMONSTRATION, infer={})


def test_startup_failure_closes_gate_and_restores_gravity():
    c, commands, gateway, _, human, _, _ = setup_controller()
    gateway.prepare_policy = Mock(side_effect=RuntimeError("bootstrap submit failed"))
    with pytest.raises(RuntimeError, match="bootstrap submit failed"):
        c.start_episode(RecordingStarted("ep", 0))
    assert not c.active and commands.episode_id is None
    human.enable_gravity_compensation.assert_called_once()


def test_fault_between_label_poll_and_gate_close_is_not_accepted():
    c, commands, gateway, _, human, _, _ = setup_controller()
    c.start_episode(RecordingStarted("ep", 0))
    assert c.poll()
    original_close = gateway.close_gate
    def concurrent_failure(epoch):
        commands.failure = RuntimeError("late command record failure")
        original_close(epoch)
    gateway.close_gate = concurrent_failure
    with pytest.raises(RuntimeError, match="late command record failure"):
        c.label(TriggerSignal(TriggerEvent.ACTIVATE, 10))
    assert not c.active
    human.enable_gravity_compensation.assert_called_once()


def test_clear_pending_failure_still_attempts_gravity_and_prevents_commit():
    c, commands, gateway, _, human, _, _ = setup_controller()
    c.start_episode(RecordingStarted("ep", 0))
    gateway.clear_pending = Mock(side_effect=RuntimeError("clear failed"))
    with pytest.raises(RuntimeError, match="clear failed"):
        c.label(TriggerSignal(TriggerEvent.ABORT, 10))
    human.enable_gravity_compensation.assert_called_once()
    assert commands.episode_id is None
    with pytest.raises(RuntimeError, match="clear failed"):
        c.stop_episode(RecordingStopping(EpisodeOutcome.FAIL, 10))
