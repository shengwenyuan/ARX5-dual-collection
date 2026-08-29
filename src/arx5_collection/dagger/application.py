from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from arx5_collection.episode.cli import load_request, run_episode_loop
from arx5_collection.episode.models import EpisodeRequest
from arx5_collection.production.checks import CheckResult
from arx5_collection.production.config import (
    load_configured_station,
    validate_task_streams,
)
from arx5_collection.production.lifecycle import termination_as_interrupt
from arx5_collection.production.orchestrator import GIB, ProductionSession
from arx5_collection.production.processes import CameraSnapshotConfig
from arx5_collection.reset import ResetState
from arx5_collection.ros2_adapters.recording import RosbagRecordingBackend
from arx5_collection.snapshot_shared_memory import (
    snapshot_arena_path,
    snapshot_socket_path,
)

from .config import DaggerCollectorSettings
from .local_snapshot import LocalVlaSnapshotClient
from .observation import Pi05ObservationEncoder
from .openpi_transport import OpenPiDaggerTransport
from .policy_client import AsyncPi05PolicyClient
from .shadow import (
    JsonlShadowLog,
    ShadowEpisodeHooks,
    ShadowInferenceLoop,
    ShadowRecordTrigger,
)
from .triggers import DaggerAutoTriggerFactory
from .topics import DAGGER_RECORDING_TOPICS


@dataclass(frozen=True, slots=True)
class DaggerRunSpec:
    station_config: Path
    task_config: Path
    task_description: str
    policy_config: Path
    output_root: Path
    episodes: int
    min_free_gib: int
    readiness_timeout_s: float
    software_version: str
    session_id: str

    @property
    def log_dir(self) -> Path:
        return self.output_root / "logs" / self.session_id


