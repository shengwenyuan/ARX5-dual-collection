from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from arx5_collection.infer.recording import RosCommandPublisher
from arx5_collection.dagger.authority_ros import RosAuthorityEventPublisher
from arx5_collection.ros2_adapters.recording import RosbagRecordingBackend
from tests.test_recording_backend import FakeRecorder, STREAMS


@pytest.fixture
def ros(monkeypatch):
    context = Mock()
    context.ok.return_value = True
    node = Mock()
    node.get_parameter.return_value.value = False
    node.destroy_publisher.return_value = True
    writers = [Mock(), Mock()]
    node.create_publisher.side_effect = writers
    module = SimpleNamespace(Context=lambda: context, init=Mock(), create_node=Mock(return_value=node))
    monkeypatch.setitem(sys.modules, "rclpy", module)
    monkeypatch.setitem(sys.modules, "rclpy.qos", SimpleNamespace(
        QoSProfile=lambda **kwargs: SimpleNamespace(**kwargs),
        ReliabilityPolicy=SimpleNamespace(RELIABLE="reliable"),
    ))
    monkeypatch.setitem(sys.modules, "arx5_collection_interfaces", SimpleNamespace(
        msg=SimpleNamespace(InferCommand=object, AuthorityEvent=object),
    ))
    return context, node, writers


@pytest.mark.parametrize("publisher_type,depth", [(RosCommandPublisher, 256), (RosAuthorityEventPublisher, 32)])
def test_each_episode_has_new_writer_but_keeps_node_context_and_qos(ros, publisher_type, depth):
    context, node, writers = ros
    with publisher_type() as publisher:
        assert publisher._publisher is None
        for writer in writers:
            publisher.start_episode()
            assert publisher._publisher is writer
            assert publisher._context is context and publisher._node is node
            with pytest.raises(RuntimeError, match="already active"):
                publisher.start_episode()
            publisher.stop_episode()
            publisher.stop_episode()  # Startup/exit cleanup can converge here.
            assert publisher._publisher is None
        assert node.destroy_node.call_count == 0
    assert node.destroy_publisher.call_count == 2
    node.destroy_node.assert_called_once()
    context.shutdown.assert_called_once()
    qos = node.create_publisher.call_args.args[2]
    assert qos.depth == depth and qos.reliability == "reliable"


@pytest.mark.parametrize("readers,count", [(["old_recorder"], 1), (["current_recorder"], 0)])
def test_match_requires_current_recorder_and_rmw_match(ros, readers, count):
    _, node, writers = ros
    node.get_subscriptions_info_by_topic.return_value = [SimpleNamespace(node_name=name) for name in readers]
    writers[0].get_subscription_count.return_value = count
    with RosCommandPublisher() as publisher:
        publisher.start_episode()
        with pytest.raises(TimeoutError, match="did not match"):
            publisher.wait_for_recorder("current_recorder", 0.001)
        node.get_subscriptions_info_by_topic.return_value = [SimpleNamespace(node_name="current_recorder")]
        writers[0].get_subscription_count.return_value = 1
        publisher.wait_for_recorder("current_recorder", 0.1)


def test_initialization_failure_cleans_context_and_node(ros):
    context, node, _ = ros
    node.get_parameter.return_value.value = True
    with pytest.raises(RuntimeError, match="host system clock"):
        with RosCommandPublisher():
            pytest.fail("must reject simulated time")
    node.destroy_node.assert_called_once()
    context.shutdown.assert_called_once()


class LifecyclePublisher:
    def __init__(self, events):
        self.events = events
        self.active = False

    def start_episode(self):
        assert not self.active
        self.events.append("writer_start")
        self.active = True

    def wait_for_recorder(self, name, timeout):
        assert self.active and timeout > 0 and name.startswith("episode_recorder_")
        self.events.append("matched")

    def stop_episode(self):
        if self.active:
            self.events.append("writer_stop")
        self.active = False


