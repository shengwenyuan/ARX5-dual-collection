---
name: arx5-config-management
description: Add, change, or audit ARX5 collection, dataset pipeline, specs, runner, and environment configuration without moving runtime choices back into source code.
---

# ARX5 Config Management

Read `docs/configuration-management.md` before changing configuration boundaries.

Keep all runtime configuration under exactly one of `config/collection`, `config/dataset_pipeline`, `config/specs`, `config/runner`, or `config/environment`. Treat task identity and upload routing as collection data, hardware bindings as environment data, reusable contracts and recipes as specs, streaming, composition, and BucketLink offline jobs as dataset pipeline data, and submission/runtime-host choices as runner data.

When adding a task, prefer copying the nearest existing config and changing only its values. Change Python only when the request introduces new protocol or algorithm behavior. Do not add package-data copies of schemas, recipes, policies, or device settings.

Keep Compose and host submission files in `config/runner`. Keep Vendor controller parameters and runtime hardware files in `config/environment`. Leave Dockerfiles, package metadata, ROS messages, patches, and algorithm registries with the code because they are build or protocol assets rather than runtime choices.

Modules named `config.py`, `configuration.py`, or `configuration/` may contain only typed models, loading, path resolution, and validation. Reject embedded configuration payloads and operational defaults in those modules.

Treat `config/specs/recipes/*.toml` as the sole authority for which units a stage enables, their order, and their parameters. Treat code registries as capability maps from a unit type to its owning stage and runner. Never add required-unit sets, default unit sequences, automatic unit insertion, missing-unit fallbacks, or a second unit list in a dataset pipeline profile.

Allow existing units to be enabled, disabled, reordered, and tuned through recipe changes alone. A new unit type, a new stage, a changed stage boundary, or changed input/output semantics requires code. Register the implementation and validate its exact parameter contract, but do not activate it from code.

Keep unit dependencies explicit. A recipe with an invalid order or missing producer must fail on the missing artifact; do not silently skip the consumer or substitute an implementation.

For a configuration-only migration, compare the normalized default contract against the latest pre-migration Git commit after rebasing and run corresponding application, worker, and builder tests on both revisions. Also run one identical deterministic semantic export fixture against both revisions. Require exact equality for the LeRobot creation contract, Episode and sample order, float32 state/action values, decoded RGB pixels, tasks, conversion report, and source manifest after normalizing only volatile absolute paths.

Keep the semantic export fixture anchored to the named pre-migration commit. When a real Episode is accessible, run `tests/dataset_pipeline/test_real_export_regression.py` with `ARX5_REAL_EXPORT_EPISODE` and a new `ARX5_REAL_EXPORT_WORK_DIR`. It must run the same MCAP through the baseline commit and current tree, compare the complete audit and selection artifacts, and compare logical Parquet schema and column values after a pinned LeRobot export.

Do not treat encoded video bytes, Parquet container bytes, elapsed durations, temporary directories, or timestamps as stable output. Compare decoded video frames and logical Parquet schema and column values. State explicitly when a regression test uses the `mcap-ros2-support` compatibility boundary instead of a native ROS2 runtime and whether the fixture uses image or video mode.

Use `ARX5_CONFIG_ROOT` for an alternate complete config tree and `ARX5_ENVIRONMENT_CONFIG` for an alternate environment. Preserve strict parsing: unknown, missing, conflicting, and invalid values must fail visibly.

After changes, run `arx5-config validate --config-root config`, the relevant focused tests, and the full test suite when source loaders or shared specs changed. Update README and the configuration management document when ownership or loading rules change.
