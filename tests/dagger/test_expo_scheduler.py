from concurrent.futures import Future
from dataclasses import replace
import pytest

from arx5_collection.dagger.expo import ExpoActionScheduler
from arx5_collection.dagger.models import (
    PolicyExecutionProfile,
    RtcRolloutProfile,
    InferenceTicket,
)
from tests.dagger.test_rtc_scheduler import CHECKPOINT, SHA, State, Sink, Mode, action
from arx5_collection.dagger.action_gateway import (
    Pi05JointActionContract,
    JointActionSafety,
)
from arx5_collection.gripper import ARX5_GRIPPER_CALIBRATION


class Policy:
    def __init__(self):
        self.slow = []
        self.fast = []

    def begin_epoch(self, e):
        pass

    def submit(self, *args, **kwargs):
        f = Future()
        self.slow.append((f, args, kwargs))
        return f

    def finish(self, *args):
        f = Future()
        self.fast.append((f, args))
        return f


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def fixture(*, bootstrap=True, bootstrap_timeout_s=None, wait=1.0):
    policy = Policy()
    sink = Sink()
    clock = Clock()
    cp = replace(
        CHECKPOINT,
        execution=PolicyExecutionProfile(30, 14, 8, 30.0),
        max_delay_steps=10,
    )
    contract = Pi05JointActionContract(
        SHA, ARX5_GRIPPER_CALIBRATION, JointActionSafety(0.25, 1.5, 0.0, 1.0)
    )
    scheduler = ExpoActionScheduler(
        policy,
        State(),
        sink,
        contract,
        Mode(),
        cp,
        RtcRolloutProfile(4, 4, 10, "rolling_max"),
        wait,
        0.12,
        clock=clock,
        bootstrap_timeout_s=bootstrap_timeout_s,
    )
    scheduler.clear_pending(1)
    scheduler.prepare_policy("episode", 1)

    def ticket():
        return InferenceTicket("id", 1, SHA, (action(),) * 30, cp.execution)

    if bootstrap:
        policy.slow[0][0].set_result(ticket())
        assert scheduler.policy_ready("episode", 1)
    return scheduler, policy, sink, clock, ticket


def test_expo_bootstrap_can_exceed_old_150ms_with_gate_closed():
    s, p, sink, clock, ticket = fixture(
        bootstrap=False, bootstrap_timeout_s=2.0, wait=0.15
    )
    clock.value = 0.164
    assert not s.policy_ready("episode", 1)
    s.step()
    assert not s.gate_open and not sink.commands
    clock.value = 0.233
    p.slow[0][0].set_result(ticket())
    assert s.policy_ready("episode", 1)
    assert not sink.commands
    # Continuation still fails at the fixed C boundary; 2s applies only to bootstrap.
    for i in range(8):
        clock.value = 0.233 + i / 30
        s.step()
    assert not s.gate_open and len(sink.commands) == 8
    assert "candidates missed" in str(s.take_fault())


def test_bootstrap_is_bounded_and_logs_its_own_timeout():
    s, p, sink, clock, ticket = fixture(
        bootstrap=False, bootstrap_timeout_s=2.0, wait=0.15
    )
    events = []
    s.diagnostic_sink = events.append
    clock.value = 2.001
    with pytest.raises(RuntimeError, match="bootstrap inference timeout"):
        s.policy_ready("episode", 1)
    assert not s.gate_open and not sink.commands
    assert events[-1]["event"] == "bootstrap_timeout"
    assert events[-1]["timeout_s"] == 2.0


def test_without_override_original_bootstrap_deadline_is_unchanged():
    s, p, sink, clock, ticket = fixture(bootstrap=False, wait=0.15)
    clock.value = 0.164
    with pytest.raises(RuntimeError, match="bootstrap inference timeout"):
        s.policy_ready("episode", 1)


def advance(scheduler, clock, index):
    clock.value = index / 30
    scheduler.step()


def test_prefetch_at_four_finish_at_eight_and_no_early_splice():
    s, p, sink, clock, ticket = fixture()
    for i in range(4):
        advance(s, clock, i)
    assert len(p.slow) == 2 and len(sink.commands) == 4
    context = p.slow[1][2]["rtc"]
    assert context.estimated_delay_steps == 4 and len(context.action_prefix) == 4
    p.slow[1][0].set_result({"ticket": "prepared"})
    for i in range(4, 8):
        advance(s, clock, i)
    assert len(p.fast) == 1 and len(sink.commands) == 8
    p.fast[0][0].set_result(ticket())
    clock.value = 7 / 30 + 0.001
    s.step()
    assert len(sink.commands) == 8 and s.pending_command_count == 8
    advance(s, clock, 8)
    assert len(sink.commands) == 9


def test_late_candidates_close_gate_without_executing_base_tail():
    s, p, sink, clock, ticket = fixture()
    for i in range(8):
        advance(s, clock, i)
    assert not s.gate_open and len(sink.commands) == 8
    assert "candidates missed" in str(s.take_fault())
    p.slow[1][0].set_result({"ticket": "late"})
    advance(s, clock, 8)
    assert len(sink.commands) == 8


def test_late_fast_phase_and_epoch_cancel_cannot_send_old_actions():
    s, p, sink, clock, ticket = fixture()
    for i in range(4):
        advance(s, clock, i)
    p.slow[1][0].set_result({"ticket": "prepared"})
    for i in range(4, 8):
        advance(s, clock, i)
    clock.value = 8 / 30 + 0.001
    s.step()
    assert not s.gate_open and "fast-stage" in str(s.take_fault())
    p.fast[0][0].set_result(ticket())
    s.clear_pending(2)
    s.step()
    assert len(sink.commands) == 8
