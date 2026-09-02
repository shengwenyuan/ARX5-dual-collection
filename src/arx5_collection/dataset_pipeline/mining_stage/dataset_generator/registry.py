from .lerobot_dataset_merge import run as lerobot_dataset_merge
from .lerobot_dataset_validator import run as lerobot_dataset_validator
from .lerobot_fragment_generator import run as lerobot_fragment_generator
from .lerobot_fragment_validator import run as lerobot_fragment_validator


EPISODE_UNIT_RUNNERS = {
    "lerobot_fragment_generator": lerobot_fragment_generator,
    "lerobot_fragment_validator": lerobot_fragment_validator,
}

DATASET_UNIT_RUNNERS = {
    "lerobot_dataset_merge": lerobot_dataset_merge,
    "lerobot_dataset_validator": lerobot_dataset_validator,
}