def test_recorder_owns_writer_boundaries_across_episodes(tmp_path):
    events = []
    publisher = LifecyclePublisher(events)

    class Recorder(FakeRecorder):
        def record(self):
            assert publisher.active
            events.append("record")
            super().record()

        def stop(self):
            assert publisher.active
            events.append("recorder_stop")
            super().stop()

    backend = RosbagRecordingBackend(
        recorder_factory=lambda uri, topics, name: Recorder(uri, topics),
        readiness_probe=lambda *args: events.append("graph_ready"),
        recording_publisher=publisher,
    )
    for index in range(2):
        backend.start(tmp_path / f"episode-{index}.mcap", STREAMS)
        assert publisher.active and events[-1] == "matched"
        backend.stop()
        assert not publisher.active
    assert events == ["writer_start", "record", "graph_ready", "matched", "recorder_stop", "writer_stop"] * 2


@pytest.mark.parametrize("phase", ["factory", "record", "graph", "match", "stop", "finalize"])
def test_recorder_failure_releases_writer_and_next_episode_can_start(tmp_path, phase):
    events = []
    publisher = LifecyclePublisher(events)

    def fail(*args):
        raise RuntimeError("injected failure")

    def factory(uri, topics, name):
        recorder = FakeRecorder(uri, topics, extra_file=phase == "finalize")
        if phase == "record": recorder.record = fail
        if phase == "stop": recorder.stop = fail
        return recorder

    backend = RosbagRecordingBackend(
        recorder_factory=fail if phase == "factory" else factory,
        readiness_probe=fail if phase == "graph" else lambda *args: None,
        recording_publisher=publisher,
    )
    if phase == "match": publisher.wait_for_recorder = fail
    with pytest.raises(RuntimeError):
        backend.start(tmp_path / "broken.mcap", STREAMS)
        backend.stop()
    assert not publisher.active and backend._recorder is None
    backend.recorder_factory = lambda uri, topics, name: FakeRecorder(uri, topics)
    backend.readiness_probe = lambda *args: None
    publisher.wait_for_recorder = lambda *args: None
    backend.start(tmp_path / "next.mcap", STREAMS)
    backend.stop()
    assert (tmp_path / "next.mcap").is_file() and not publisher.active


@pytest.mark.parametrize("fault", ["monitor_start", "start_hook", "stop_hook", "interrupt"])
def test_runtime_failure_paths_release_episode_publisher(tmp_path, fault):
    from arx5_collection.episode.models import EpisodeRequest, StreamMetrics
    from arx5_collection.episode.runtime import EpisodeRuntime
    from arx5_collection.episode.store import EpisodeStore
    from tests.episode.fakes import FakeMonitor, FakeTrigger
    from tests.infer.test_collection import spec

    publisher = LifecyclePublisher([])
    backend = RosbagRecordingBackend(
        recorder_factory=lambda uri, topics, name: FakeRecorder(uri, topics),
        recording_publisher=publisher,
    )
    monitor = FakeMonitor((StreamMetrics(STREAMS[0].id, 1, 0.0, 0.0, 0.0),))
    def fail(*args): raise RuntimeError("injected runtime failure")
    if fault == "monitor_start": monitor.start = fail
    def started(*args):
        assert publisher.active
        if fault == "start_hook": fail()
    def stopping(*args):
        assert publisher.active
        if fault == "stop_hook": fail()
    runtime = EpisodeRuntime(
        EpisodeStore(tmp_path), FakeTrigger([True, KeyboardInterrupt() if fault == "interrupt" else True]),
        backend, monitor, "test", recording_started_hook=started, recording_stopping_hook=stopping,
    )
    request = EpisodeRequest("task", "test", tmp_path, spec(tmp_path).station_config, STREAMS)
    if fault == "interrupt":
        result = runtime.run_once(request)
        assert result.outcome.value == "aborted"
    else:
        with pytest.raises(RuntimeError, match="injected runtime failure"):
            runtime.run_once(request)
    assert not publisher.active and backend._recorder is None
