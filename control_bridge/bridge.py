#!/usr/bin/env python3
"""
control_bridge/bridge.py — CV/USB → obs-websocket bridge.

Reads DC-coupled CV from a Rebel Technology OWL-ACDC USB interface and pushes
the values to mapped shader-filter properties in both OBS instances. Also
bidirectionally mirrors GUI changes so tweaking a slider in one OBS UI updates
the matching property in the other instance automatically.

v1 (this file): proves plumbing. Connects to both obs-websocket endpoints,
prints the current filter property values, opens an audio stream on the OWL-ACDC,
and prints CV channel changes. Does NOT yet write filter values from CV or
mirror GUI changes — that comes next, once we've confirmed the plumbing works.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import numpy as np
import simpleobsws
import sounddevice as sd
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-9s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def find_cv_device(name_match: str) -> int:
    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        if name_match.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
            log.info(
                f"Found CV device: [{idx}] {dev['name']} "
                f"({dev['max_input_channels']} in, {int(dev['default_samplerate'])} Hz)"
            )
            return idx
    log.error(f"No input device matched '{name_match}'. Available inputs:")
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            log.error(f"  [{idx}] {dev['name']} ({dev['max_input_channels']} in)")
    raise SystemExit(1)


async def connect_obs(
    name: str, host: str, port: int, password: str
) -> simpleobsws.WebSocketClient:
    url = f"ws://{host}:{port}"
    log.info(f"[{name}] connecting to {url}...")
    ws = simpleobsws.WebSocketClient(url=url, password=password)
    await ws.connect()
    await ws.wait_until_identified()
    log.info(f"[{name}] connected.")
    return ws


async def read_filter_settings(
    name: str, ws: simpleobsws.WebSocketClient, source: str, filter_name: str
) -> None:
    req = simpleobsws.Request(
        "GetSourceFilter", {"sourceName": source, "filterName": filter_name}
    )
    resp = await ws.call(req)
    if not resp.ok():
        log.warning(
            f"[{name}] couldn't read filter '{filter_name}' on '{source}': "
            f"{resp.responseData}"
        )
        return
    settings = resp.responseData.get("filterSettings", {})
    log.info(f"[{name}] {source}/{filter_name} settings: {settings}")


async def main_async(config: dict) -> None:
    # Connect to all configured OBS instances. Failures are non-fatal —
    # the bridge can still report CV values even if obs-websocket is down.
    obs_clients: dict[str, simpleobsws.WebSocketClient] = {}
    for inst_name, inst_cfg in config.get("obs_instances", {}).items():
        host = inst_cfg.get("host", "localhost")
        port = inst_cfg["port"]
        env_key = f"OBS_{inst_name.upper()}_PASSWORD"
        password = inst_cfg.get("password") or os.environ.get(env_key, "")
        try:
            obs_clients[inst_name] = await connect_obs(inst_name, host, port, password)
        except Exception as exc:
            log.error(f"[{inst_name}] failed to connect: {exc}")

    # For each mapping target, show the current filter property values so we
    # know we're pointing at the right filter / property name in each instance.
    for mapping in config.get("mappings", []):
        for target in mapping.get("targets", []):
            inst = target["instance"]
            if inst not in obs_clients:
                continue
            await read_filter_settings(
                inst, obs_clients[inst], target["source"], target["filter"]
            )

    # Open the CV input stream.
    cv_cfg = config["cv"]
    device_idx = find_cv_device(cv_cfg["device_name_match"])
    sample_rate = cv_cfg.get("sample_rate", 48000)
    channels = cv_cfg.get("channels", 4)
    poll_hz = cv_cfg.get("poll_hz", 30)
    change_threshold = cv_cfg.get("change_threshold", 0.01)
    block_size = max(1, sample_rate // poll_hz)

    log.info(
        f"Opening CV stream: device=[{device_idx}], sr={sample_rate}, "
        f"channels={channels}, block={block_size} ({poll_hz} Hz polling)"
    )

    last_reported = np.zeros(channels, dtype=np.float32)

    def audio_callback(indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.warning(f"audio status: {status}")
        means = indata.mean(axis=0)
        for ch in range(channels):
            v = float(means[ch])
            if abs(v - last_reported[ch]) > change_threshold:
                last_reported[ch] = v
                log.info(f"CV ch{ch}: {v:+.3f}")

    stream = sd.InputStream(
        device=device_idx,
        channels=channels,
        samplerate=sample_rate,
        blocksize=block_size,
        callback=audio_callback,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    with stream:
        log.info("Bridge running. Move CV inputs to see values. Ctrl-C to exit.")
        await stop_event.wait()

    log.info("Disconnecting OBS clients...")
    for ws in obs_clients.values():
        await ws.disconnect()
    log.info("Bye.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to YAML config (default: control_bridge/config.yaml).",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        example = config_path.with_name("config.example.yaml")
        if example.exists():
            log.error(
                f"{config_path} not found. Copy {example.name} to {config_path.name} "
                f"and edit, or pass --config explicitly."
            )
        else:
            log.error(f"{config_path} not found.")
        return 1

    asyncio.run(main_async(load_config(config_path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
