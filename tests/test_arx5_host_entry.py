from __future__ import annotations

import os
from pathlib import Path
import runpy
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
ENTRY = runpy.run_path(ROOT / "scripts" / "arx5")


def test_collect_hides_compose_arguments() -> None:
    with (
        patch.dict(
            os.environ,
            {
                "ARX5_OUTPUT_ROOT": "/reports/2026-08-27/fold_cloth",
            },
        ),
        patch("subprocess.call", return_value=0) as call,
    ):
        assert ENTRY["collect"]() == 0

    argv = call.call_args.args[0]
    assert argv[-3:] == ["run", "--rm", "collector"]
    assert "compose.production.yaml" in argv[3]
    assert call.call_args.kwargs["env"]["ARX5_COLLECTION_CONFIG"] == str(
        ROOT / "config/collection/fold-cloth-rgbd.toml"
    )


def test_collect_config_is_scoped_to_compose_process() -> None:
    config = ROOT / "config/collection/fold-cloth-rgb-only.toml"
    with (
        patch.dict(
            os.environ,
            {
                "ARX5_OUTPUT_ROOT": "/reports/2026-08-27/fold_cloth",
            },
            clear=True,
        ),
        patch("subprocess.call", return_value=0) as call,
    ):
        assert ENTRY["collect"](config=config) == 0

    assert call.call_args.kwargs["env"]["ARX5_COLLECTION_CONFIG"] == str(config)
    assert "ARX5_COLLECTION_CONFIG" not in os.environ


def test_dagger_config_is_scoped_to_compose_process() -> None:
    config = ROOT / "config/collection/fold-cloth-rgb-only.toml"
    with (
        patch.dict(
            os.environ,
            {
                "ARX5_OUTPUT_ROOT": "/reports/2026-08-27/fold_cloth",
            },
            clear=True,
        ),
        patch("subprocess.call", return_value=0) as call,
    ):
        assert ENTRY["dagger"]("takeover", config=config) == 0

    assert call.call_args.kwargs["env"]["ARX5_COLLECTION_CONFIG"] == str(config)
    assert "ARX5_COLLECTION_CONFIG" not in os.environ


def test_dagger_leaves_collection_config_to_station_env() -> None:
    with (
        patch.dict(
            os.environ,
            {
                "ARX5_OUTPUT_ROOT": "/reports/2026-08-27/fold_cloth",
            },
            clear=True,
        ),
        patch("subprocess.call", return_value=0) as call,
    ):
        assert ENTRY["dagger"]("takeover") == 0

    assert "ARX5_COLLECTION_CONFIG" not in call.call_args.kwargs["env"]


def test_upload_passes_only_resolved_inputs_to_core(tmp_path: Path) -> None:
    source = "/reports/2026-08-27/fold_cloth"
    with (
        patch.dict(
            os.environ,
            {
                "ARX5_OUTPUT_ROOT": source,
            },
        ),
        patch.object(Path, "home", return_value=tmp_path),
        patch("subprocess.call", return_value=0) as call,
    ):
        assert ENTRY["upload"]("false") == 0

    argv = call.call_args.args[0]
    assert argv[-6:] == [
        "--source",
        source,
        "--collection-config",
        "/config/collection.toml",
        "--full-check",
        "false",
    ]
    assert "--dagger" not in argv
    assert "arx5-dual-collection:dataset" in argv


def test_upload_mounts_explicit_collection_config(tmp_path: Path) -> None:
    config = ROOT / "config/collection/fold-cloth-rgb-only.toml"
    with (
        patch.dict(
            os.environ,
            {
                "ARX5_OUTPUT_ROOT": "/reports/2026-08-27/fold_cloth",
            },
        ),
        patch.object(Path, "home", return_value=tmp_path),
        patch("subprocess.call", return_value=0) as call,
    ):
        assert ENTRY["upload"]("true", config=config) == 0

    assert f"{config}:/config/collection.toml:ro" in call.call_args.args[0]
