# youtube_live — unattended YouTube broadcast management

Drives the YouTube Data API directly so the long-running stream never depends on
YouTube Studio's "default stream key" behaviour (which silently stops producing a
broadcast after a previous one ends — "OBS says live, YouTube shows nothing").

What it owns:

* **The stream key.** One reusable `liveStream` on your channel. If OBS is already
  pushing to a key YouTube recognises, that stream is reused untouched; otherwise the
  tool's own key is written into OBS over obs-websocket. You never copy a key again.
* **Broadcasts.** One per session. `restart` ends the current broadcast and starts a
  fresh one (new video, new title date, new thumbnail) without stopping OBS.
* **Thumbnails.** Grabbed from OBS's program output (left eye of the full-SBS frame by
  default, 1280x720 JPEG) and uploaded as the custom thumbnail.
* **Watchdog.** `watch` keeps the channel live while OBS is streaming and can refresh
  the thumbnail every N hours.

## Which OBS instance

YouTube is fed by the third OBS instance, `full_sbs->youtube` (websocket :4457), a clone of
the Twitch relay: it captures the main instance's virtual camera and letterboxes the
3840x1080 full-SBS frame into 1920x1080 (scene `full_sbs`; `half_sbs` squashes instead).
`launch_dual.sh` starts it alongside the other two. The main instance no longer streams
anywhere; YouTube rejected its raw 32:9 frame (Studio sat on "preparing stream").

Thumbnails trim the letterbox bars geometrically (`thumbnail.source_aspect`, 32:9) before
cropping an eye, so they come out 16:9 regardless of how dark the picture's edges are.

## One-time setup (≈10 minutes, needs a browser)

1. Go to <https://console.cloud.google.com/> and create a project (any name, e.g. `obs-shaders`).
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen** → External → fill app name + your email →
   under **Test users** add the Google account that owns the YouTube channel. Leave the
   app in *Testing* (no verification needed for your own account; refresh tokens for
   Testing apps expire after 7 days unless you publish — click **Publish app** to avoid
   re-authing weekly; no review is required for the `youtube.force-ssl` scope on a
   self-only app, Google just shows an "unverified app" warning once).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   Application type **Desktop app** → Create → **Download JSON**.
5. Save it as `~/.config/obs_shaders/client_secret.json`.
6. Authorise once (opens a browser; pick the channel's Google account):

   ```bash
   cd /Users/paulgreyson/Dev/obs_shaders/youtube_live
   .venv/bin/python3 yt_live.py auth
   ```

   The refresh token lands in `~/.config/obs_shaders/youtube_token.json` (mode 600).

The channel must have live streaming enabled and be verified (phone) for custom
thumbnails; both are already true if you have streamed before.

## Day to day

```bash
cd /Users/paulgreyson/Dev/obs_shaders/youtube_live
Y=.venv/bin/python3

$Y yt_live.py status            # OBS output, YouTube stream health, live/upcoming broadcasts
$Y yt_live.py go-live           # make sure something is live on the stream OBS is pushing
$Y yt_live.py restart           # after an edit day: end broadcast, new one + fresh thumbnail
$Y yt_live.py thumbnail         # just re-grab the thumbnail (add --save x.jpg to inspect)
$Y yt_live.py thumbnail --no-upload --save ~/Desktop/yt_thumbnail.jpg   # no Google auth; upload by hand in Studio
$Y yt_live.py end --stop-obs    # end broadcast and stop OBS streaming
```

`go-live` and `restart` take `--start-obs` to press Start Streaming in OBS if it isn't.
Both are idempotent: running `go-live` while already live is a no-op.

### Watchdog (recommended)

Launch detached, the same way as the control bridge — Claude Code background tasks are
killed at session end:

```bash
cd /Users/paulgreyson/Dev/obs_shaders/youtube_live
nohup .venv/bin/python3 yt_live.py watch > /tmp/yt_live.log 2>&1 &
```

Every `watch.interval_s` it checks: OBS streaming? → is a broadcast live? → if not,
go live. With `watch.refresh_thumbnail_hours: 6` it also re-grabs the thumbnail every
6 h so the preview tracks what the patch has settled into. Stop with plain `kill`.

## Config

`config.yaml` (gitignored; see `config.example.yaml`). Notable knobs:

| key | meaning |
|---|---|
| `broadcast.title` | `{date}` expands to the local date; `restart` makes a new video each time |
| `broadcast.privacy` | `public` / `unlisted` / `private` |
| `broadcast.stereo_layout` | `left_right` tells YouTube the SBS frame is 3D (player shows one eye, offers 3D modes). `null` = raw SBS for everyone |
| `thumbnail.source` | `left` / `right` eye crop or `full` SBS frame |
| `watch.refresh_thumbnail_hours` | 0 = only on go-live/restart |

## Quota

YouTube Data API default quota is 10,000 units/day. `watch` at 60 s costs ~1,500/day
(list calls); a `restart` is ~250. Nowhere near the limit.

## How go-live decides what to do

1. Read OBS's stream key. Find the YouTube `liveStream` with that key, else find/create
   the tool's own (`stream.title`) and push its key into OBS (stop → set → start).
2. If a broadcast is already **live** on that stream → done.
3. Else reuse an **upcoming** broadcast bound to that stream (e.g. one Studio created and
   left in "ready"), else insert a new one and bind it.
4. Wait for the stream to report `active`, transition the broadcast to `live`
   (via `testing` first if YouTube insists), upload the thumbnail.
