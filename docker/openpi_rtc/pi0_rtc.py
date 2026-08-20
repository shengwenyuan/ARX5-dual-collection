"""Training-time RTC for the JAX π0.5 action expert."""

from __future__ import annotations

import einops
import flax.nnx as nnx
import jax
import jax.numpy as jnp
from typing_extensions import override

from openpi.models import model as _model
from openpi.models import pi0
from openpi.models import pi0_rtc_config
from openpi.shared import array_typing as at


def prefix_mask(delay: at.Int[at.Array, " b"], action_horizon: int) -> at.Bool[at.Array, "b ah"]:
    return jnp.arange(action_horizon)[None, :] < delay[:, None]


def hard_prefix_update(x_t, velocity, action_prefix, conditioned_prefix, dt):
    updated = x_t + dt * velocity
    return jnp.where(conditioned_prefix[..., None], action_prefix, updated)


class Pi05ActionPrefixModel(pi0.Pi0):
    """π0.5 with per-action timesteps and hard-prefix action generation."""

    def __init__(self, config: pi0_rtc_config.Pi05RtcConfig, rngs: nnx.Rngs):
        super().__init__(config, rngs)
        self.max_delay = config.max_delay

    def embed_suffix(self, obs: _model.Observation, noisy_actions: _model.Actions, timestep):
        del obs
        if timestep.ndim != 2 or timestep.shape != noisy_actions.shape[:2]:
            raise ValueError(
                f"training-time RTC expects per-token timesteps {noisy_actions.shape[:2]}, got {timestep.shape}"
            )
        action_tokens = self.action_in_proj(noisy_actions)
        flat_time = timestep.reshape(-1)
        time_emb = pi0.posemb_sincos(
            flat_time,
            self.action_in_proj.out_features,
            min_period=4e-3,
            max_period=4.0,
        ).reshape(*timestep.shape, self.action_in_proj.out_features)
        time_emb = self.time_mlp_in(time_emb)
        time_emb = nnx.swish(time_emb)
        time_emb = self.time_mlp_out(time_emb)
        adarms_cond = nnx.swish(time_emb)
        input_mask = jnp.ones(action_tokens.shape[:2], dtype=jnp.bool_)
        ar_mask = jnp.array([True] + ([False] * (self.action_horizon - 1)))
        return action_tokens, input_mask, ar_mask, adarms_cond

    def _cached_velocity(self, observation, x_t, token_time, prefix_mask_, kv_cache):
        batch_size = x_t.shape[0]
        suffix_tokens, suffix_mask, suffix_ar_mask, adarms_cond = self.embed_suffix(observation, x_t, token_time)
        suffix_mask_self = pi0.make_attn_mask(suffix_mask, suffix_ar_mask)
        suffix_mask_prefix = einops.repeat(prefix_mask_, "b p -> b s p", s=suffix_tokens.shape[1])
        full_mask = jnp.concatenate([suffix_mask_prefix, suffix_mask_self], axis=-1)
        positions = jnp.sum(prefix_mask_, axis=-1)[:, None] + jnp.cumsum(suffix_mask, axis=-1) - 1
        (_, suffix_out), _ = self.PaliGemma.llm(
            [None, suffix_tokens],
            mask=full_mask,
            positions=positions,
            kv_cache=kv_cache,
            adarms_cond=[None, adarms_cond],
        )
        return self.action_out_proj(suffix_out[:, -self.action_horizon :]).reshape(
            batch_size, self.action_horizon, self.action_dim
        )

    @override
    def sample_actions(
        self,
        rng: at.KeyArrayLike,
        observation: _model.Observation,
        *,
        action_prefix: _model.Actions | None = None,
        delay: at.Int[at.Array, " b"] | None = None,
        num_steps: int | at.Int[at.Array, ""] = 10,
        noise: at.Float[at.Array, "b ah ad"] | None = None,
    ) -> _model.Actions:
        if (action_prefix is None) != (delay is None):
            raise ValueError("action_prefix and delay must be provided together")
        observation = _model.preprocess_observation(None, observation, train=False)
        batch_size = observation.state.shape[0]
        if noise is None:
            noise = jax.random.normal(rng, (batch_size, self.action_horizon, self.action_dim))
        elif noise.shape != (batch_size, self.action_horizon, self.action_dim):
            raise ValueError(
                "noise must have shape "
                f"{(batch_size, self.action_horizon, self.action_dim)}, got {noise.shape}"
            )
        if action_prefix is None:
            action_prefix = jnp.zeros_like(noise)
        elif action_prefix.shape != noise.shape:
            raise ValueError(f"action_prefix must have shape {noise.shape}, got {action_prefix.shape}")
        if delay is None:
            delay = jnp.zeros((batch_size,), dtype=jnp.int32)
        elif delay.shape != (batch_size,):
            raise ValueError(f"delay must have shape {(batch_size,)}, got {delay.shape}")
        mask = prefix_mask(delay, self.action_horizon)[..., None]

        prefix_tokens, prefix_mask_, prefix_ar_mask = self.embed_prefix(observation)
        prefix_attn_mask = pi0.make_attn_mask(prefix_mask_, prefix_ar_mask)
        positions = jnp.cumsum(prefix_mask_, axis=1) - 1
        _, kv_cache = self.PaliGemma.llm([prefix_tokens, None], mask=prefix_attn_mask, positions=positions)
        dt = -1.0 / num_steps

        def step(carry):
            x_t, time = carry
            x_t = jnp.where(mask, action_prefix, x_t)
            token_time = jnp.where(mask[..., 0], 0.0, jnp.broadcast_to(time, mask.shape[:2]))
            velocity = self._cached_velocity(observation, x_t, token_time, prefix_mask_, kv_cache)
            return hard_prefix_update(x_t, velocity, action_prefix, mask[..., 0], dt), time + dt

        def cond(carry):
            _, time = carry
            return time >= -dt / 2

        actions, _ = jax.lax.while_loop(cond, step, (noise, 1.0))
        return jnp.where(mask, action_prefix, actions)
