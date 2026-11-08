#!/usr/bin/env python3
"""
control_bridge/bridge.py — CV/USB → obs-websocket bridge.

Reads DC-coupled CV from a Rebel Technology OWL-ACDC USB interface and pushes
the values to mapped shader-filter properties in both OBS instances.

v2: actually writes filter-property values from CV (using SetSourceFilterSettings)
in addition to the v1 plumbing. Bidirectional GUI mirror via event subscription
is a planned v2.5 follow-on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import sys
import threading
import time
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
) -> dict | None:
    req = simpleobsws.Request(
        "GetSourceFilter", {"sourceName": source, "filterName": filter_name}
    )
    resp = await ws.call(req)
    if not resp.ok():
        log.warning(
            f"[{name}] couldn't read filter '{filter_name}' on '{source}': "
            f"{resp.responseData}"
        )
        return None
    return resp.responseData.get("filterSettings", {})


async def write_filter_property(
    ws: simpleobsws.WebSocketClient,
    source: str,
    filter_name: str,
    prop: str,
    value: float,
) -> bool:
    req = simpleobsws.Request(
        "SetSourceFilterSettings",
        {
            "sourceName": source,
            "filterName": filter_name,
            "filterSettings": {prop: value},
            "overlay": True,  # merge with existing settings instead of replacing
        },
    )
    resp = await ws.call(req)
    return resp.ok()


class CameraAlign:
    """Scale the stereo splat so the synth's depth range occupies the SAME
    screen-disparity range as the live stereo camera (depth_sidecar publishes
    the camera's displayed disparity percentiles). The CV knob becomes the
    FRACTION of that range the synth uses (0 = flat, 1 = exact match); if the
    stats go stale (camera off, sidecar down) the knob falls back to driving
    the splat depth directly, as before.

      camera screen disparity  = canvas_scale * d_src            (px, L-R)
      splat screen disparity   = 2 * (dt - c) * f0 * eye_px,  f0 = depth^2 * max_parallax
      match dt=0 -> p5, dt=1 -> p95:
        f0 = canvas_scale * (p95 - p5) / (2 * eye_px),   c = -p5 / (p95 - p5)
    """

    def __init__(self, cfg: dict):
        self.path = cfg.get("stats_file", "/tmp/stereopi_depth_stats.json")
        self.max_age = float(cfg.get("max_age_s", 3.0))
        self.canvas_scale = float(cfg.get("canvas_scale", 2.0))   # camera 1920 -> 3840 canvas
        self.eye_px = float(cfg.get("eye_px", 1920))
        self.max_parallax = float(cfg.get("max_parallax", 0.08))
        self.conv_targets = cfg.get("convergence_targets", [])
        self._mtime = None
        self._stats = None
        self.active = False
        self._last_log = 0.0

    def stats(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        if time.time() - st.st_mtime > self.max_age:
            return None
        if st.st_mtime != self._mtime:
            try:
                with open(self.path) as fh:
                    self._stats = json.load(fh)
                self._mtime = st.st_mtime
            except (OSError, ValueError):
                return None
        return self._stats

    def solve(self, knob: float):
        """-> (depth, convergence) or None when stats are stale."""
        st = self.stats()
        if st is None:
            if self.active:
                log.info("camera_align: stats stale -> knob drives splat depth directly")
            self.active = False
            return None
        p5, p95 = float(st["p5"]), float(st["p95"])
        span = max(p95 - p5, 0.5)
        f0 = self.canvas_scale * span / (2.0 * self.eye_px)
        c = -p5 / span
        c_cl = min(1.0, max(0.0, c))
        depth = math.sqrt(max(0.0, knob) * f0 / self.max_parallax)
        depth_cl = min(1.0, depth)
        now = time.time()
        if not self.active or now - self._last_log > 10:
            self.active = True
            self._last_log = now
            log.info(f"camera_align: cam disp p5 {p5:+.1f} p95 {p95:+.1f} src px -> "
                     f"f0={f0:.4f} depth={depth_cl:.3f} (knob {knob:.2f}) conv={c_cl:.3f}"
                     + (" [conv clamped]" if c != c_cl else "") + (" [depth clamped]" if depth != depth_cl else ""))
        return depth_cl, c_cl


def clamp_and_remap(v: float, cv_range: tuple[float, float], out_range: tuple[float, float]) -> float:
    cv_lo, cv_hi = cv_range
    out_lo, out_hi = out_range
    t = (v - cv_lo) / (cv_hi - cv_lo) if cv_hi != cv_lo else 0.0
    t = max(0.0, min(1.0, t))
    return out_lo + t * (out_hi - out_lo)


async def _write_filter_batch(
    name: str,
    ws: simpleobsws.WebSocketClient,
    source: str,
    filter_name: str,
    settings: dict,
) -> None:
    """Push multiple property values to one filter in a single obs-websocket call."""
    req = simpleobsws.Request(
        "SetSourceFilterSettings",
        {
            "sourceName": source,
            "filterName": filter_name,
            "filterSettings": settings,
            "overlay": True,
        },
    )
    try:
        resp = await ws.call(req)
        if not resp.ok():
            log.warning(f"[{name}] batch write failed for {filter_name}: {settings}")
    except Exception as exc:
        log.error(f"[{name}] batch write error: {exc}")


async def cv_writer_loop(
    latest_means: np.ndarray,
    latest_lock: threading.Lock,
    mappings: list[dict],
    obs_clients: dict[str, simpleobsws.WebSocketClient],
    write_hz: float,
    write_threshold: float,
    stop_event: asyncio.Event,
) -> None:
    """Periodically push the latest CV value of each mapped channel to OBS.

    Each pass groups all property writes targeting the same (instance, source,
    filter) into a single SetSourceFilterSettings call, then dispatches the
    group-writes for all instances in parallel via asyncio.gather. Total
    obs-websocket round-trips per pass = number of distinct (instance, source,
    filter) tuples, executed concurrently. Was ~6 sequential round-trips
    before, now typically ~2 parallel.
    """
    last_written: dict[tuple, float] = {}
    interval = 1.0 / write_hz
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # normal — wake up and do a write pass

        with latest_lock:
            means = latest_means.copy()

        # Group property writes by (instance, source, filter) so each filter
        # gets one SetSourceFilterSettings call per pass with all changed
        # properties merged.
        batched: dict[tuple, dict] = {}  # (inst, source, filter) -> {prop: value}
        for mapping in mappings:
            ch = mapping["cv_channel"]
            cv_range = tuple(mapping.get("cv_range", [0.0, 1.0]))
            out_range = tuple(mapping.get("out_range", [0.0, 1.0]))
            v = clamp_and_remap(float(means[ch]), cv_range, out_range)
            writes = [(t, v) for t in mapping.get("targets", [])]
            align = mapping.get("_align")
            if align is not None:
                solved = align.solve(v)
                if solved is not None:
                    v, conv = solved
                    writes = [(t, v) for t in mapping.get("targets", [])] + \
                             [(t, conv) for t in align.conv_targets]
            for target, val in writes:
                inst = target["instance"]
                if inst not in obs_clients:
                    continue
                prop_key = (inst, target["source"], target["filter"], target["property"])
                prev = last_written.get(prop_key)
                if prev is not None and abs(prev - val) < write_threshold:
                    continue
                last_written[prop_key] = val
                group_key = (inst, target["source"], target["filter"])
                batched.setdefault(group_key, {})[target["property"]] = val

        if not batched:
            continue

        await asyncio.gather(*(
            _write_filter_batch(inst, obs_clients[inst], source, filter_name, settings)
            for (inst, source, filter_name), settings in batched.items()
        ))


async def main_async(config: dict) -> None:
    for mapping in config.get("mappings", []):
        if "camera_align" in mapping:
            mapping["_align"] = CameraAlign(mapping["camera_align"])
            log.info(f"[{mapping.get('name', '?')}] camera_align enabled "
                     f"(stats {mapping['_align'].path}, max age {mapping['_align'].max_age}s)")
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

    # Dump current values for each unique (instance, source, filter) the config
    # touches, so we know our names match the OBS scene.
    seen: set[tuple[str, str, str]] = set()
    for mapping in config.get("mappings", []):
        for target in mapping.get("targets", []):
            inst = target["instance"]
            if inst not in obs_clients:
                continue
            key = (inst, target["source"], target["filter"])
            if key in seen:
                continue
            seen.add(key)
            settings = await read_filter_settings(
                inst, obs_clients[inst], target["source"], target["filter"]
            )
            if settings is not None:
                log.info(f"[{inst}] {target['source']}/{target['filter']} → {settings}")

    cv_cfg = config["cv"]
    device_idx = find_cv_device(cv_cfg["device_name_match"])
    sample_rate = cv_cfg.get("sample_rate", 48000)
    channels = cv_cfg.get("channels", 4)
    poll_hz = cv_cfg.get("poll_hz", 30)
    change_threshold = cv_cfg.get("change_threshold", 0.01)
    write_hz = cv_cfg.get("write_hz", poll_hz)
    write_threshold = cv_cfg.get("write_threshold", 0.005)
    block_size = max(1, sample_rate // poll_hz)

    log.info(
        f"CV stream: device=[{device_idx}], sr={sample_rate}, channels={channels}, "
        f"block={block_size} ({poll_hz} Hz polling, {write_hz} Hz writes)"
    )

    latest_means = np.zeros(channels, dtype=np.float32)
    latest_lock = threading.Lock()
    last_reported = np.zeros(channels, dtype=np.float32)
    heartbeat_counter = [0]
    heartbeat_every = max(1, poll_hz * 5)  # full snapshot every ~5s

    def audio_callback(indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.warning(f"audio status: {status}")
        means = indata.mean(axis=0)
        with latest_lock:
            latest_means[:] = means
        for ch in range(channels):
            v = float(means[ch])
            if abs(v - last_reported[ch]) > change_threshold:
                last_reported[ch] = v
                log.info(f"CV ch{ch}: {v:+.3f}")
        heartbeat_counter[0] += 1
        if heartbeat_counter[0] % heartbeat_every == 0:
            with latest_lock:
                snap = " ".join(
                    f"ch{c}={latest_means[c]:+.3f}" for c in range(channels)
                )
            log.info(f"CV heartbeat: {snap}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    writer_task = asyncio.create_task(
        cv_writer_loop(
            latest_means,
            latest_lock,
            config.get("mappings", []),
            obs_clients,
            write_hz,
            write_threshold,
            stop_event,
        )
    )

    stream = sd.InputStream(
        device=device_idx,
        channels=channels,
        samplerate=sample_rate,
        blocksize=block_size,
        callback=audio_callback,
    )

    with stream:
        log.info("Bridge running. CV writes are live. Ctrl-C to exit.")
        await stop_event.wait()

    writer_task.cancel()
    try:
        await writer_task
    except asyncio.CancelledError:
        pass

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
