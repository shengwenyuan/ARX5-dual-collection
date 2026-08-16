from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum


class CheckPhase(str, Enum):
    SESSION = "session"
    SYSTEM = "system"
    ROS = "ros"
    EPISODE = "episode"
    RUNTIME = "runtime"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    phase: CheckPhase
    passed: bool
    detail: str


Check = Callable[[], CheckResult]


class CheckFailure(RuntimeError):
    def __init__(self, results: tuple[CheckResult, ...]) -> None:
        self.results = results
        failed = [result.name for result in results if not result.passed]
        super().__init__(f"checks failed: {', '.join(failed)}")


def run_checks(checks: Iterable[Check]) -> tuple[CheckResult, ...]:
    results = tuple(check() for check in checks)
    if any(not result.passed for result in results):
        raise CheckFailure(results)
    return results

