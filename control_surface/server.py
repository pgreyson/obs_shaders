#!/usr/bin/env python3
"""control_surface/server.py — one touch surface for every OBS filter parameter.

Serves a single page (index.html) and a websocket on one port. Open
http://<this mac>:8080/ on the iPad. The server is the only thing that talks
to OBS (ws :4455, password from control_bridge/config.yaml), so the password
never reaches the browser.

  model    built from the LIVE filter list of main: every filter on every source,
           enabled or not. Control ranges come from the shader file each filter
           points at (uniform annotations); native filters from info.yaml.
  text     info.yaml (grouping, short labels, info overlays) + the shader's own
           header and per-uniform comments.
  live     values are read back from OBS while a page is open, so CV-driven values
           and changes made in the OBS filter panel show up on the surface.

Run detached with the bridge's venv (it has simpleobsws + websockets + yaml):
  nohup control_bridge/.venv/bin/python3 control_surface/server.py > /tmp/control_surface.log 2>&1 &
There is no login: anyone on the same network can move the controls.
"""
import argparse
import asyncio
import json
import re
import socket
import time
from pathlib import Path

import simpleobsws
import yaml
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BRIDGE_CFG = REPO / "control_bridge" / "config.yaml"
BRIDGE_REBASE = ("127.0.0.1", 9456)   # control_bridge listens here (see bridge.py Rebase)
RELAYS = {"Twitch": 4456, "YouTube": 4457, "Projector": 4458}
SKIP_SOURCES = {"zz_save_nudge"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- shader files

def _num(s):
    s = s.strip().rstrip("f")
    if s in ("true", "false"):
        return s == "true"
    try:
        return float(s)
    except ValueError:
        return None


def parse_shader(path):
    """-> {'header': str, 'params': [ {key,type,label,min,max,step,default,comment} ]}"""
    text = Path(path).read_text(errors="replace")
    lines = text.split("\n")
    n_head = 0
    while n_head < len(lines) and lines[n_head].startswith("//"):
        n_head += 1
    header = "\n".join(re.sub(r"^// ?", "", l) for l in lines[:n_head]).strip()
    params = []
    for m in re.finditer(r"^uniform\s+(float|int|bool)\s+(\w+)\s*", text, re.M):
        typ, key = m.group(1), m.group(2)
        pos, ann = m.end(), ""
        if text[pos:pos + 1] == "<":
            j, inq = pos + 1, False
            while j < len(text):
                ch = text[j]
                if ch == '"':
                    inq = not inq
                elif ch == ">" and not inq:
                    break
                j += 1
            ann, pos = text[pos + 1:j], j + 1
        end = text.find(";", pos)
        dm = re.search(r"=\s*(.+)", text[pos:end] if end >= 0 else "")
        p = {"key": key, "type": typ, "label": key}
        for am in re.finditer(r'(string|float|int|bool)\s+(\w+)\s*=\s*(?:"([^"]*)"|([^;]+));', ann):
            name = am.group(2)
            if name == "label":
                p["label"] = am.group(3) or ""
            elif name in ("minimum", "maximum", "step"):
                v = _num(am.group(4) or "")
                if v is not None:
                    p[{"minimum": "min", "maximum": "max", "step": "step"}[name]] = v
        d = _num(dm.group(1)) if dm else None
        if typ == "bool":
            p["default"] = bool(d) if d is not None else False
        else:
            p.setdefault("min", 0.0)
            p.setdefault("max", 1.0)
            p.setdefault("step", 1.0 if typ == "int" else 0.01)
            p["default"] = d if d is not None else p["min"]
        # the comment block directly above the uniform (not the file header)
        ln = text.count("\n", 0, m.start())
        k = ln
        while k > 0 and lines[k - 1].startswith("//"):
            k -= 1
        if k < ln and k >= n_head:
            c = [re.sub(r"^// ?", "", l) for l in lines[k:ln]]
            c = [l for l in c if not re.match(r"^-{3,}.*-{3,}\s*$", l)]
            p["comment"] = "\n".join(c).strip()
        params.append(p)
    return {"header": header, "params": params}


# ---------------------------------------------------------------- OBS link

class Obs:
    """One obs-websocket connection that survives OBS being relaunched."""

    def __init__(self, port, password, on_event=None):
        self.url, self.password, self.on_event = f"ws://127.0.0.1:{port}", password, on_event
        self.ws, self.ok = None, False

    async def connect(self):
        await self.close()
        ws = simpleobsws.WebSocketClient(url=self.url, password=self.password)
        await asyncio.wait_for(ws.connect(), 4)
        await asyncio.wait_for(ws.wait_until_identified(), 4)
        if self.on_event:
            ws.register_event_callback(self.on_event)
        self.ws, self.ok = ws, True

    async def close(self):
        ws, self.ws, self.ok = self.ws, None, False
        if ws:
            try:
                await asyncio.wait_for(ws.disconnect(), 2)
            except Exception:
                pass

    async def call(self, typ, data=None):
        if not self.ws:
            raise ConnectionError("not connected")
        try:
            r = await asyncio.wait_for(self.ws.call(simpleobsws.Request(typ, data)), 5)
        except Exception:
            self.ok = False
            raise
        if not r.ok():
            raise RuntimeError(f"{typ}: {r.requestStatus.comment}")
        return r.responseData or {}


# ---------------------------------------------------------------- the surface

class Surface:
    def __init__(self, password):
        self.obs = Obs(4455, password, self.on_event)
        self.relays = {name: Obs(port, password) for name, port in RELAYS.items()}
        self.clients = set()
        self.model = None
        self.schema = {}     # (source, filter) -> {key: param}
        self.values = {}     # (source, filter) -> {key: value}
        self.pending = {}    # (source, filter) -> {key: value}   writes waiting to go to OBS
        self.dirty = {}      # (source, filter) -> {key: value}   changes waiting to go to pages
        self.status = {"obs": False}
        self.save_at = None
        self.wrote = {}      # ((source, filter), key) -> time of the last write from a page
        self.fast = set()    # filters with a CV-driven control (read back more often)
        self.rebuild = asyncio.Event()

    # ---- model -------------------------------------------------------------

    def load_text(self):
        info = yaml.safe_load((HERE / "info.yaml").read_text()) or {}
        cv = {}
        try:
            cfg = yaml.safe_load(BRIDGE_CFG.read_text())
            main_port = 4455
            for m in cfg.get("mappings", []):
                for t in m.get("targets", []):
                    inst = cfg["obs_instances"].get(t.get("instance"), {})
                    if inst.get("port") == main_port:
                        cv[(t["source"], t["filter"], t["property"])] = {
                            "ch": m["cv_channel"], "name": m.get("name", ""),
                            "range": m.get("out_range"),
                            "limits": m.get("limits") or sorted(m.get("out_range", [0, 1]))}
        except Exception as e:
            log("bridge config not read:", e)
        return info, cv

    def filter_params(self, kind, settings, info):
        """-> (params, filter text, shader header, shader file name)"""
        if kind == "shader_filter":
            path = settings.get("shader_file_name") or ""
            stem = Path(path).stem
            text = (info.get("shaders") or {}).get(stem, {})
            try:
                sh = parse_shader(path)
            except Exception as e:
                return [], f"Shader file could not be read: {e}", "", Path(path).name
            cur = {}
            for inc in text.get("include") or []:      # reuse another shader's control text
                for k, v in ((info["shaders"].get(inc["from"]) or {}).get("params") or {}).items():
                    v = dict(v)
                    if inc.get("section") and "section" not in v:
                        v["section"] = inc["section"]
                    cur[k] = v
            cur.update(text.get("params") or {})
            params = []
            for p in sh["params"]:
                c = cur.get(p["key"], {})
                p = dict(p)
                p["shader_label"] = p["label"]
                for k in ("label", "info", "section", "options"):
                    if k in c:
                        p[k] = c[k]
                params.append(p)
            return params, text.get("info", ""), sh["header"], Path(path).name
        nat = (info.get("native") or {}).get(kind)
        if nat:
            return [dict(p) for p in nat.get("params", [])], nat.get("info", ""), "", ""
        return [], "", "", ""

    async def build(self):
        info, cv = self.load_text()
        o = self.obs
        sl = await o.call("GetSceneList")
        program = sl.get("currentProgramSceneName")
        scenes = [s["sceneName"] for s in sl["scenes"]]
        names, layers = list(scenes), []
        for sc in scenes:
            items = (await o.call("GetSceneItemList", {"sceneName": sc})).get("sceneItems", [])
            for it in items:
                if it["sourceName"] not in names:
                    names.append(it["sourceName"])
            if sc == program:
                ltext = info.get("layers") or {}
                layers = [{"scene": sc, "id": it["sceneItemId"], "name": it["sourceName"],
                           "on": it["sceneItemEnabled"], "info": ltext.get(it["sourceName"], "")}
                          for it in sorted(items, key=lambda i: i["sceneItemIndex"])]
        for inp in (await o.call("GetInputList")).get("inputs", []):
            if inp["inputName"] not in names:
                names.append(inp["inputName"])

        schema, values, by_source = {}, {}, {}
        inst_notes = info.get("instances") or {}
        for src in names:
            if src in SKIP_SOURCES:
                continue
            fl = (await o.call("GetSourceFilterList", {"sourceName": src})).get("filters", [])
            out = []
            for i, f in enumerate(sorted(fl, key=lambda f: f["filterIndex"])):
                st = f.get("filterSettings") or {}
                params, text, header, fname = self.filter_params(f["filterKind"], st, info)
                key = (src, f["filterName"])
                schema[key] = {p["key"]: p for p in params}
                vals = {}
                for p in params:
                    v = st.get(p["key"], p["default"])
                    vals[p["key"]] = bool(v) if p["type"] == "bool" else v
                    c = cv.get((src, f["filterName"], p["key"]))
                    if c:
                        p["cv"] = c
                values[key] = vals
                out.append({"name": f["filterName"], "kind": f["filterKind"], "on": f["filterEnabled"],
                            "pos": i + 1, "of": len(fl), "file": fname, "info": text,
                            "note": inst_notes.get(f"{src}/{f['filterName']}", ""),
                            "header": header, "params": params, "values": vals})
            if out:
                by_source[src] = out

        tabs, used = [], set()
        for t in info.get("tabs") or []:
            src = t["source"]
            fl = by_source.get(src, [])
            if not fl and not t.get("layers"):
                continue
            used.add(src)
            stages, placed = [], set()
            for stg in t.get("stages") or []:
                fs = [f for f in fl if f["name"] in stg["filters"]]
                fs.sort(key=lambda f: f["pos"])
                placed.update(f["name"] for f in fs)
                if fs:
                    stages.append({"title": stg["title"], "filters": fs})
            rest = [f for f in fl if f["name"] not in placed]
            if rest:
                stages.append({"title": "Other", "filters": rest})
            tabs.append({"id": t["id"], "title": t["title"], "source": src, "info": t.get("info", ""),
                         "stages": stages, "layers": layers if t.get("layers") else None})
        for src, fl in by_source.items():
            if src not in used:
                tabs.append({"id": "src-" + src, "title": src, "source": src,
                             "info": "A source with filters that info.yaml does not describe yet.",
                             "stages": [{"title": "Filters", "filters": fl}], "layers": None})
        # virtual controls: one slider that moves several real ones together
        self.virtual = {}
        for v in info.get("virtual") or []:
            tab = next((t for t in tabs if t["id"] == v["tab"]), None)
            if not tab:
                continue
            p = dict(v["param"])
            try:
                val = float(json.loads((HERE / v["state"]).read_text())[p["key"]])
            except Exception:
                val = p["default"]
            key = (tab["source"], v["name"])
            schema[key], values[key] = {p["key"]: p}, {p["key"]: val}
            self.virtual[key] = v
            card = {"name": v["name"], "kind": "virtual", "virtual": True, "on": True, "pos": 0, "of": 0,
                    "file": "", "info": v.get("info", ""), "note": "", "header": "", "params": [p],
                    "values": values[key]}
            if tab["stages"]:
                tab["stages"][0]["filters"].insert(0, card)
            else:
                tab["stages"].append({"title": "Whole program", "filters": [card]})
        self.schema, self.values = schema, values
        self.fast = {k for k, sch in schema.items() if any("cv" in p for p in sch.values())}
        self.model = {"t": "model", "program": program, "tabs": tabs}
        log(f"model: {sum(len(v) for v in by_source.values())} filters on {len(by_source)} sources, "
            f"{sum(len(s) for s in schema.values())} controls")

    # ---- OBS events --------------------------------------------------------

    async def on_event(self, typ, data):
        if typ == "SourceFilterEnableStateChanged":
            await self.send_all({"t": "en", "s": data["sourceName"], "f": data["filterName"],
                                 "on": data["filterEnabled"]})
            self.patch_filter(data["sourceName"], data["filterName"], on=data["filterEnabled"])
        elif typ == "SceneItemEnableStateChanged":
            await self.send_all({"t": "item", "scene": data["sceneName"], "id": data["sceneItemId"],
                                 "on": data["sceneItemEnabled"]})
            for tab in (self.model or {}).get("tabs", []):
                for l in tab.get("layers") or []:
                    if l["scene"] == data["sceneName"] and l["id"] == data["sceneItemId"]:
                        l["on"] = data["sceneItemEnabled"]
        elif typ in ("SourceFilterCreated", "SourceFilterRemoved", "SourceFilterNameChanged",
                     "SourceFilterListReindexed", "SceneItemCreated", "SceneItemRemoved",
                     "CurrentProgramSceneChanged", "SceneCollectionChanged"):
            if data.get("sourceName") in SKIP_SOURCES or data.get("sceneName") in SKIP_SOURCES:
                return
            self.rebuild.set()

    def absorb(self, key, st):
        """Settings read back from OBS -> queue what differs from what the pages have."""
        sch, cur = self.schema.get(key), self.values.get(key)
        if sch is None:
            return
        now = time.monotonic()
        for k, p in sch.items():
            if k in st and now - self.wrote.get((key, k), 0) > 0.6:
                v = bool(st[k]) if p["type"] == "bool" else st[k]
                if cur.get(k) != v:
                    cur[k] = v
                    self.dirty.setdefault(key, {})[k] = v

    async def poller(self):
        """obs-websocket 5.7 sends no event when filter SETTINGS change (only enable state),
        so values moved by the CV bridge or the OBS filter panel are read back: CV-driven
        filters 5x a second, everything else once a second. Idle while no page is open."""
        n = 0
        while True:
            await asyncio.sleep(0.2)
            if not self.clients or not self.obs.ok or not self.model:
                continue
            n += 1
            try:
                if n % 5 == 0:
                    for src in {k[0] for k in self.schema}:
                        fl = (await self.obs.call("GetSourceFilterList", {"sourceName": src})).get("filters", [])
                        for f in fl:
                            self.absorb((src, f["filterName"]), f.get("filterSettings") or {})
                else:
                    for key in self.fast:
                        r = await self.obs.call("GetSourceFilter", {"sourceName": key[0], "filterName": key[1]})
                        self.absorb(key, r.get("filterSettings") or {})
            except Exception as e:
                if self.obs.ok:
                    log("read-back failed:", repr(e))

    def patch_filter(self, src, name, **kw):
        for tab in (self.model or {}).get("tabs", []):
            if tab["source"] == src:
                for stg in tab["stages"]:
                    for f in stg["filters"]:
                        if f["name"] == name:
                            f.update(kw)

    # ---- pages -------------------------------------------------------------

    async def send_all(self, msg, skip=None):
        if not self.clients:
            return
        raw = json.dumps(msg)
        for c in list(self.clients):
            if c is skip:
                continue
            try:
                await c.send(raw)
            except Exception:
                self.clients.discard(c)

    def coerce(self, p, v):
        if p["type"] == "bool":
            return bool(v)
        v = float(v)
        v = max(p["min"], min(p["max"], v))
        return int(round(v)) if p["type"] == "int" else v

    async def handle(self, conn):
        self.clients.add(conn)
        log("page connected:", conn.remote_address[0], f"({len(self.clients)} open)")
        try:
            await conn.send(json.dumps({"t": "status", **self.status}))
            if self.model:
                await conn.send(json.dumps(self.model_now()))
            async for raw in conn:
                try:
                    m = json.loads(raw)
                    await self.on_message(conn, m)
                except Exception as e:
                    log("message failed:", str(raw)[:120], "->", e)
                    await conn.send(json.dumps({"t": "err", "msg": str(e)}))
        except Exception:
            pass
        finally:
            self.clients.discard(conn)

    def model_now(self):
        """The model with current values (it is built once and patched as events arrive)."""
        for tab in self.model["tabs"]:
            for stg in tab["stages"]:
                for f in stg["filters"]:
                    f["values"] = self.values.get((tab["source"], f["name"]), f["values"])
        return self.model

    async def on_message(self, conn, m):
        t = m.get("t")
        if t == "set":
            key = (m["s"], m["f"])
            p = self.schema[key][m["k"]]
            v = self.coerce(p, m["v"])
            if key in self.virtual:
                await self.set_virtual(key, m["k"], v, conn)
                return
            self.pending.setdefault(key, {})[m["k"]] = v
            self.values[key][m["k"]] = v
            self.wrote[(key, m["k"])] = time.monotonic()
            if "cv" in p:
                # tell the bridge, so the knob continues from this value instead of snapping back
                self.to_bridge({"source": m["s"], "filter": m["f"], "property": m["k"], "value": v})
            await self.send_all({"t": "v", "s": m["s"], "f": m["f"], "k": {m["k"]: v}}, skip=conn)
        elif t == "en":
            await self.obs.call("SetSourceFilterEnabled", {"sourceName": m["s"], "filterName": m["f"],
                                                           "filterEnabled": bool(m["on"])})
            self.touch_save()
        elif t == "item":
            await self.obs.call("SetSceneItemEnabled", {"sceneName": m["scene"], "sceneItemId": int(m["id"]),
                                                        "sceneItemEnabled": bool(m["on"])})
        elif t == "cvclear":
            if "cv" in self.schema[(m["s"], m["f"])][m["k"]]:
                self.to_bridge({"source": m["s"], "filter": m["f"], "property": m["k"], "clear": True})
        elif t == "reload":
            self.rebuild.set()

    async def set_virtual(self, key, k, v, conn):
        """Move every target by the change of the virtual control, then remember its value."""
        spec, d = self.virtual[key], v - self.values[key][k]
        self.values[key][k] = v
        await self.send_all({"t": "v", "s": key[0], "f": key[1], "k": {k: v}}, skip=conn)
        now = time.monotonic()
        for src, flt, prop in spec["targets"]:
            tk = (src, flt)
            p = self.schema.get(tk, {}).get(prop)
            if not p:
                continue
            nv = self.coerce(p, self.values[tk][prop] + d)
            self.values[tk][prop] = nv
            self.pending.setdefault(tk, {})[prop] = nv
            self.wrote[(tk, prop)] = now
            await self.send_all({"t": "v", "s": src, "f": flt, "k": {prop: nv}})
        try:
            (HERE / spec["state"]).write_text(json.dumps({k: v}))
        except Exception as e:
            log("virtual state not saved:", e)

    def to_bridge(self, msg):
        try:
            if not hasattr(self, "udp"):
                self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.sendto(json.dumps(msg).encode(), BRIDGE_REBASE)
        except Exception as e:
            log("bridge not told:", e)

    def touch_save(self):
        self.save_at = time.monotonic() + 4.0

    # ---- loops -------------------------------------------------------------

    async def writer(self):
        """Coalesce slider traffic: at most one SetSourceFilterSettings per filter per 33 ms."""
        while True:
            await asyncio.sleep(0.033)
            if self.pending and self.obs.ok:
                batch, self.pending = self.pending, {}
                for (src, flt), st in batch.items():
                    try:
                        await self.obs.call("SetSourceFilterSettings", {
                            "sourceName": src, "filterName": flt, "filterSettings": st, "overlay": True})
                    except Exception as e:
                        log("write failed:", src, flt, e)
                self.touch_save()

    async def pusher(self):
        """Changes that came from OBS (CV bridge, filter panel, other pages) -> pages, 10 Hz."""
        while True:
            await asyncio.sleep(0.1)
            if self.dirty:
                batch, self.dirty = self.dirty, {}
                for (src, flt), ks in batch.items():
                    await self.send_all({"t": "v", "s": src, "f": flt, "k": ks})

    async def saver(self):
        """Filter changes made over obs-websocket do not mark the collection changed, so OBS
        would not autosave them. Creating and removing a scratch scene does (same trick as
        tools/camera_chain.py). Runs once, 4 s after the last change."""
        while True:
            await asyncio.sleep(0.5)
            if self.save_at and time.monotonic() >= self.save_at and self.obs.ok:
                self.save_at = None
                try:
                    await self.obs.call("CreateScene", {"sceneName": "zz_save_nudge"})
                    await asyncio.sleep(0.5)
                    await self.obs.call("RemoveScene", {"sceneName": "zz_save_nudge"})
                    await self.send_all({"t": "saved"})
                except Exception as e:
                    log("save nudge failed:", e)

    async def keeper(self):
        """Keep the link to main up, rebuild the model when asked, publish status."""
        last_relay = 0.0
        while True:
            if not self.obs.ok:
                try:
                    await self.obs.connect()
                    log("connected to main (:4455)")
                    self.rebuild.set()
                except Exception:
                    self.status = {"obs": False}
                    await self.send_all({"t": "status", **self.status})
                    await asyncio.sleep(3)
                    continue
            if self.rebuild.is_set():
                self.rebuild.clear()
                try:
                    await self.build()
                    await self.send_all(self.model_now())
                except Exception as e:
                    log("model build failed:", repr(e))
                    self.obs.ok = False
                    continue
            st = {"obs": True}
            try:
                s = await self.obs.call("GetStats")
                st.update(fps=round(s.get("activeFps", 0), 1), ms=round(s.get("averageFrameRenderTime", 0), 1),
                          cpu=round(s.get("cpuUsage", 0), 1), mem=round(s.get("memoryUsage", 0)))
            except Exception:
                st = {"obs": False}
            if time.monotonic() - last_relay > 6:
                last_relay = time.monotonic()
                self.relay_state = {}
                for name, r in self.relays.items():
                    try:
                        if not r.ok:
                            await r.connect()
                        if name == "Projector":
                            self.relay_state[name] = "up"
                        else:
                            ss = await r.call("GetStreamStatus")
                            self.relay_state[name] = ("reconnecting" if ss.get("outputReconnecting")
                                                      else "live" if ss.get("outputActive") else "idle")
                    except Exception:
                        r.ok = False
                        self.relay_state[name] = "down"
            st["relays"] = getattr(self, "relay_state", {})
            self.status = st
            await self.send_all({"t": "status", **st})
            try:
                await asyncio.wait_for(self.rebuild.wait(), 2)
            except asyncio.TimeoutError:
                pass


def http(connection, request):
    """Plain HTTP on the websocket port: serve the page."""
    if (request.headers.get("Upgrade") or "").lower() == "websocket":
        return None
    path = request.path.split("?")[0]
    if path in ("/", "/index.html"):
        body, ctype, code, reason = (HERE / "index.html").read_bytes(), "text/html; charset=utf-8", 200, "OK"
    else:
        body, ctype, code, reason = b"not found\n", "text/plain", 404, "Not Found"
    return Response(code, reason, Headers([("Content-Type", ctype), ("Content-Length", str(len(body))),
                                           ("Cache-Control", "no-store"), ("Connection", "close")]), body)


def lan_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.255.255", 9))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    password = yaml.safe_load(BRIDGE_CFG.read_text())["obs_instances"]["viture"]["password"]
    s = Surface(password)
    async with serve(s.handle, "0.0.0.0", args.port, process_request=http, ping_interval=20):
        log(f"control surface: http://{lan_address()}:{args.port}/")
        await asyncio.gather(s.keeper(), s.writer(), s.pusher(), s.saver(), s.poller())


if __name__ == "__main__":
    asyncio.run(main())
