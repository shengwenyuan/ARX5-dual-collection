from __future__ import annotations

import tempfile
from pathlib import Path

from arx5_collection.episode import store as store_module


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = store_module.EpisodeStore(Path(directory) / "episodes")
        pending = store.prepare("link-episode")
        pending.mcap_path.write_bytes(b"mcap")
        pending.metadata_path.write_text("{}")
        stored = store.commit(pending)

        assert stored.mcap_path.read_bytes() == b"mcap"
        assert stored.metadata_path.read_text() == "{}"
        assert store.list_partials() == ()
        assert {path.name for path in stored.directory.iterdir()} == {
            "episode.mcap",
            "metadata.json",
        }

    print(f"installed_from={store_module.__file__}")
    print("episode_store_link=ok")


if __name__ == "__main__":
    main()
