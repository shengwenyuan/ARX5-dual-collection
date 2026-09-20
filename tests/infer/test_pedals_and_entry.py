from pathlib import Path
import os
import runpy
from unittest.mock import patch

from arx5_collection.episode.adapters.pedal import HidrawPedal, PRESS_REPORT, PedalTrigger
from arx5_collection.episode.ports import TriggerEvent


def test_simultaneous_reports_are_conflict_only_when_opted_in():
    for conflict_event, expected in ((None, TriggerEvent.ABORT), (TriggerEvent.CONFLICT, TriggerEvent.CONFLICT)):
        readable = iter([(), (21, 23)])
        with PedalTrigger(
            {TriggerEvent.ACTIVATE: HidrawPedal(Path("right"), 21), TriggerEvent.ABORT: HidrawPedal(Path("left"), 23)},
            select_function=lambda *args: (next(readable), (), ()),
            read_function=lambda *args: PRESS_REPORT, close_function=lambda fd: None,
            conflict_event=conflict_event,
        ) as pedal:
            pedal.arm()
            assert pedal.wait(0).event is expected


def test_host_infer_reuses_dagger_services_with_command_override():
    root = Path(__file__).parents[2]
    entry = runpy.run_path(root / "scripts/arx5")
    with patch.dict(os.environ, {"ARX5_OUTPUT_ROOT": "/reports/infer", "ARX5_TASK_DESCRIPTION": "fold"}, clear=True), patch("subprocess.call", return_value=0) as call:
        assert entry["dagger"]("infer") == 0
    argv = call.call_args.args[0]
    assert str(root / "docker/compose.dagger.yaml") in argv
    assert str(root / "docker/compose.infer.yaml") in argv
    assert "--no-build" in argv
