# Action validation and independent diagnostics

`absolute-target-continuity-v2` is recorded in each infer episode configuration.

- `max_initial_joint_step_rad` bounds the first target relative to measured state when no target has been published. TOML omission inherits `max_joint_step_rad` for existing configurations.
- `max_joint_step_rad` bounds successive targets, including the first target of a continuation against the last successfully published command.
- `max_joint_departure_rad` still bounds targets relative to the current measured state. Finite values, freshness, prefix/epoch identity, watchdogs and manual stopping remain required.
- EXPO checks its fast deadline again after accepting the window and reacquiring the command lock. An overdue window publishes no command.

Tracking error is not a successive-target step. The historical 0.25 rad continuation check was not a continuous or certified tracking limit. Removing that conflation does **not** resolve the observed tracking error or establish motion safety. Analyze tracking from recorded commands and feedback offline; the normal collection path has no dedicated tracking trace or experimental bypass flag.

`arx5 infer` remains the human-operated entry. `arx5 infer --external-policy` starts only the collector and uses a separately managed policy service (for stations using a host GPU environment). The caller must establish that service and enforce resource ownership. This option does not change action validation or recording.
