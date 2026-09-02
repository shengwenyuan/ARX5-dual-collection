from pathlib import Path


ROOT = Path(__file__).parents[1]
CONFIG_CATEGORIES = {
    "collection",
    "dataset_pipeline",
    "specs",
    "runner",
    "environment",
}
CONFIG_SUFFIXES = {".toml", ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg"}


def test_config_root_has_exactly_five_categories() -> None:
    entries = set((ROOT / "config").iterdir())

    assert {path.name for path in entries if path.is_dir()} == CONFIG_CATEGORIES
    assert not {path for path in entries if path.is_file()}


def test_runtime_configuration_assets_stay_out_of_code_directories() -> None:
    observed = {
        path.relative_to(ROOT).as_posix()
        for directory in (ROOT / "src", ROOT / "scripts", ROOT / "docker")
        for path in directory.rglob("*")
        if path.is_file()
        and (path.suffix in CONFIG_SUFFIXES or path.name.endswith(".env.example"))
    }
    package_metadata = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src/ros2").glob("*/package.xml")
    } | {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src/ros2").glob("*/setup.cfg")
    }

    assert observed == package_metadata


def test_source_has_no_configuration_payload_directories() -> None:
    forbidden = {"config", "configs", "recipes", "schemas", "policies"}
    observed = {
        path.relative_to(ROOT).as_posix()
        for directory in (ROOT / "src", ROOT / "scripts", ROOT / "docker")
        for path in directory.rglob("*")
        if path.is_dir() and path.name in forbidden
    }

    assert observed == set()
