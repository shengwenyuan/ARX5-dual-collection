from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ArmResetSpec:
    name: str
    status_topic: str
    go_home_service: str
    gravity_service: str


DEFAULT_HOME = (0.0, 0.948, 0.858, -0.573, 0.0, 0.0)
DEFAULT_ARMS = (
    ArmResetSpec(
        "left",
        "/arm_master_l_status",
        "/arm_master_l/go_home",
        "/arm_master_l/gravity_compensation",
    ),
    ArmResetSpec(
        "right",
        "/arm_master_r_status",
        "/arm_master_r/go_home",
        "/arm_master_r/gravity_compensation",
    ),
)

TimingSink = Callable[[str, float], None]
ArmSample = tuple[tuple[float, ...], tuple[float, ...], float]


class RosDualArmResetController:
    """Keep Vendor home services and arm telemetry alive for one Session."""

    def __init__(
        self,
        arms: tuple[ArmResetSpec, ...] = DEFAULT_ARMS,
        home: tuple[float, ...] = DEFAULT_HOME,
        timeout_s: float = 45.0,
        position_tolerance_rad: float = 0.03,
        velocity_tolerance_rad_s: float = 0.05,
        stable_s: float = 0.5,
        timing_sink: TimingSink | None = None,
        clock: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], None] = sleep,
    ) -> None:
        if len(arms) != 2 or len(home) != 6:
            raise ValueError("dual-arm reset requires two arms and six home joints")
        if min(timeout_s, position_tolerance_rad, velocity_tolerance_rad_s, stable_s) <= 0:
            raise ValueError("reset convergence settings must be positive")
        self.arms = arms
        self.home = home
        self.timeout_s = timeout_s
        self.position_tolerance_rad = position_tolerance_rad
        self.velocity_tolerance_rad_s = velocity_tolerance_rad_s
        self.stable_s = stable_s
        self.timing_sink = timing_sink or (lambda phase, elapsed_s: None)
        self.clock = clock
        self.sleep_fn = sleep_fn
        self._samples: dict[str, ArmSample] = {}
        self._samples_lock = Lock()
        self._context: Any | None = None
        self._node: Any | None = None
        self._executor: Any | None = None
        self._thread: Thread | None = None
        self._home_clients: list[Any] = []
        self._gravity_clients: list[Any] = []
        self._spin_error: BaseException | None = None

    def open(self) -> None:
        if self._context is not None:
            raise RuntimeError("dual-arm reset controller is already open")
        import rclpy
        from arx5_arm_msg.msg import RobotStatus
        from rclpy.executors import SingleThreadedExecutor
        from std_srvs.srv import Trigger

        started_s = self.clock()
        self._spin_error = None
        self._context = rclpy.Context()
        try:
            rclpy.init(context=self._context)
            self._node = rclpy.create_node(
                f"dual_arm_reset_{uuid4().hex[:8]}", context=self._context
            )
            self._executor = SingleThreadedExecutor(context=self._context)
            self._executor.add_node(self._node)

            for arm in self.arms:
                def observe(message, name=arm.name):
                    with self._samples_lock:
                        self._samples[name] = (
                            tuple(message.joint_pos[:6]),
                            tuple(message.joint_vel[:6]),
                            self.clock(),
                        )

                self._node.create_subscription(
                    RobotStatus, arm.status_topic, observe, 10
                )

            self._home_clients = [
                self._node.create_client(Trigger, arm.go_home_service)
                for arm in self.arms
            ]
            self._gravity_clients = [
                self._node.create_client(Trigger, arm.gravity_service)
                for arm in self.arms
            ]
            self._thread = Thread(
                target=self._spin,
                name="dual-arm-reset",
                daemon=False,
            )
            self._thread.start()
            self._wait_for_services(self._home_clients + self._gravity_clients)
            self.timing_sink("service_discovery", self.clock() - started_s)
        except BaseException:
            self.close()
            raise

    def reset_both(self) -> None:
        if self._context is None:
            raise RuntimeError("dual-arm reset controller is not open")
        total_started_s = self.clock()

        phase_started_s = self.clock()
        self._call_all(self._home_clients, "GO_HOME")
        home_requested_s = self.clock()
        self.timing_sink("go_home_request", home_requested_s - phase_started_s)

        phase_started_s = self.clock()
        self._wait_for_convergence(home_requested_s)
        self.timing_sink("convergence", self.clock() - phase_started_s)

        phase_started_s = self.clock()
        self._call_all(self._gravity_clients, "G_COMPENSATION")
        self.timing_sink("gravity_compensation", self.clock() - phase_started_s)
        self.timing_sink("total", self.clock() - total_started_s)

    def enable_gravity_compensation(self) -> None:
        """Put both arms in the user-movable mode without commanding a pose."""
        if self._context is None:
            raise RuntimeError("dual-arm reset controller is not open")
        self._call_all(self._gravity_clients, "G_COMPENSATION")

    def wait_for_samples(self, timeout_s: float | None = None) -> None:
        if self._context is None:
            raise RuntimeError("dual-arm reset controller is not open")
        deadline = self.clock() + (timeout_s or self.timeout_s)
        while self.clock() < deadline:
            self._require_spin()
            with self._samples_lock:
                ready = set(self._samples) == {arm.name for arm in self.arms}
            if ready:
                return
            self.sleep_fn(0.01)
        raise TimeoutError("dual-arm status samples are not ready")

    def sample_positions(self) -> dict[str, tuple[float, ...]]:
        with self._samples_lock:
            return {
                name: sample[0]
                for name, sample in self._samples.items()
            }

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(timeout_sec=5.0)
        if self._node is not None:
            self._node.destroy_node()
        if self._context is not None and self._context.ok():
            self._context.shutdown()
        if self._thread is not None:
            self._thread.join(5.0)
            if self._thread.is_alive():
                raise TimeoutError("dual-arm reset controller did not stop")
        self._context = None
        self._node = None
        self._executor = None
        self._thread = None
        self._home_clients = []
        self._gravity_clients = []
        with self._samples_lock:
            self._samples.clear()

    def _spin(self) -> None:
        try:
            assert self._executor is not None
            self._executor.spin()
        except BaseException as error:
            self._spin_error = error

    def _require_spin(self) -> None:
        if self._spin_error is not None:
            raise RuntimeError("dual-arm reset executor failed") from self._spin_error

    def _wait_for_services(self, clients: list[Any]) -> None:
        deadline = self.clock() + self.timeout_s
        pending = list(clients)
        while pending and self.clock() < deadline:
            self._require_spin()
            self.sleep_fn(0.05)
            pending = [client for client in pending if not client.service_is_ready()]
        if pending:
            raise TimeoutError("ARX5 reset services are not ready")

    def _call_all(self, clients: list[Any], label: str) -> None:
        from std_srvs.srv import Trigger

        futures = [client.call_async(Trigger.Request()) for client in clients]
        deadline = self.clock() + self.timeout_s
        while not all(future.done() for future in futures) and self.clock() < deadline:
            self._require_spin()
            self.sleep_fn(0.01)
        if not all(future.done() for future in futures):
            raise TimeoutError(f"ARX5 {label} request timed out")
        failures = []
        for future in futures:
            if future.exception() is not None:
                failures.append(str(future.exception()))
                continue
            response = future.result()
            if response is None:
                failures.append("empty response")
            elif not response.success:
                failures.append(response.message)
        if failures:
            raise RuntimeError(f"ARX5 {label} rejected: {failures}")

    def _wait_for_convergence(self, command_started_s: float) -> None:
        deadline = self.clock() + self.timeout_s
        stable_since: float | None = None
        while self.clock() < deadline:
            self._require_spin()
            with self._samples_lock:
                current = dict(self._samples)
            converged = len(current) == len(self.arms) and all(
                received_at_s >= command_started_s
                and all(
                    abs(position[index] - target) <= self.position_tolerance_rad
                    for index, target in enumerate(self.home)
                )
                and all(
                    abs(value) <= self.velocity_tolerance_rad_s
                    for value in velocity
                )
                for position, velocity, received_at_s in current.values()
            )
            if not converged:
                stable_since = None
            else:
                now_s = self.clock()
                if stable_since is None:
                    stable_since = now_s
                elif now_s - stable_since >= self.stable_s:
                    return
            self.sleep_fn(0.01)
        raise TimeoutError("dual-arm GO_HOME did not converge")
