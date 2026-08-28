from __future__ import annotations

import os
from pathlib import Path
import runpy
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
ENTRY = runpy.run_path(ROOT / "scripts" / "arx5")


def test_collect_hides_compose_arguments() -> None:
    with patch.dict(
        os.environ,
        {
            "ARX5_OUTPUT_ROOT": "/reports/2026-08-27/fold_cloth",
            "ARX5_TASK_DESCRIPTION": "folding the cloth",
        },
    ), patch("subprocess.call", return_value=0) as call:
        assert ENTRY["collect"]() == 0

    argv = call.call_args.args[0]
    assert argv[-3:] == ["run", "--rm", "collector"]
    assert "compose.production.yaml" in argv[3]
    assert "ARX5_TASK_CONFIG" not in call.call_args.kwargs["env"]


def test_collect_rgb_only_is_scoped_to_compose_process() -> None:
    with patch.dict(
        os.environ,
        {
            "ARX5_OUTPUT_ROOT": "/reports/2026-08-27/fold_cloth",
            "ARX5_TASK_DESCRIPTION": "folding the cloth",
        },
        clear=True,
    ), patch("subprocess.call", return_value=0) as call:
        assert ENTRY["collect"](rgb_only=True) == 0

    assert call.call_args.kwargs["env"]["ARX5_TASK_CONFIG"] == (
        "/workspace/config/task.rgb-only.json"
    )
    assert "ARX5_TASK_CONFIG" not in os.environ


def test_upload_passes_only_resolved_inputs_to_core(tmp_path: Path) -> None:
    source = "/reports/2026-08-27/fold_cloth"
    with patch.dict(
        os.environ,
        {
            "ARX5_OUTPUT_ROOT": source,
            "ARX5_TASK_DESCRIPTION": "folding the cloth",
        },
    ), patch.object(Path, "home", return_value=tmp_path), patch(
        "subprocess.call", return_value=0
    ) as call:
        assert ENTRY["upload"]("false") == 0

    argv = call.call_args.args[0]
    assert argv[-6:] == [
        "--source",
        source,
        "--task-description",
        "folding the cloth",
        "--full-check",
        "false",
    ]
    assert "arx5-dual-collection:dataset" in argv
