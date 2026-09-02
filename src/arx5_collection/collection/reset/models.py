from enum import Enum


class ResetState(str, Enum):
    RESETTING = "resetting"
    COMPLETE = "reset_complete"
