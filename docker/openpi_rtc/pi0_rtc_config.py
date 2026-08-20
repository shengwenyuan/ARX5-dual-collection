import dataclasses
from typing import TYPE_CHECKING

import flax.nnx as nnx
from typing_extensions import override

from openpi.models import pi0_config
from openpi.shared import array_typing as at

if TYPE_CHECKING:
    from openpi.models.pi0_rtc import Pi05ActionPrefixModel


@dataclasses.dataclass(frozen=True)
class Pi05RtcConfig(pi0_config.Pi0Config):
    """π0.5 config for training-time action-prefix conditioning."""

    pi05: bool = True
    max_delay: int = 10

    def __post_init__(self):
        super().__post_init__()
        if not self.pi05:
            raise ValueError("Pi05RtcConfig requires pi05=True")
        if not 1 <= self.max_delay <= self.action_horizon:
            raise ValueError("max_delay must be within [1, action_horizon]")

    @override
    def create(self, rng: at.KeyArrayLike) -> "Pi05ActionPrefixModel":
        from openpi.models.pi0_rtc import Pi05ActionPrefixModel

        return Pi05ActionPrefixModel(self, rngs=nnx.Rngs(rng))
