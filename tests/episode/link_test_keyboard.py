from __future__ import annotations

import os

from arx5_collection.episode.adapters import keyboard as keyboard_module


def main() -> None:
    master_fd, slave_fd = os.openpty()
    try:
        with os.fdopen(slave_fd, "r") as stream:
            with keyboard_module.KeyboardTrigger(stream) as trigger:
                os.write(master_fd, b" ")
                assert trigger.wait(0.1)
    finally:
        os.close(master_fd)

    print(f"installed_from={keyboard_module.__file__}")
    print("episode_keyboard_link=ok")


if __name__ == "__main__":
    main()
