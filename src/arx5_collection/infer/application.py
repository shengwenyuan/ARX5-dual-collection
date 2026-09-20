from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import math
import sys
from typing import TextIO

from arx5_collection.capture import CaptureProfile, metadata_extensions
from arx5_collection.dagger.action_runtime import open_takeover_action_runtime
from arx5_collection.dagger.application import DaggerRunSpec, DaggerSessionBuilder
from arx5_collection.dagger.authority_ros import ACTION_OUTPUT_TOPICS
from arx5_collection.dagger.command_ros import RosDualArmControlPort
from arx5_collection.dagger.config import DaggerCollectorSettings
from arx5_collection.dagger.local_snapshot import LocalVlaSnapshotClient
from arx5_collection.dagger.observation import Pi05ObservationEncoder
from arx5_collection.dagger.openpi_transport import OpenPiDaggerTransport
from arx5_collection.dagger.policy_client import AsyncPi05PolicyClient
from arx5_collection.episode.adapters.pedal import PedalDeviceResolver
from arx5_collection.episode.cli import load_request, run_episode_loop
from arx5_collection.episode.models import EpisodeRequest
from arx5_collection.episode.ports import TriggerEvent
from arx5_collection.production.config import validate_task_streams
from arx5_collection.production.lifecycle import termination_as_interrupt
from arx5_collection.production.orchestrator import ProductionSession
from arx5_collection.production.triggers import open_configured_pedals

from .controller import InferController, InferRecordTrigger
from .recording import COMMAND_TOPIC, RecordedControl, RosCommandPublisher


def recording_configuration(
    spec: DaggerRunSpec, settings: DaggerCollectorSettings
) -> dict[str, object]:
    return {
        "checkpoint_sha256": settings.checkpoint_sha256,
        "policy_config_sha256": sha256(spec.policy_config.read_bytes()).hexdigest(),
        "station_config_sha256": sha256(spec.station_config.read_bytes()).hexdigest(),
        "task_config_sha256": sha256(spec.task_config.read_bytes()).hexdigest(),
        "software_version": spec.software_version,
        "session_id": spec.session_id,
        "prompt": settings.prompt,
        "checkpoint_profile": asdict(settings.checkpoint_profile),
        "rtc_rollout": asdict(settings.rtc_rollout),
        "control": asdict(settings.control),
        "gripper_contract": settings.gripper_contract,
        "gripper_calibration": asdict(settings.grippers),
        "gripper_action_offset": settings.gripper_action_offset,
        "normalization_identity": "checkpoint_sha256 (server checkpoint assets)",
        "action_semantics": "absolute_vendor_target",
        "action_order": [
            f"{side}_{joint}"
            for side in ("left", "right")
            for joint in ("j1", "j2", "j3", "j4", "j5", "j6", "gripper")
        ],
        "action_units": ["rad"] * 6 + ["vendor_raw"] + ["rad"] * 6 + ["vendor_raw"],
        "command_topic": COMMAND_TOPIC,
        "command_stamp": "host ROS system clock immediately before paired publish",
        "monotonic_stamp": "host monotonic clock; paired with command header.stamp",
    }


@dataclass
class InferApplication:
    spec: DaggerRunSpec
    settings: DaggerCollectorSettings
    request: EpisodeRequest
    capture_profile: CaptureProfile
    session: ProductionSession
    configuration: dict[str, object]
    post_stop_recording_s: float = 0.1
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr

    @classmethod
    def build(
        cls, spec: DaggerRunSpec, post_stop_recording_s: float = 0.1
    ) -> InferApplication:
        if not math.isfinite(post_stop_recording_s) or post_stop_recording_s <= 0:
            raise ValueError("post-stop recording time must be finite and positive")
        settings = DaggerCollectorSettings.load(spec.policy_config)
        if settings.checkpoint_profile.policy_type != "training_time_rtc":
            raise ValueError("infer stage A requires a training_time_rtc checkpoint")
        capture = validate_task_streams(spec.task_config)
        request = load_request(
            spec.task_config, spec.output_root, spec.station_config,
            task_description=spec.task_description,
        )
        configuration = recording_configuration(spec, settings)
        session = DaggerSessionBuilder().build(
            spec, settings, request,
            additional_recording_topics=(COMMAND_TOPIC,), fail_directory="infer_fail",
        )
        return cls(
            spec, settings, request, capture, session, configuration,
            post_stop_recording_s,
        )

    def run(self) -> int:
        settings = self.settings
        snapshot = self.session.camera_snapshot
        if snapshot is None:
            raise RuntimeError("infer requires the existing Snapshot data plane")
        status = lambda message: print(message, file=self.stderr, flush=True)
        services = tuple(
            f"/{name}/enable_policy_control"
            for name in (
                settings.arm_profile.left_controller_name,
                settings.arm_profile.right_controller_name,
            )
        )
        # Resolve both pedals before starting hardware; infer has no keyboard fallback.
        with termination_as_interrupt(), open_configured_pedals(
            self.session.station, PedalDeviceResolver(), conflict_event=TriggerEvent.CONFLICT
        ) as pedals, self.session, RosDualArmControlPort(
            ACTION_OUTPUT_TOPICS, policy_enable_services=services,
            allow_vendor_commands=True, state_timeout_s=settings.control.state_timeout_s,
        ) as control, OpenPiDaggerTransport(
            settings.server_host, settings.server_port, settings.checkpoint_sha256,
            settings.inference_timeout_s, settings.checkpoint_profile,
        ) as transport, LocalVlaSnapshotClient(
            timeout_s=settings.snapshot_timeout_s, socket_path=snapshot.socket_path,
            arena_path=snapshot.arena_path, width=settings.checkpoint_profile.input.width,
            height=settings.checkpoint_profile.input.height,
        ) as observations, RosCommandPublisher() as publisher:
            self.session.backend.recording_publisher = publisher
            policy = AsyncPi05PolicyClient(
                session_id=self.spec.session_id, prompt=settings.prompt,
                checkpoint_sha256=settings.checkpoint_sha256, observations=observations,
                encoder=Pi05ObservationEncoder(settings.grippers), transport=transport,
                execution=settings.execution,
            )
            commands = RecordedControl(control, publisher, ros_clock=publisher.now_ns)
            try:
                with open_takeover_action_runtime(
                    settings, policy, commands, self.spec.log_dir
                ) as actions:
                    controller = InferController(
                        actions.gateway, self.session.home_controller, commands, self.configuration,
                        self.post_stop_recording_s, status_sink=status,
                    )
                    try:
                        actions.executor.start()
                        runtime = self.session.create_runtime(
                            self.request, InferRecordTrigger(pedals, controller),
                            metadata_context_provider=controller.metadata_context,
                            recording_started_hook=controller.start_episode,
                            recording_stopping_hook=controller.stop_episode,
                            metadata_extensions=metadata_extensions(self.capture_profile),
                        )
                        return run_episode_loop(
                            runtime, self.request, episodes=self.spec.episodes,
                            stdout=self.stdout, stderr=self.stderr,
                            ready_message="READY: new right pedal=HOME/start; left ignored; Ctrl+C=exit",
                            recording_message="RECORDING / RTC bootstrap: wait for INFER RUNNING",
                        )
                    finally:
                        try:
                            controller.close()
                        finally:
                            actions.executor.close()
            finally:
                policy.close()