class DaggerSessionBuilder:
    """Build the hardware Session from a mode-specific DAgger profile."""

    def __init__(
        self,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr

    def build(
        self,
        spec: DaggerRunSpec,
        settings: DaggerCollectorSettings,
        additional_recording_topics: tuple[str, ...] = (),
    ) -> ProductionSession:
        station = load_configured_station(spec.station_config)
        station.task_upload_directory(spec.task_description)
        return ProductionSession(
            station=station,
            output_root=spec.output_root,
            log_dir=spec.log_dir,
            software_version=spec.software_version,
            min_free_bytes=spec.min_free_gib * GIB,
            readiness_timeout_s=spec.readiness_timeout_s,
            arm_profile=settings.arm_profile,
            camera_snapshot=CameraSnapshotConfig(
                max_camera_span_ms=settings.observation.max_camera_span_ns / 1e6,
                max_arm_age_ms=settings.observation.max_arm_age_ns / 1e6,
                max_snapshot_age_ms=settings.observation.max_snapshot_age_ns / 1e6,
                width=settings.checkpoint_profile.input.width,
                height=settings.checkpoint_profile.input.height,
                arena_path=snapshot_arena_path(station.ros_domain_id),
                socket_path=snapshot_socket_path(station.ros_domain_id),
            ),
            backend=(
                RosbagRecordingBackend(
                    additional_topics=additional_recording_topics
                )
                if additional_recording_topics
                else None
            ),
            home_state_sink=self._render_reset_state,
            home_timing_sink=self._render_home_timing,
            check_sink=self._render_check,
            warning_sink=lambda message: print(
                f"WARNING {message}", file=self.stderr
            ),
            fail_directory="dagger_fail",
        )

    def _render_check(self, result: CheckResult) -> None:
        state = "PASS" if result.passed else "FAIL"
        print(
            f"{state} [{result.phase.value}] {result.name}: {result.detail}",
            file=self.stdout,
        )

    def _render_reset_state(self, state: ResetState) -> None:
        message = (
            "RESETTING: moving both arms to Vendor home"
            if state is ResetState.RESETTING
            else "RESET_COMPLETE: gravity compensation restored"
        )
        print(message, file=self.stderr, flush=True)

    def _render_home_timing(self, phase: str, elapsed_s: float) -> None:
        print(
            f"HOME_TIMING {phase}={elapsed_s:.3f}s",
            file=self.stderr,
            flush=True,
        )


class ShadowApplication:
    """Own Shadow-only resources around one long-lived production Session."""

    def __init__(
        self,
        spec: DaggerRunSpec,
        settings: DaggerCollectorSettings,
        request: EpisodeRequest,
        session: ProductionSession,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
    ) -> None:
        self.spec = spec
        self.settings = settings
        self.request = request
        self.session = session
        self.stdout = stdout
        self.stderr = stderr

    def run(self) -> int:
        log_dir = self.spec.log_dir
        snapshot = self.session.camera_snapshot
        if snapshot is None:
            raise RuntimeError("DAgger Session requires a Snapshot data plane")
        status_sink = lambda message: print(
            message, file=self.stderr, flush=True
        )
        with termination_as_interrupt(), OpenPiDaggerTransport(
            self.settings.server_host,
            self.settings.server_port,
            self.settings.checkpoint_sha256,
            self.settings.inference_timeout_s,
            self.settings.checkpoint_profile,
        ) as transport, LocalVlaSnapshotClient(
            timeout_s=self.settings.snapshot_timeout_s,
            socket_path=snapshot.socket_path,
            arena_path=snapshot.arena_path,
            width=self.settings.checkpoint_profile.input.width,
            height=self.settings.checkpoint_profile.input.height,
        ) as observations, JsonlShadowLog(
            log_dir / "dagger-shadow.jsonl"
        ) as shadow_log, self.session:
            policy = AsyncPi05PolicyClient(
                session_id=self.spec.session_id,
                prompt=self.settings.prompt,
                checkpoint_sha256=self.settings.checkpoint_sha256,
                observations=observations,
                encoder=Pi05ObservationEncoder(self.settings.grippers),
                transport=transport,
                execution=self.settings.execution,
            )
            shadow_loop = ShadowInferenceLoop(
                policy,
                period_s=self.settings.execution.inference_period_s,
                attempt_sink=shadow_log,
                status_sink=status_sink,
            )
            hooks = ShadowEpisodeHooks(
                shadow_loop,
                self.settings.checkpoint_sha256,
            )
            print(f"DAGGER SHADOW READY logs={log_dir}", file=self.stdout, flush=True)
            trigger_factory = DaggerAutoTriggerFactory(status_sink=status_sink)
            try:
                with trigger_factory.open(self.session.station) as trigger:
                    runtime = self.session.create_runtime(
                        self.request,
                        ShadowRecordTrigger(trigger, status_sink=status_sink),
                        metadata_context_provider=hooks.metadata_context,
                        recording_started_hook=hooks.recording_started,
                        recording_stopping_hook=hooks.recording_stopping,
                    )
                    return run_episode_loop(
                        runtime,
                        self.request,
                        episodes=self.spec.episodes,
                        stdout=self.stdout,
                        stderr=self.stderr,
                    )
            finally:
                shadow_loop.stop()
                policy.close()


class TakeoverDryRunApplication:
    """Validate Take-over authority and recording without model actions."""

    def __init__(
        self,
        spec: DaggerRunSpec,
        settings: DaggerCollectorSettings,
        request: EpisodeRequest,
        session: ProductionSession,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
    ) -> None:
        self.spec = spec
        self.settings = settings
        self.request = request
        self.session = session
        self.stdout = stdout
        self.stderr = stderr

    def run(self) -> int:
        from .authority_ros import (
            RosAuthorityEventPublisher,
            require_no_action_publishers,
        )
        from .takeover import (
            AuthorityTimeline,
            NoActionGateway,
            TakeoverController,
            TakeoverRecordTrigger,
        )

        log_dir = self.spec.log_dir
        status_sink = lambda message: print(
            message, file=self.stderr, flush=True
        )
        with termination_as_interrupt():
            require_no_action_publishers()
            with RosAuthorityEventPublisher() as events, self.session:
                gateway = NoActionGateway()
                controller = TakeoverController(
                    gateway=gateway,
                    human_mode=self.session.home_controller,
                    timeline=AuthorityTimeline(
                        self.settings.checkpoint_sha256,
                        events,
                    ),
                    status_sink=status_sink,
                )
                trigger_factory = DaggerAutoTriggerFactory(status_sink=status_sink)
                print(
                    f"DAGGER TAKEOVER DRY-RUN READY logs={log_dir}; "
                    "model=disabled action_output=disabled",
                    file=self.stdout,
                    flush=True,
                )
                with trigger_factory.open(self.session.station) as trigger:
                    runtime = self.session.create_runtime(
                        self.request,
                        TakeoverRecordTrigger(trigger, controller, status_sink),
                        metadata_context_provider=controller.metadata_context,
                        recording_started_hook=controller.start_episode,
                        recording_stopping_hook=controller.stop_episode,
                    )
                    return run_episode_loop(
                        runtime,
                        self.request,
                        episodes=self.spec.episodes,
                        stdout=self.stdout,
                        stderr=self.stderr,
                    )


class TakeoverApplication:
    """Run explicit Take-over with the sole validated Vendor command Gateway."""

    def __init__(
        self,
        spec: DaggerRunSpec,
        settings: DaggerCollectorSettings,
        request: EpisodeRequest,
        session: ProductionSession,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
    ) -> None:
        self.spec = spec
        self.settings = settings
        self.request = request
        self.session = session
        self.stdout = stdout
        self.stderr = stderr

    def run(self) -> int:
        from .action_runtime import open_takeover_action_runtime
        from .authority_ros import ACTION_OUTPUT_TOPICS, RosAuthorityEventPublisher
        from .command_ros import RosDualArmControlPort
        from .takeover import (
            AuthorityTimeline,
            TakeoverController,
            TakeoverRecordTrigger,
        )

        log_dir = self.spec.log_dir
        snapshot = self.session.camera_snapshot
        if snapshot is None:
            raise RuntimeError("DAgger Session requires a Snapshot data plane")
        status_sink = lambda message: print(
            message, file=self.stderr, flush=True
        )
        enable_services = tuple(
            f"/{name}/enable_policy_control"
            for name in (
                self.settings.arm_profile.left_controller_name,
                self.settings.arm_profile.right_controller_name,
            )
        )
        with termination_as_interrupt(), self.session, RosDualArmControlPort(
            ACTION_OUTPUT_TOPICS,
            policy_enable_services=enable_services,
            allow_vendor_commands=True,
            state_timeout_s=self.settings.control.state_timeout_s,
        ) as control, OpenPiDaggerTransport(
            self.settings.server_host,
            self.settings.server_port,
            self.settings.checkpoint_sha256,
            self.settings.inference_timeout_s,
            self.settings.checkpoint_profile,
        ) as transport, LocalVlaSnapshotClient(
            timeout_s=self.settings.snapshot_timeout_s,
            socket_path=snapshot.socket_path,
            arena_path=snapshot.arena_path,
            width=self.settings.checkpoint_profile.input.width,
            height=self.settings.checkpoint_profile.input.height,
        ) as observations, RosAuthorityEventPublisher() as events:
            policy = AsyncPi05PolicyClient(
                session_id=self.spec.session_id,
                prompt=self.settings.prompt,
                checkpoint_sha256=self.settings.checkpoint_sha256,
                observations=observations,
                encoder=Pi05ObservationEncoder(self.settings.grippers),
                transport=transport,
                execution=self.settings.execution,
            )
            def run_with_gateway(gateway, executor) -> int:
                controller = TakeoverController(
                    gateway=gateway,
                    human_mode=self.session.home_controller,
                    timeline=AuthorityTimeline(
                        self.settings.checkpoint_sha256,
                        events,
                    ),
                    status_sink=status_sink,
                )
                trigger_factory = DaggerAutoTriggerFactory(status_sink=status_sink)
                executor.start()
                print(
                    f"DAGGER TAKEOVER READY logs={log_dir}; "
                    f"policy={self.settings.checkpoint_profile.policy_type} "
                    f"horizon={self.settings.execution.action_chunk_size}@"
                    f"{self.settings.execution.control_rate_hz:g}Hz",
                    file=self.stdout,
                    flush=True,
                )
                try:
                    with trigger_factory.open(self.session.station) as trigger:
                        runtime = self.session.create_runtime(
                            self.request,
                            TakeoverRecordTrigger(trigger, controller, status_sink),
                            metadata_context_provider=controller.metadata_context,
                            recording_started_hook=controller.start_episode,
                            recording_stopping_hook=controller.stop_episode,
                        )
                        return run_episode_loop(
                            runtime,
                            self.request,
                            episodes=self.spec.episodes,
                            stdout=self.stdout,
                            stderr=self.stderr,
                        )
                finally:
                    executor.close()

            try:
                with open_takeover_action_runtime(
                    self.settings,
                    policy,
                    control,
                    log_dir,
                ) as action_runtime:
                    return run_with_gateway(
                        action_runtime.gateway,
                        action_runtime.executor,
                    )
            finally:
                policy.close()


class DaggerApplicationBuilder:
    """Validate inputs once and compose a DAgger application."""

    def __init__(
        self,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
        session_builder: DaggerSessionBuilder | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.session_builder = session_builder or DaggerSessionBuilder(stdout, stderr)

    def build_shadow(self, spec: DaggerRunSpec) -> ShadowApplication:
        settings, request = self._load(spec)
        session = self.session_builder.build(spec, settings)
        return ShadowApplication(
            spec,
            settings,
            request,
            session,
            stdout=self.stdout,
            stderr=self.stderr,
        )

    def build_takeover_dry_run(
        self,
        spec: DaggerRunSpec,
    ) -> TakeoverDryRunApplication:
        settings, request = self._load(spec)
        session = self.session_builder.build(
            spec,
            settings,
            additional_recording_topics=DAGGER_RECORDING_TOPICS,
        )
        return TakeoverDryRunApplication(
            spec,
            settings,
            request,
            session,
            stdout=self.stdout,
            stderr=self.stderr,
        )

    def build_takeover(self, spec: DaggerRunSpec) -> TakeoverApplication:
        settings, request = self._load(spec)
        session = self.session_builder.build(
            spec,
            settings,
            additional_recording_topics=DAGGER_RECORDING_TOPICS,
        )
        return TakeoverApplication(
            spec,
            settings,
            request,
            session,
            stdout=self.stdout,
            stderr=self.stderr,
        )

    @staticmethod
    def _load(
        spec: DaggerRunSpec,
    ) -> tuple[DaggerCollectorSettings, EpisodeRequest]:
        validate_task_streams(spec.task_config)
        settings = DaggerCollectorSettings.load(spec.policy_config)
        request = load_request(
            spec.task_config,
            spec.output_root,
            spec.station_config,
            task_description=spec.task_description,
        )
        return settings, request
