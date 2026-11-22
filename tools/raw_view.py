#!/usr/bin/env python3
"""raw_view.py — live view of the RAW Elgato input in a browser, bypassing OBS.

For setting up the analog->HDMI converter (resolution, colour standard) when the
picture OBS shows is wrong and you need to see what the capture card itself gets.

  depth_sidecar/.venv/bin/python3 tools/raw_view.py [--device N] [--port 8081] [--minutes 60]

Then open  http://<mac address>:8081  on any device on the LAN. The page shows
the picture (about 10 fps), the frame size the card delivers, and whether the
frame carries any colour. It opens the card as a second consumer (the Elgato
allows that) and exits by itself after --minutes so it is never left running.

Note: the size shown is what the Elgato hands to the Mac. The card scales its
HDMI input to that, so it is not necessarily the converter's output resolution.
"""
import argparse, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("OPENCV_AVFOUNDATION_SKIP_AUTH", "1")
import cv2  # noqa: E402

PAGE = b"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Raw Elgato input</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:16px -apple-system,sans-serif}
 img{display:block;width:100%;max-height:85vh;object-fit:contain;background:#000}
 p{margin:10px 16px} b{color:#fff} .bad{color:#f66} .ok{color:#6d6}
</style>
<img src="/stream">
<p id=i>waiting for the capture card...</p>
<p><a href="/frame.png" style="color:#8bf">Full-size still</a></p>
<script>
async function tick(){
  try{
    const s = await (await fetch('/info')).json();
    document.getElementById('i').innerHTML = s.w ?
      '<b>'+s.w+' x '+s.h+'</b> from the card &middot; '+s.fps.toFixed(0)+' fps read &middot; colour: '+
      (s.colour ? '<span class=ok>yes</span>' : '<span class=bad>none (monochrome)</span>')+
      ' &middot; brightness '+s.luma.toFixed(0)+'/255' : 'no frames from the capture card';
  }catch(e){ document.getElementById('i').textContent = 'viewer stopped'; }
}
setInterval(tick, 1000); tick();
</script>"""


class State:
    frame = None
    info = {"w": 0, "h": 0, "fps": 0.0, "colour": False, "luma": 0.0}
    seq = 0
    cond = threading.Condition()


def pick_device():
    """First device that delivers frames and is not OBS's own 3840-wide virtual camera."""
    for i in range(6):
        cap = cv2.VideoCapture(i, cv2.CAP_AVFOUNDATION)
        ok, f = False, None
        for _ in range(10):
            ok, f = cap.read()
        if ok and f is not None and f.shape[1] != 3840:
            return i, cap
        cap.release()
    raise SystemExit("no capture device delivering frames (other than the OBS virtual camera)")


def capture(cap):
    t0, n = time.time(), 0
    while True:
        ok, f = cap.read()
        if not ok or f is None:
            State.info = dict(State.info, w=0, h=0)
            time.sleep(0.5)
            continue
        n += 1
        now = time.time()
        if now - t0 >= 1.0:
            small = f[::8, ::8].astype(int)
            spread = (small.max(axis=2) - small.min(axis=2)).mean()
            State.info = {"w": f.shape[1], "h": f.shape[0], "fps": n / (now - t0),
                          "colour": bool(spread > 1.5), "luma": float(small.mean())}
            t0, n = now, 0
        with State.cond:
            State.frame = f
            State.seq += 1
            State.cond.notify_all()
        time.sleep(0.08)          # about 10 fps: a setup view, keep the load off the rig


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            self._send(200, "text/html", PAGE)
        elif self.path == "/info":
            self._send(200, "application/json", json.dumps(State.info).encode())
        elif self.path == "/frame.png":
            f = State.frame
            if f is None:
                return self._send(503, "text/plain", b"no frame")
            self._send(200, "image/png", cv2.imencode(".png", f)[1].tobytes())
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = -1
            try:
                while True:
                    with State.cond:
                        State.cond.wait_for(lambda: State.seq != last, timeout=5)
                        last, f = State.seq, State.frame
                    if f is None:
                        continue
                    if f.shape[1] > 1280:
                        f = cv2.resize(f, (1280, f.shape[0] * 1280 // f.shape[1]))
                    jpg = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
                    self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                    self.wfile.write(jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="AVFoundation device index (default: auto)")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--minutes", type=float, default=60, help="exit after this long")
    a = ap.parse_args()
    if a.device is None:
        dev, cap = pick_device()
    else:
        dev, cap = a.device, cv2.VideoCapture(a.device, cv2.CAP_AVFOUNDATION)
    print(f"raw_view: device {dev}, http://0.0.0.0:{a.port}, exits in {a.minutes:g} min", flush=True)
    threading.Thread(target=capture, args=(cap,), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(a.minutes * 60)
    print("raw_view: time limit reached, exiting", flush=True)


if __name__ == "__main__":
    main()
