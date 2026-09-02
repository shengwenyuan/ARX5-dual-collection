from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def prepare_model_prefix(
    observation: Mapping[str, Any],
    rtc: Mapping[str, Any],
    input_transform: Any,
    *,
    action_horizon: int,
    action_dimension: int,
    max_delay_steps: int,
    numpy_module: Any,
) -> tuple[Mapping[str, Any], Any, int]:
    """Transform a minimal robot-space prefix through the checkpoint pipeline."""
    if set(rtc) != {"estimated_delay_steps", "action_prefix"}:
        raise ValueError("RTC request contains fields outside the minimal contract")
    delay = rtc["estimated_delay_steps"]
    if not isinstance(delay, int) or isinstance(delay, bool):
        raise ValueError("estimated_delay_steps must be an integer")
    if not 0 <= delay < max_delay_steps:
        raise ValueError("estimated delay is outside the trained range")
    prefix = numpy_module.asarray(rtc["action_prefix"], dtype=numpy_module.float32)
    if prefix.shape != (delay, action_dimension):
        raise ValueError(
            "robot-space action prefix shape must be "
            f"{(delay, action_dimension)}, got {prefix.shape}"
        )
    padded = numpy_module.zeros(
        (action_horizon, action_dimension),
        dtype=numpy_module.float32,
    )
    padded[:delay] = prefix
    transform_input = dict(observation)
    transform_input["actions"] = padded
    transformed = dict(input_transform(transform_input))
    try:
        model_prefix = transformed.pop("actions")
    except KeyError as error:
        raise RuntimeError(
            "checkpoint input transform did not produce actions"
        ) from error
    return transformed, model_prefix, delay


class TrainingTimeRtcPolicyAdapter:
    """Expose the v3 hard-prefix model behind the minimal collector protocol."""

    def __init__(
        self,
        base_policy: Any,
        *,
        action_horizon: int,
        action_dimension: int,
        max_delay_steps: int,
        flow_steps: int,
    ) -> None:
        if type(base_policy._model).__name__ != "Pi05ActionPrefixModel":
            raise TypeError("training-time RTC requires Pi05ActionPrefixModel")
        if base_policy._model.action_horizon != action_horizon:
            raise ValueError("loaded model action horizon does not match the profile")
        if base_policy._model.max_delay != max_delay_steps:
            raise ValueError("loaded model delay range does not match the profile")
        self._model = base_policy._model
        self._input_transform = base_policy._input_transform
        self._output_transform = base_policy._output_transform
        self._sample_actions = base_policy._sample_actions
        self._rng = base_policy._rng
        self._action_horizon = action_horizon
        self._action_dimension = action_dimension
        self._max_delay_steps = max_delay_steps
        self._flow_steps = flow_steps

    def infer(self, observation: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._sample(observation, None)

    def infer_rtc(
        self,
        observation: Mapping[str, Any],
        rtc: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._sample(observation, rtc)

    def _sample(
        self,
        observation: Mapping[str, Any],
        rtc: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        import jax
        import jax.numpy as jnp
        import numpy as np
        from openpi.models import model as model_api

        if rtc is None:
            transformed = self._input_transform(dict(observation))
            model_prefix = None
            delay = 0
        else:
            transformed, model_prefix, delay = prepare_model_prefix(
                observation,
                rtc,
                self._input_transform,
                action_horizon=self._action_horizon,
                action_dimension=self._action_dimension,
                max_delay_steps=self._max_delay_steps,
                numpy_module=np,
            )
        inputs = jax.tree.map(lambda value: jnp.asarray(value)[None, ...], transformed)
        model_observation = model_api.Observation.from_dict(inputs)
        self._rng, sample_rng = jax.random.split(self._rng)
        kwargs: dict[str, Any] = {"num_steps": self._flow_steps}
        if model_prefix is not None:
            kwargs.update(
                action_prefix=jnp.asarray(model_prefix)[None, ...],
                delay=jnp.asarray([delay], dtype=jnp.int32),
            )
        actions = self._sample_actions(sample_rng, model_observation, **kwargs)
        jax.block_until_ready(actions)
        return self._output_transform(
            {
                "state": np.asarray(inputs["state"][0]),
                "actions": np.asarray(actions[0]),
            }
        )
