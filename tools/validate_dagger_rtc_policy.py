#!/usr/bin/env python3
from __future__ import annotations

import argparse

from arx5_collection.dagger.config import DaggerCollectorSettings
from arx5_collection.dagger.openpi_transport import OpenPiDaggerTransport
from arx5_collection.dagger.policy_probe import run_rtc_policy_probe


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate DAgger bootstrap and RTC policy paths without robot I/O."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    settings = DaggerCollectorSettings.load(args.config)
    with OpenPiDaggerTransport(
        host=settings.server_host,
        port=settings.server_port,
        checkpoint_sha256=settings.checkpoint_sha256,
        timeout_s=settings.inference_timeout_s,
        checkpoint_profile=settings.checkpoint_profile,
    ) as transport:
        result = run_rtc_policy_probe(settings, transport)
    print(
        "PASS v3 RTC policy round-trip: "
        f"bootstrap_s={result.bootstrap_s:.3f} "
        f"rtc_s={result.rtc_s:.3f} "
        f"prefix_steps={result.prefix_steps} "
        f"prefix_max_error={result.prefix_max_error:.8f}"
    )


if __name__ == "__main__":
    main()
