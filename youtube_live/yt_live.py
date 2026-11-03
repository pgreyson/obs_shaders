#!/usr/bin/env python3
"""yt_live.py — manage the YouTube live broadcast for the obs_shaders rig.

YouTube's "default stream key" flow in Studio is unreliable for long-running
unattended streams: once a broadcast ends, pushing RTMP to the same key often
produces nothing ("OBS says live, YouTube shows nothing") until the key is
reset by hand. This tool sidesteps Studio entirely and drives the YouTube
Data API directly:

  * one reusable liveStream resource owned by this tool (its key is pushed
    into OBS automatically, so you never copy a stream key again)
  * one liveBroadcast per "session"; `restart` ends the current one and
    starts a fresh one, so each edit day gets a clean video + thumbnail
  * thumbnails are grabbed straight from OBS's program output (left eye of
    the full-SBS frame by default) and uploaded as the custom thumbnail
  * `watch` runs as a daemon: if OBS is streaming and nothing is live on
    YouTube, it goes live; it can also refresh the thumbnail every N hours.

Commands:
  auth        one-time OAuth consent (opens a browser), stores a refresh token
  status      OBS output state + YouTube stream health + active/upcoming broadcasts
  go-live     ensure a broadcast is live on the stream OBS is pushing (idempotent)
  restart     end the live broadcast and go live again with a fresh thumbnail
  thumbnail   grab a new thumbnail from OBS and set it on the live broadcast
  end         end the live broadcast (--stop-obs also stops OBS streaming)
  watch       daemon loop around go-live + periodic thumbnail refresh

See README.md for the one-time Google Cloud setup.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import io
import json
import logging
import os
import signal
import sys
import time
import warnings
from pathlib import Path

# google-auth nags about Python 3.9 EOL and urllib3 about LibreSSL on every run.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import simpleobsws  # noqa: E402
import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image

HERE = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
RTMPS_INGEST = "rtmps://a.rtmps.youtube.com:443/live2"

log = logging.getLogger("yt_live")


# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------

DEFAULTS = {
    "obs": {"host": "localhost", "port": 4457, "password": ""},
    "credentials_dir": "~/.config/obs_shaders",
    "stream": {"title": "obs_shaders persistent"},
    "broadcast": {
        "title": "Live {date}",
        "description": "",
        "privacy": "public",
        "latency": "normal",
        "dvr": True,
        "record_from_start": True,
        "made_for_kids": False,
        "projection": None,
        "stereo_layout": None,
    },
    "thumbnail": {"source": "left", "width": 1280, "quality": 90},
    "watch": {"interval_s": 60, "refresh_thumbnail_hours": 0, "start_obs": False},
}


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> dict:
    cfg = {}
    if path.exists():
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    else:
        log.warning(f"config {path} not found; using defaults")
    cfg = deep_merge(DEFAULTS, cfg)
    cfg["credentials_dir"] = str(Path(cfg["credentials_dir"]).expanduser())
    return cfg


# ----------------------------------------------------------------------------
# OBS (obs-websocket v5 via simpleobsws)
# ----------------------------------------------------------------------------


class OBS:
    def __init__(self, cfg: dict):
        self.cfg = cfg["obs"]
        self.ws: simpleobsws.WebSocketClient | None = None

    async def __aenter__(self):
        url = f"ws://{self.cfg['host']}:{self.cfg['port']}"
        self.ws = simpleobsws.WebSocketClient(url=url, password=self.cfg["password"] or None)
        await self.ws.connect()
        if not await self.ws.wait_until_identified(timeout=10):
            raise RuntimeError(f"could not identify with obs-websocket at {url} (bad password?)")
        return self

    async def __aexit__(self, *exc):
        if self.ws:
            await self.ws.disconnect()

    async def call(self, name: str, data: dict | None = None) -> dict:
        r = await self.ws.call(simpleobsws.Request(name, data or {}))
        if not r.ok():
            raise RuntimeError(f"OBS {name} failed: {r.requestStatus.comment}")
        return r.responseData or {}

    async def stream_status(self) -> dict:
        return await self.call("GetStreamStatus")

    async def streaming(self) -> bool:
        return bool((await self.stream_status()).get("outputActive"))

    async def service_settings(self) -> dict:
        return (await self.call("GetStreamServiceSettings"))["streamServiceSettings"]

    async def set_stream_key(self, key: str, server: str = RTMPS_INGEST) -> None:
        settings = await self.service_settings()
        settings.update({"key": key, "server": server, "service": "YouTube - RTMPS", "protocol": "RTMPS"})
        await self.call(
            "SetStreamServiceSettings",
            {"streamServiceType": "rtmp_common", "streamServiceSettings": settings},
        )

    async def start_stream(self) -> None:
        if not await self.streaming():
            await self.call("StartStream")
            for _ in range(30):
                await asyncio.sleep(1)
                if await self.streaming():
                    return
            raise RuntimeError("OBS StartStream did not become active within 30s")

    async def stop_stream(self) -> None:
        if await self.streaming():
            await self.call("StopStream")
            for _ in range(30):
                await asyncio.sleep(1)
                if not await self.streaming():
                    return

    async def program_screenshot(self) -> Image.Image:
        scene = await self.call("GetCurrentProgramScene")
        name = scene.get("sceneName") or scene.get("currentProgramSceneName")
        vid = await self.call("GetVideoSettings")
        shot = await self.call(
            "GetSourceScreenshot",
            {
                "sourceName": name,
                "imageFormat": "png",
                "imageWidth": vid["outputWidth"],
                "imageHeight": vid["outputHeight"],
            },
        )
        b64 = shot["imageData"].split(",", 1)[1]
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def make_thumbnail(img: Image.Image, cfg: dict) -> bytes:
    """Crop one eye out of a full-SBS frame and encode as JPEG (<2MB for YouTube).

    The YouTube instance letterboxes the 32:9 SBS frame inside 16:9, so first trim
    pure-black bars (only if what remains is full-width and a sane height, so a
    momentarily dark patch doesn't produce a sliver).
    """
    w, h = img.size
    bbox = img.convert("L").point(lambda v: 255 if v > 4 else 0).getbbox()
    if bbox:
        x0, y0, x1, y1 = bbox
        if x0 <= 2 and x1 >= w - 2 and (y1 - y0) >= 0.4 * h:
            img = img.crop((0, y0, w, y1))
            w, h = img.size
    src = cfg["thumbnail"]["source"]
    if src == "left":
        img = img.crop((0, 0, w // 2, h))
    elif src == "right":
        img = img.crop((w // 2, 0, w, h))
    elif src != "full":
        raise ValueError(f"thumbnail.source must be left|right|full, got {src!r}")
    tw = int(cfg["thumbnail"]["width"])
    th = round(tw * img.size[1] / img.size[0])
    img = img.resize((tw, th), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=int(cfg["thumbnail"]["quality"]), optimize=True)
    data = buf.getvalue()
    if len(data) > 2_000_000:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70, optimize=True)
        data = buf.getvalue()
    return data


# ----------------------------------------------------------------------------
# YouTube
# ----------------------------------------------------------------------------


class YouTube:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.cred_dir = Path(cfg["credentials_dir"])
        self.secret_path = self.cred_dir / "client_secret.json"
        self.token_path = self.cred_dir / "youtube_token.json"
        self.api = None

    # -- auth --------------------------------------------------------------
    def authorize(self, interactive: bool = False) -> None:
        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save(creds)
        if not creds or not creds.valid:
            if not interactive:
                raise SystemExit(
                    f"no valid YouTube token at {self.token_path}; run `yt_live.py auth` first"
                )
            if not self.secret_path.exists():
                raise SystemExit(
                    f"missing {self.secret_path}\n"
                    "Create an OAuth 'Desktop app' client in Google Cloud and save its JSON there "
                    "(see README.md)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(self.secret_path), SCOPES)
            creds = flow.run_local_server(port=0, prompt="consent")
            self._save(creds)
        self.api = build("youtube", "v3", credentials=creds, cache_discovery=False)

    def _save(self, creds: Credentials) -> None:
        self.cred_dir.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        os.chmod(self.token_path, 0o600)

    # -- streams -----------------------------------------------------------
    def list_streams(self) -> list[dict]:
        r = self.api.liveStreams().list(part="id,snippet,cdn,status", mine=True, maxResults=50).execute()
        return r.get("items", [])

    def stream_by_key(self, key: str) -> dict | None:
        for s in self.list_streams():
            if s["cdn"]["ingestionInfo"]["streamName"] == key:
                return s
        return None

    def get_stream(self, stream_id: str) -> dict | None:
        r = self.api.liveStreams().list(part="id,snippet,cdn,status", id=stream_id).execute()
        items = r.get("items", [])
        return items[0] if items else None

    def ensure_stream(self, obs_key: str) -> dict:
        """Return the liveStream OBS should push to.

        Prefer whatever stream already matches the key in OBS (no disruption).
        Otherwise find/create the tool's own reusable stream.
        """
        s = self.stream_by_key(obs_key) if obs_key else None
        if s:
            return s
        title = self.cfg["stream"]["title"]
        for s in self.list_streams():
            if s["snippet"]["title"] == title and s["cdn"]["ingestionType"] == "rtmp":
                return s
        log.info(f"creating reusable liveStream '{title}'")
        return (
            self.api.liveStreams()
            .insert(
                part="snippet,cdn,contentDetails,status",
                body={
                    "snippet": {"title": title},
                    "cdn": {"frameRate": "variable", "ingestionType": "rtmp", "resolution": "variable"},
                    "contentDetails": {"isReusable": True},
                },
            )
            .execute()
        )

    # -- broadcasts --------------------------------------------------------
    def list_broadcasts(self, status: str) -> list[dict]:
        """status: active | upcoming | completed | all. Includes persistent (Studio) broadcasts."""
        r = (
            self.api.liveBroadcasts()
            .list(
                part="id,snippet,status,contentDetails",
                broadcastStatus=status,
                broadcastType="all",
                maxResults=25,
            )
            .execute()
        )
        return r.get("items", [])

    def current_broadcast(self, stream_id: str | None = None) -> dict | None:
        """The broadcast that is live/testing (optionally only if bound to stream_id)."""
        for b in self.list_broadcasts("active"):
            if b["status"]["lifeCycleStatus"] in ("live", "testing", "liveStarting"):
                if stream_id is None or b["contentDetails"].get("boundStreamId") == stream_id:
                    return b
        return None

    def pending_broadcast(self, stream_id: str) -> dict | None:
        """An upcoming broadcast already bound to our stream (e.g. one Studio created)."""
        for b in self.list_broadcasts("upcoming"):
            if b["contentDetails"].get("boundStreamId") == stream_id and b["status"][
                "lifeCycleStatus"
            ] in ("created", "ready"):
                return b
        return None

    def create_broadcast(self, stream_id: str) -> dict:
        bc = self.cfg["broadcast"]
        now = dt.datetime.now(dt.timezone.utc)
        title = bc["title"].format(date=now.astimezone().strftime("%Y-%m-%d"))
        content = {
            "enableAutoStart": False,
            "enableAutoStop": False,
            "enableDvr": bool(bc["dvr"]),
            "recordFromStart": bool(bc["record_from_start"]),
            "monitorStream": {"enableMonitorStream": False},
            "latencyPreference": bc["latency"],
        }
        if bc.get("projection"):
            content["projection"] = bc["projection"]
        if bc.get("stereo_layout"):
            content["stereoLayout"] = bc["stereo_layout"]
        body = {
            "snippet": {
                "title": title,
                "description": bc.get("description") or "",
                "scheduledStartTime": now.isoformat().replace("+00:00", "Z"),
            },
            "status": {
                "privacyStatus": bc["privacy"],
                "selfDeclaredMadeForKids": bool(bc["made_for_kids"]),
            },
            "contentDetails": content,
        }
        log.info(f"creating broadcast '{title}' ({bc['privacy']})")
        b = self.api.liveBroadcasts().insert(part="snippet,status,contentDetails", body=body).execute()
        b = (
            self.api.liveBroadcasts()
            .bind(part="id,snippet,status,contentDetails", id=b["id"], streamId=stream_id)
            .execute()
        )
        log.info(f"broadcast {b['id']} bound to stream {stream_id}")
        return b

    def wait_stream_active(self, stream_id: str, timeout: int = 90) -> None:
        deadline = time.time() + timeout
        while True:
            s = self.get_stream(stream_id)
            st = s["status"]["streamStatus"] if s else "?"
            if st == "active":
                return
            if time.time() > deadline:
                raise RuntimeError(f"stream {stream_id} is '{st}', not active — is OBS pushing to this key?")
            log.info(f"waiting for stream to become active (now '{st}')...")
            time.sleep(5)

    def transition(self, broadcast_id: str, status: str) -> dict:
        """status: testing | live | complete. Tolerates redundant transitions."""
        try:
            b = (
                self.api.liveBroadcasts()
                .transition(part="id,status", id=broadcast_id, broadcastStatus=status)
                .execute()
            )
            log.info(f"broadcast {broadcast_id} → {status}")
            return b
        except HttpError as e:
            reason = _reason(e)
            if reason == "redundantTransition":
                log.info(f"broadcast {broadcast_id} already {status}")
                return self.get_broadcast(broadcast_id)
            raise

    def get_broadcast(self, broadcast_id: str) -> dict | None:
        r = (
            self.api.liveBroadcasts()
            .list(part="id,snippet,status,contentDetails", id=broadcast_id)
            .execute()
        )
        items = r.get("items", [])
        return items[0] if items else None

    def wait_lifecycle(self, broadcast_id: str, target: str, timeout: int = 60) -> dict:
        deadline = time.time() + timeout
        while True:
            b = self.get_broadcast(broadcast_id)
            st = b["status"]["lifeCycleStatus"]
            if st == target:
                return b
            if time.time() > deadline:
                log.warning(f"broadcast {broadcast_id} is '{st}' after {timeout}s (wanted {target})")
                return b
            time.sleep(3)

    def set_thumbnail(self, broadcast_id: str, jpeg: bytes) -> None:
        media = MediaIoBaseUpload(io.BytesIO(jpeg), mimetype="image/jpeg", resumable=False)
        self.api.thumbnails().set(videoId=broadcast_id, media_body=media).execute()
        log.info(f"thumbnail set on {broadcast_id} ({len(jpeg)//1024} KB)")


def _reason(e: HttpError) -> str:
    try:
        return json.loads(e.content)["error"]["errors"][0]["reason"]
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# operations
# ----------------------------------------------------------------------------


async def grab_thumbnail(cfg: dict) -> bytes:
    async with OBS(cfg) as obs:
        img = await obs.program_screenshot()
    return make_thumbnail(img, cfg)


async def ensure_obs_pushing(cfg: dict, yt: YouTube, start_obs: bool) -> dict:
    """Make sure OBS is streaming to a key YouTube knows about. Returns the liveStream."""
    async with OBS(cfg) as obs:
        settings = await obs.service_settings()
        stream = yt.ensure_stream(settings.get("key", ""))
        key = stream["cdn"]["ingestionInfo"]["streamName"]
        server = stream["cdn"]["ingestionInfo"].get("rtmpsIngestionAddress") or RTMPS_INGEST
        was_streaming = await obs.streaming()
        if settings.get("key") != key or settings.get("server") != server:
            log.info(f"updating OBS stream key to liveStream '{stream['snippet']['title']}'")
            if was_streaming:
                await obs.stop_stream()
            await obs.set_stream_key(key, server)
            if was_streaming or start_obs:
                await obs.start_stream()
        elif not was_streaming:
            if start_obs:
                log.info("OBS is not streaming; starting")
                await obs.start_stream()
            else:
                raise RuntimeError("OBS is not streaming (use --start-obs or press Start Streaming)")
    return stream


def go_live(cfg: dict, yt: YouTube, start_obs: bool, with_thumbnail: bool = True) -> dict:
    stream = asyncio.run(ensure_obs_pushing(cfg, yt, start_obs))
    sid = stream["id"]

    b = yt.current_broadcast(sid)
    if b and b["status"]["lifeCycleStatus"] == "live":
        log.info(f"already live: https://youtu.be/{b['id']}  '{b['snippet']['title']}'")
        return b

    if not b:
        b = yt.pending_broadcast(sid)
        if b:
            log.info(f"using existing pending broadcast {b['id']} '{b['snippet']['title']}'")
        else:
            b = yt.create_broadcast(sid)

    yt.wait_stream_active(sid)
    # monitorStream is disabled on broadcasts we create, so ready → live is one hop.
    # Studio-created ones may have it enabled, in which case go via testing.
    try:
        yt.transition(b["id"], "live")
    except HttpError as e:
        if _reason(e) == "invalidTransition" and b["status"]["lifeCycleStatus"] != "testing":
            yt.transition(b["id"], "testing")
            yt.wait_lifecycle(b["id"], "testing")
            yt.transition(b["id"], "live")
        else:
            raise
    b = yt.wait_lifecycle(b["id"], "live")
    log.info(f"LIVE: https://youtu.be/{b['id']}  '{b['snippet']['title']}'")

    if with_thumbnail:
        try:
            yt.set_thumbnail(b["id"], asyncio.run(grab_thumbnail(cfg)))
        except Exception as e:  # thumbnail is cosmetic; never fail go-live on it
            log.warning(f"thumbnail failed: {e}")
    return b


def end_live(cfg: dict, yt: YouTube, stop_obs: bool) -> dict | None:
    b = yt.current_broadcast()
    if b:
        yt.transition(b["id"], "complete")
        log.info(f"ended broadcast {b['id']} '{b['snippet']['title']}'")
    else:
        log.info("no live broadcast to end")
    if stop_obs:
        asyncio.run(_stop_obs(cfg))
    return b


async def _stop_obs(cfg: dict) -> None:
    async with OBS(cfg) as obs:
        await obs.stop_stream()
        log.info("OBS streaming stopped")


def cmd_status(cfg: dict, yt: YouTube) -> None:
    async def obs_part():
        try:
            async with OBS(cfg) as obs:
                st = await obs.stream_status()
                svc = await obs.service_settings()
                return st, svc
        except Exception as e:
            return None, str(e)

    st, svc = asyncio.run(obs_part())
    print("== OBS")
    if st is None:
        print(f"  unreachable: {svc}")
        obs_key = ""
    else:
        obs_key = svc.get("key", "")
        print(
            f"  streaming={st['outputActive']} duration={st.get('outputTimecode')} "
            f"reconnecting={st.get('outputReconnecting')} skipped={st.get('outputSkippedFrames')}"
        )
        print(f"  service={svc.get('service')} server={svc.get('server')} key=…{obs_key[-4:]}")

    print("== YouTube liveStreams (mine)")
    streams = yt.list_streams()
    for s in streams:
        key = s["cdn"]["ingestionInfo"]["streamName"]
        mark = "<- OBS" if key == obs_key else ""
        hs = s["status"].get("healthStatus", {}).get("status", "?")
        print(
            f"  {s['id']}  '{s['snippet']['title']}'  status={s['status']['streamStatus']} "
            f"health={hs} key=…{key[-4:]} {mark}"
        )
    if not streams:
        print("  (none)")

    for status in ("active", "upcoming"):
        items = yt.list_broadcasts(status)
        print(f"== YouTube broadcasts ({status})")
        for b in items:
            print(
                f"  {b['id']}  '{b['snippet']['title']}'  lifecycle={b['status']['lifeCycleStatus']} "
                f"privacy={b['status']['privacyStatus']} bound={b['contentDetails'].get('boundStreamId')} "
                f"https://youtu.be/{b['id']}"
            )
        if not items:
            print("  (none)")


def cmd_watch(cfg: dict, yt: YouTube) -> None:
    w = cfg["watch"]
    interval = int(w["interval_s"])
    refresh_s = float(w["refresh_thumbnail_hours"]) * 3600
    last_thumb = 0.0
    stop = {"flag": False}

    def _sig(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    log.info(f"watch: every {interval}s; thumbnail refresh every {refresh_s/3600:.1f}h" if refresh_s else
             f"watch: every {interval}s; no thumbnail refresh")

    while not stop["flag"]:
        try:
            streaming = asyncio.run(_obs_streaming(cfg))
            if not streaming and not w["start_obs"]:
                log.info("OBS not streaming; idle")
            else:
                b = yt.current_broadcast()
                if not b or b["status"]["lifeCycleStatus"] != "live":
                    log.info("no live broadcast; going live")
                    go_live(cfg, yt, start_obs=bool(w["start_obs"]))
                    last_thumb = time.time()
                elif refresh_s and time.time() - last_thumb > refresh_s:
                    yt.set_thumbnail(b["id"], asyncio.run(grab_thumbnail(cfg)))
                    last_thumb = time.time()
                else:
                    log.info(f"live ok: https://youtu.be/{b['id']}")
        except Exception as e:
            log.error(f"watch iteration failed: {e}")
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("watch: bye.")


async def _obs_streaming(cfg: dict) -> bool:
    async with OBS(cfg) as obs:
        return await obs.streaming()


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(HERE / "config.yaml"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth")
    sub.add_parser("status")
    sp = sub.add_parser("go-live")
    sp.add_argument("--start-obs", action="store_true", help="press Start Streaming in OBS if needed")
    sp.add_argument("--no-thumbnail", action="store_true")
    sp = sub.add_parser("restart")
    sp.add_argument("--start-obs", action="store_true")
    sp.add_argument("--no-thumbnail", action="store_true")
    sp = sub.add_parser("thumbnail")
    sp.add_argument("--save", metavar="PATH", help="also write the JPEG here")
    sp = sub.add_parser("end")
    sp.add_argument("--stop-obs", action="store_true", help="also stop OBS streaming")
    sub.add_parser("watch")
    a = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(name)-8s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    cfg = load_config(Path(a.config))
    yt = YouTube(cfg)

    if a.cmd == "auth":
        yt.authorize(interactive=True)
        ch = yt.api.channels().list(part="snippet", mine=True).execute()["items"][0]
        print(f"authorized as channel '{ch['snippet']['title']}' ({ch['id']}); token saved to {yt.token_path}")
        return 0

    yt.authorize()
    if a.cmd == "status":
        cmd_status(cfg, yt)
    elif a.cmd == "go-live":
        go_live(cfg, yt, a.start_obs, not a.no_thumbnail)
    elif a.cmd == "restart":
        end_live(cfg, yt, stop_obs=False)
        time.sleep(5)  # let YouTube settle the completed broadcast before rebinding
        go_live(cfg, yt, a.start_obs, not a.no_thumbnail)
    elif a.cmd == "thumbnail":
        jpeg = asyncio.run(grab_thumbnail(cfg))
        if a.save:
            Path(a.save).write_bytes(jpeg)
            log.info(f"wrote {a.save}")
        b = yt.current_broadcast()
        if not b:
            log.error("no live broadcast to set a thumbnail on")
            return 1
        yt.set_thumbnail(b["id"], jpeg)
    elif a.cmd == "end":
        end_live(cfg, yt, a.stop_obs)
    elif a.cmd == "watch":
        cmd_watch(cfg, yt)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HttpError as e:
        log.error(f"YouTube API error: {e.resp.status} {_reason(e)}: {e}")
        sys.exit(2)
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)
