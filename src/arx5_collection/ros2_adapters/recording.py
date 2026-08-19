from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any, Callable
from uuid import uuid4

from arx5_collection.episode.models import StreamMetrics, StreamSpec

from .mcap_metrics import audit_mcap


RecorderFactory = Callable[[Path, tuple[str, ...], str], Any]
ReadinessProbe = Callable[[tuple[str, ...], str, float], None]


def _make_recorder(
    output_uri: Path,
    topics: tuple[str, ...],
    node_name: str,
) -> Any:
    import rosbag2_py

    storage_options = rosbag2_py.StorageOptions(
        uri=str(output_uri),
        storage_id="mcap",
        max_bagfile_size=0,
        max_bagfile_duration=0,
    )
    record_options = rosbag2_py.RecordOptions()
    record_options.all_topics = False
    record_options.all_services = False
    record_options.topics = list(topics)
    record_options.disable_keyboard_controls = True
    record_options.include_hidden_topics = False
    record_options.include_unpublished_topics = False
    record_options.compression_mode = ""
    record_options.compression_format = ""
    return rosbag2_py.Recorder(
        storage_options,
        record_options,
        "info",
        node_name,
    )


def _wait_for_recorder_subscriptions(
    topics: tuple[str, ...],
    recorder_node_name: str,
    timeout_s: float,
) -> None:
    import rclpy
    from rclpy.executors import SingleThreadedExecutor

    if timeout_s <= 0:
        raise TimeoutError("rosbag2 recorder subscription readiness timed out")

    context = rclpy.Context()
    node = None
    executor = None
    try:
        rclpy.init(context=context)
        node = rclpy.create_node(
            f"episode_recorder_probe_{uuid4().hex[:8]}",
            context=context,
        )
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        deadline = monotonic() + timeout_s
        pending = set(topics)
        while pending and monotonic() < deadline:
            executor.spin_once(timeout_sec=min(0.05, max(0.0, deadline - monotonic())))
            pending = {
                topic
                for topic in pending
                if not any(
                    endpoint.node_name == recorder_node_name
                    for endpoint in node.get_subscriptions_info_by_topic(topic)
                )
            }
        if pending:
            missing = ", ".join(sorted(pending))
            raise TimeoutError(
                f"rosbag2 recorder did not subscribe to requested topics: {missing}"
            )
    finally:
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        if context.ok():
            context.shutdown()


