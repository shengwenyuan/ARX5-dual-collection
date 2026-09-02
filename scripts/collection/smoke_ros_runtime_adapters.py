#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

from arx5_collection.collection.episode.models import StreamSpec
from arx5_collection.adapters.ros2 import RosStreamMonitor, RosbagRecordingBackend
from arx5_collection.common.specs import RUNTIME_INTERFACE_SPEC


STREAM = StreamSpec(
    id="smoke_arm_state",
    topic="/smoke/arm_state",
    required=True,
    expected_hz=50.0,
)


class FakeRosSource:
    def __init__(self) -> None:
        self.stop_event = Event()
        self.status_enabled = Event()
        self.status_enabled.set()
        self.ready = Event()
        self.thread = Thread(target=self._run, name="fake-ros-source", daemon=False)
        self.error: BaseException | None = None

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(5.0):
            raise TimeoutError("fake ROS source did not start")

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(5.0)
        if self.thread.is_alive():
            raise TimeoutError("fake ROS source did not stop")
        if self.error is not None:
            raise RuntimeError("fake ROS source failed") from self.error

    def pause_status(self) -> None:
        self.status_enabled.clear()

    def _run(self) -> None:
        context = None
        node = None
        executor = None
        try:
            import rclpy
            from arx5_collection_interfaces.msg import ArmState, StreamStatus
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.qos import (
                QoSDurabilityPolicy,
                QoSHistoryPolicy,
                QoSProfile,
                QoSReliabilityPolicy,
            )

            context = rclpy.Context()
            rclpy.init(context=context)
            node = rclpy.create_node("fake_episode_source", context=context)
            executor = SingleThreadedExecutor(context=context)
            executor.add_node(node)
            qos = QoSProfile(
                history=QoSHistoryPolicy.KEEP_LAST,
                depth=64,
                reliability=QoSReliabilityPolicy.RELIABLE,
                durability=QoSDurabilityPolicy.VOLATILE,
            )
            data_publisher = node.create_publisher(ArmState, STREAM.topic, qos)
            status_publisher = node.create_publisher(
                StreamStatus,
                RUNTIME_INTERFACE_SPEC.stream_status_topic,
                qos,
            )
            total_count = 0
            window_count = 0
            last_stamp = None
            next_data_s = monotonic()
            next_status_s = next_data_s + 0.5
            self.ready.set()

            while not self.stop_event.is_set():
                now_s = monotonic()
                if now_s >= next_data_s:
                    stamp = node.get_clock().now().to_msg()
                    message = ArmState()
                    message.header.stamp = stamp
                    data_publisher.publish(message)
                    total_count += 1
                    window_count += 1
                    last_stamp = stamp
                    next_data_s += 0.02
                if now_s >= next_status_s:
                    if self.status_enabled.is_set():
                        status = StreamStatus()
                        status.header.stamp = node.get_clock().now().to_msg()
                        status.stream_id = STREAM.id
                        status.topic = STREAM.topic
                        status.total_count = total_count
                        status.window_count = window_count
                        status.window_duration_s = window_count / STREAM.expected_hz
                        status.observed_hz = STREAM.expected_hz
                        status.max_gap_ms = 20.0
                        if last_stamp is not None:
                            status.last_message_stamp = last_stamp
                        status.silence_s = 0.0
                        status.non_monotonic_count = 0
                        status_publisher.publish(status)
                    window_count = 0
                    next_status_s += 0.5
                executor.spin_once(timeout_sec=0.0)
                sleep(0.001)
        except BaseException as error:
            self.error = error
            self.ready.set()
        finally:
            if executor is not None:
                executor.shutdown()
            if node is not None:
                node.destroy_node()
            if context is not None and context.ok():
                context.shutdown()


def run(
    output_root: Path,
    episodes: int,
    duration_s: float,
    test_heartbeat_failure: bool,
) -> dict[str, object]:
    if episodes <= 0 or duration_s <= 0:
        raise ValueError("episodes and duration must be positive")
    output_root.mkdir(parents=True, exist_ok=False)
    source = FakeRosSource()
    source.start()
    sleep(1.0)
    results = []
    heartbeat_result = None
    try:
        for index in range(episodes):
            episode_dir = output_root / f"episode-{index:02d}"
            episode_dir.mkdir()
            target = episode_dir / "episode.mcap"
            backend = RosbagRecordingBackend()
            monitor = RosStreamMonitor(backend, status_sink=None)
            backend.start(target, (STREAM,))
            monitor.start((STREAM,))
            deadline = monotonic() + duration_s
            while monotonic() < deadline:
                failure = monitor.required_failure()
                if failure is not None:
                    raise RuntimeError(failure)
                sleep(0.05)
            backend.stop()
            metrics = monitor.stop()
            if set(episode_dir.iterdir()) != {target}:
                raise RuntimeError("smoke episode did not converge to one MCAP")
            if metrics[0].count <= 0 or metrics[0].warnings:
                raise RuntimeError(
                    "recorded stream failed metrics audit: "
                    f"count={metrics[0].count}, warnings={metrics[0].warnings}"
                )
            results.append(
                {
                    "episode": index,
                    "mcap": str(target),
                    "bytes": target.stat().st_size,
                    "count": metrics[0].count,
                    "observed_hz": metrics[0].observed_hz,
                    "max_gap_ms": metrics[0].max_gap_ms,
                    "warnings": metrics[0].warnings,
                }
            )
        if test_heartbeat_failure:
            failure_dir = output_root / "heartbeat-failure"
            failure_dir.mkdir()
            target = failure_dir / "episode.mcap"
            backend = RosbagRecordingBackend()
            monitor = RosStreamMonitor(backend, status_sink=None)
            backend.start(target, (STREAM,))
            monitor.start((STREAM,))
            sleep(1.2)
            source.pause_status()
            paused_s = monotonic()
            failure = None
            while monotonic() - paused_s < 3.5:
                failure = monitor.required_failure()
                if failure is not None:
                    break
                sleep(0.05)
            if failure != "required stream smoke_arm_state telemetry stopped":
                raise RuntimeError(f"unexpected heartbeat failure result: {failure}")
            latency_s = monotonic() - paused_s
            backend.stop()
            metrics = monitor.stop()
            if set(failure_dir.iterdir()) != {target}:
                raise RuntimeError("heartbeat failure did not converge to one MCAP")
            heartbeat_result = {
                "failure": failure,
                "detection_latency_s": latency_s,
                "mcap": str(target),
                "count": metrics[0].count,
                "observed_hz": metrics[0].observed_hz,
            }
    finally:
        source.stop()
    return {"episodes": results, "heartbeat_failure": heartbeat_result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--episodes", type=int, default=10)
    parser.add_argument("--duration", type=float, default=0.5)
    parser.add_argument("--skip-heartbeat-failure", action="store_true")
    args = parser.parse_args()
    report = run(
        args.output_root,
        args.episodes,
        args.duration,
        not args.skip_heartbeat_failure,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
