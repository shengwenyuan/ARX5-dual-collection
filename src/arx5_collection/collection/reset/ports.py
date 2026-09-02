from typing import Protocol, runtime_checkable


@runtime_checkable
class DualArmResetController(Protocol):
    def reset_both(self) -> None: ...

    def enable_gravity_compensation(self) -> None: ...