class RosbagRecordingBackend:
    def __init__(
        self,
        recorder_factory: RecorderFactory | None = None,
        readiness_probe: ReadinessProbe | None = None,
        start_timeout_s: float = 5.0,
        stop_timeout_s: float = 15.0,
        warning_ratio: float = 0.9,
        additional_topics: tuple[str, ...] = (),
    ) -> None:
        if start_timeout_s <= 0 or stop_timeout_s <= 0:
            raise ValueError("recorder timeouts must be positive")
        if not 0 < warning_ratio <= 1:
            raise ValueError("warning_ratio must be in (0, 1]")
        if any(not topic for topic in additional_topics):
            raise ValueError("additional recording topics must not be empty")
        if len(additional_topics) != len(set(additional_topics)):
            raise ValueError("additional recording topics must be unique")
        self.recorder_factory = recorder_factory or _make_recorder
        self.readiness_probe = (
            readiness_probe
            if readiness_probe is not None
            else (_wait_for_recorder_subscriptions if recorder_factory is None else None)
        )
        self.start_timeout_s = start_timeout_s
        self.stop_timeout_s = stop_timeout_s
        self.warning_ratio = warning_ratio
        self.additional_topics = additional_topics
        self._recorder: Any | None = None
        self._thread: Thread | None = None
        self._thread_error: BaseException | None = None
        self._spin_started = Event()
        self._record_entered = Event()
        self._worker_release = Event()
        self._target_path: Path | None = None
        self._temporary_uri: Path | None = None
        self._streams: tuple[StreamSpec, ...] = ()
        self._last_completed_path: Path | None = None

    def start(self, mcap_path: Path, streams: tuple[StreamSpec, ...]) -> None:
        if self._recorder is not None:
            raise RuntimeError("recording is already active")
        if not streams:
            raise ValueError("at least one stream is required")
        if mcap_path.suffix != ".mcap":
            raise ValueError("recording target must end with .mcap")
        if not mcap_path.parent.is_dir():
            raise FileNotFoundError(mcap_path.parent)

        temporary_uri = mcap_path.parent / f".{mcap_path.name}.rosbag2"
        if mcap_path.exists() or temporary_uri.exists():
            raise FileExistsError(mcap_path if mcap_path.exists() else temporary_uri)

        stream_topics = tuple(stream.topic for stream in streams)
        overlap = set(stream_topics).intersection(self.additional_topics)
        if overlap:
            raise ValueError(
                "additional recording topics duplicate monitored streams: "
                + ", ".join(sorted(overlap))
            )
        topics = stream_topics + self.additional_topics
        recorder_node_name = f"episode_recorder_{uuid4().hex[:8]}"
        self._recorder = self.recorder_factory(
            temporary_uri,
            topics,
            recorder_node_name,
        )
        self._target_path = mcap_path
        self._temporary_uri = temporary_uri
        self._streams = streams
        self._last_completed_path = None
        self._thread_error = None
        self._spin_started.clear()
        self._record_entered.clear()
        self._worker_release.clear()
        self._thread = Thread(
            target=self._record,
            name="rosbag2-recorder",
            daemon=False,
        )
        self._thread.start()

        deadline = monotonic() + self.start_timeout_s
        while monotonic() < deadline:
            if self._thread_error is not None:
                error = self._thread_error
                try:
                    self._stop_recorder_thread()
                finally:
                    if self._thread is not None and not self._thread.is_alive():
                        self._clear_active()
                raise RuntimeError("rosbag2 recorder failed during start") from error
            if temporary_uri.is_dir() and self._record_entered.is_set():
                try:
                    if self.readiness_probe is not None:
                        self.readiness_probe(
                            topics,
                            recorder_node_name,
                            max(0.0, deadline - monotonic()),
                        )
                except BaseException:
                    try:
                        self._stop_recorder_thread()
                    finally:
                        if self._thread is not None and not self._thread.is_alive():
                            self._clear_active()
                    raise
                return
            if self._thread is not None and not self._thread.is_alive():
                self._clear_active()
                raise RuntimeError("rosbag2 recorder exited during start")
            sleep(0.01)

        try:
            self._stop_recorder_thread()
        finally:
            if self._thread is not None and not self._thread.is_alive():
                self._clear_active()
        raise TimeoutError("rosbag2 recorder did not create its output directory")

    def _record(self) -> None:
        try:
            assert self._recorder is not None
            self._recorder.start_spin()
            self._spin_started.set()
            self._recorder.record()
            self._record_entered.set()
            self._worker_release.wait()
        except BaseException as error:
            self._thread_error = error
            self._record_entered.set()

    def stop(self) -> None:
        if self._recorder is None or self._thread is None:
            raise RuntimeError("recording is not active")
        target_path = self._target_path
        temporary_uri = self._temporary_uri
        assert target_path is not None and temporary_uri is not None

        try:
            self._stop_recorder_thread()
            if self._thread_error is not None:
                raise RuntimeError("rosbag2 recorder failed") from self._thread_error
            self._finalize_single_mcap(temporary_uri, target_path)
            self._last_completed_path = target_path
        finally:
            if self._thread is not None and not self._thread.is_alive():
                self._clear_active()

    def _stop_recorder_thread(self) -> None:
        assert self._recorder is not None and self._thread is not None
        stop_errors: list[BaseException] = []
        if self._record_entered.is_set() and self._thread_error is None:
            try:
                self._recorder.stop()
            except BaseException as error:
                stop_errors.append(error)
        if self._spin_started.is_set():
            try:
                self._recorder.stop_spin()
            except BaseException as error:
                stop_errors.append(error)
        self._worker_release.set()
        self._thread.join(self.stop_timeout_s)
        if self._thread.is_alive():
            raise TimeoutError("rosbag2 recorder thread did not stop")
        if stop_errors:
            raise RuntimeError("rosbag2 recorder stop failed") from stop_errors[0]

    @staticmethod
    def _finalize_single_mcap(temporary_uri: Path, target_path: Path) -> None:
        if not temporary_uri.is_dir():
            raise RuntimeError("rosbag2 output directory is missing")
        entries = tuple(temporary_uri.iterdir())
        mcap_files = tuple(path for path in entries if path.suffix == ".mcap")
        metadata_path = temporary_uri / "metadata.yaml"
        expected = set(mcap_files) | {metadata_path}
        if len(mcap_files) != 1 or not metadata_path.is_file() or set(entries) != expected:
            raise RuntimeError(
                "rosbag2 output must contain exactly one MCAP and metadata.yaml"
            )
        if mcap_files[0].stat().st_size <= 0:
            raise RuntimeError("rosbag2 produced an empty MCAP")
        if target_path.exists():
            raise FileExistsError(target_path)

        metadata_path.unlink()
        mcap_files[0].replace(target_path)
        temporary_uri.rmdir()

    def metrics(self, streams: tuple[StreamSpec, ...]) -> tuple[StreamMetrics, ...]:
        if self._last_completed_path is None:
            raise RuntimeError("no completed MCAP is available")
        if streams != self._streams:
            raise ValueError("stream contract differs from the completed recording")
        return audit_mcap(
            self._last_completed_path,
            streams,
            warning_ratio=self.warning_ratio,
        )

    def _clear_active(self) -> None:
        self._recorder = None
        self._thread = None
        self._target_path = None
        self._temporary_uri = None
