from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic
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


class RosDualArmResetController:
    """Call the patched Vendor GO_HOME services and verify telemetry convergence."""

    def __init__(
        self,
        arms: tuple[ArmResetSpec, ...] = DEFAULT_ARMS,
        home: tuple[float, ...] = DEFAULT_HOME,
        timeout_s: float = 45.0,
        position_tolerance_rad: float = 0.03,
        velocity_tolerance_rad_s: float = 0.05,
        stable_s: float = 0.5,
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

    def reset_both(self) -> None:
        import rclpy
        from arx5_arm_msg.msg import RobotStatus
        from rclpy.executors import SingleThreadedExecutor
        from std_srvs.srv import Trigger

        context = rclpy.Context()
        node = None
        executor = None
        samples: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {}
        samples_lock = Lock()
        try:
            rclpy.init(context=context)
            node = rclpy.create_node(
                f"dual_arm_reset_{uuid4().hex[:8]}", context=context
            )
            executor = SingleThreadedExecutor(context=context)
            executor.add_node(node)

            for arm in self.arms:
                def observe(message, name=arm.name):
                    with samples_lock:
                        samples[name] = (
                            tuple(message.joint_pos[:6]),
                            tuple(message.joint_vel[:6]),
                        )

                node.create_subscription(RobotStatus, arm.status_topic, observe, 10)

            home_clients = [
                node.create_client(Trigger, arm.go_home_service) for arm in self.arms
            ]
            gravity_clients = [
                node.create_client(Trigger, arm.gravity_service) for arm in self.arms
            ]
            self._wait_for_services(executor, home_clients + gravity_clients)
            self._call_all(executor, home_clients, "GO_HOME")
            self._wait_for_convergence(executor, samples, samples_lock)
            self._call_all(executor, gravity_clients, "G_COMPENSATION")
        finally:
            if executor is not None:
                executor.shutdown()
            if node is not None:
                node.destroy_node()
            if context.ok():
                context.shutdown()

    def _wait_for_services(self, executor, clients) -> None:
        deadline = monotonic() + self.timeout_s
        pending = list(clients)
        while pending and monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)
            pending = [client for client in pending if not client.service_is_ready()]
        if pending:
            raise TimeoutError("ARX5 reset services are not ready")

    def _call_all(self, executor, clients, label: str) -> None:
        from std_srvs.srv import Trigger

        futures = [client.call_async(Trigger.Request()) for client in clients]
        deadline = monotonic() + self.timeout_s
        while not all(future.done() for future in futures) and monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)
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

    def _wait_for_convergence(self, executor, samples, samples_lock: Lock) -> None:
        deadline = monotonic() + self.timeout_s
        stable_since: float | None = None
        while monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)
            with samples_lock:
                current = dict(samples)
            converged = len(current) == len(self.arms) and all(
                all(
                    abs(position[index] - target) <= self.position_tolerance_rad
                    for index, target in enumerate(self.home)
                )
                and all(abs(value) <= self.velocity_tolerance_rad_s for value in velocity)
                for position, velocity in current.values()
            )
            if not converged:
                stable_since = None
                continue
            now = monotonic()
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= self.stable_s:
                return
        raise TimeoutError("dual-arm GO_HOME did not converge")
