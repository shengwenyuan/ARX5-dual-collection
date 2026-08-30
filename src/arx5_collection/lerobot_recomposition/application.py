from __future__ import annotations

from dataclasses import dataclass
from io import TextIOBase
from pathlib import Path

from .alignment import align_plan
from .config import load_config
from .models import CompositionResult
from .planner import build_plan
from .v21 import build_v21
from .v3 import build_v3


@dataclass(frozen=True, slots=True)
class CompositionRequest:
    config_path: Path


def execute_composition(
    request: CompositionRequest,
    stdin: TextIOBase,
    stdout: TextIOBase,
) -> CompositionResult:
    config = load_config(request.config_path)
    plan = build_plan(config)
    align_plan(plan, stdin, stdout)
    if config.output.backend == "lerobot-v2.1":
        return build_v21(plan)
    if config.output.backend == "lerobot-v3.0":
        return build_v3(plan)
    raise ValueError(f"unsupported composition backend: {config.output.backend}")
