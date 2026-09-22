from types import SimpleNamespace
from threading import Event
import numpy as np
import pytest
from arx5_collection.dagger.expo import ExpoPolicyClient
from arx5_collection.dagger.models import PolicyExecutionProfile
from arx5_collection.dagger.policy_client import (
    RtcPolicyContext,
    StalePolicyResponseError,
)
from tests.dagger.test_pi05_policy_client import Encoder


class Source:
    def __init__(self):
        self.stamp = 100

    def capture(self):
        self.stamp += 1
        return SimpleNamespace(cutoff_ns=self.stamp)


class Transport:
    _numpy = np
    bundle_id = "bundle"

    def __init__(self):
        self.calls = []
        self.block = False
        self.entered = Event()
        self.release = Event()

    def rpc(self, op, **fields):
        self.calls.append((op, fields))
        if op == "begin":
            return {"ok": True}
        wire = fields["payload"]
        if op == "prepare":
            self.entered.set()
            if self.block:
                self.release.wait(2.0)
            return {
                "ticket": "ticket",
                "bundle_id": "bundle",
                "delay": wire.get("rtc", {}).get("estimated_delay_steps", 0),
            }
        return {
            **{
                k: wire[k]
                for k in (
                    "session_id",
                    "episode_id",
                    "control_epoch",
                    "inference_id",
                    "checkpoint_sha256",
                    "expo_bundle_id",
                )
            },
            "actions": np.zeros((30, 14)),
            "started_at_ns": 0,
            "completed_at_ns": 1,
            "expo": {
                "selected_index": 41,
                "mode": "q_edit",
                "actual_delay": fields["actual_delay"],
                "selected_window": [fields["actual_delay"], fields["actual_delay"] + 8],
            },
        }


def client():
    transport = Transport()
    return ExpoPolicyClient(
        "session",
        "task",
        "a" * 64,
        Source(),
        Encoder(),
        transport,
        PolicyExecutionProfile(30, 14, 8, 30.0),
    ), transport


def test_bootstrap_uses_edit_q_and_continuation_gets_new_snapshot():
    c, t = client()
    try:
        result = c.submit("episode", 0, "bootstrap").result(2)
        assert dict(result.candidate_identity) == dict(
            bundle_id="bundle", selected_index=41, mode="q_edit", actual_delay=0
        )
        assert [v[0] for v in t.calls] == ["begin", "prepare", "finish"]
        prepared = c.submit(
            "episode", 0, "next", rtc=RtcPolicyContext(4, ((0.0,) * 14,) * 4)
        ).result(2)
        assert t.calls[-1][0] == "prepare"
        c.finish(prepared, "episode", 0, "next", 4).result(2)
        slow = t.calls[-2][1]["payload"]
        fast = t.calls[-1][1]["payload"]
        assert (slow["observation_time_ns"], fast["observation_time_ns"]) == (103, 104)
        assert all(a.flags.c_contiguous for a in slow["observation"]["images"].values())
    finally:
        c.close()


def test_epoch_change_discards_inflight_candidates():
    c, t = client()
    t.block = True
    try:
        f = c.submit("episode", 0, "request")
        assert t.entered.wait(1)
        c.begin_epoch(1)
        t.release.set()
        with pytest.raises(StalePolicyResponseError):
            f.result(2)
        assert [op for op, _ in t.calls] == ["begin", "prepare"]
    finally:
        t.release.set()
        c.close()
