from __future__ import annotations

from arx5_collection.ros2_adapters.reset import RosDualArmResetController


def test_reset_reuses_open_controller_and_reports_phase_timings(monkeypatch) -> None:
    now = [0.0]
    timings: list[tuple[str, float]] = []
    controller = RosDualArmResetController(
        clock=lambda: now[0],
        timing_sink=lambda phase, elapsed_s: timings.append((phase, elapsed_s)),
    )
    context = object()
    controller._context = context
    controller._home_clients = [object(), object()]
    controller._gravity_clients = [object(), object()]

    def call_all(clients, label) -> None:
        now[0] += 0.1

    def converge(command_started_s) -> None:
        now[0] += 0.5

    monkeypatch.setattr(controller, "_call_all", call_all)
    monkeypatch.setattr(controller, "_wait_for_convergence", converge)

    for _ in range(20):
        controller.reset_both()

    assert controller._context is context
    assert [phase for phase, _ in timings] == [
        "go_home_request",
        "convergence",
        "gravity_compensation",
        "total",
    ] * 20
