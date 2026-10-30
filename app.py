"""NYC live transit: GTFS-RT -> vehicle snapshot -> GeoJSON / GPX / raster tiles / MapLibre page.

Single process, stdlib HTTP server. Deps: gtfs-realtime-bindings, pillow.
Env: DATA_DIR (default ./data), POLL_SECONDS (15), PORT (8080), PUBLIC_URL (for the OsmAnd link page),
     NJT_USER / NJT_PASS (optional, NJ Transit, NOT implemented in this prototype: see README).
"""
import csv, io, json, math, os, threading, time, urllib.request, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from google.transit import gtfs_realtime_pb2 as pb
from PIL import Image, ImageDraw, ImageFont

DATA = os.environ.get("DATA_DIR", "./data")
POLL = int(os.environ.get("POLL_SECONDS", "15"))
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:%d" % PORT)
MTA = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/"
S3 = "https://rrgtfsfeeds.s3.amazonaws.com/"
# mode -> (realtime feed urls, static zip or None)
SUBWAY = ["nyct%2Fgtfs", "nyct%2Fgtfs-ace", "nyct%2Fgtfs-bdfm", "nyct%2Fgtfs-g", "nyct%2Fgtfs-jz",
          "nyct%2Fgtfs-nqrw", "nyct%2Fgtfs-l", "nyct%2Fgtfs-si"]
FEEDS = {
    "subway": ([MTA + f for f in SUBWAY], "gtfs_subway.zip"),
    "lirr": ([MTA + "lirr%2Fgtfs-lirr"], "gtfslirr.zip"),
    "mnr": ([MTA + "mnr%2Fgtfs-mnr"], "gtfsmnr.zip"),
    # Bus Time: documented as key-required (?key=); answered keyless on 2026-10-30. Set BUS_KEY if it starts 401ing.
    "bus": (["http://gtfsrt.prod.obanyc.com/vehiclePositions" + ("?key=" + os.environ["BUS_KEY"] if os.environ.get("BUS_KEY") else "")], None),
}
MODE_COLOR = {"lirr": "#0039a6", "mnr": "#d1232a", "bus": "#1d70b8", "subway": "#444444"}

stops = {m: {} for m in FEEDS}    # mode -> stop_id -> (lat, lon)
routes = {m: {} for m in FEEDS}   # mode -> route_id -> (color, short_name)
snap = {"t": 0, "vehicles": [], "errors": {}}
tile_cache = {}
lock = threading.Lock()


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-transit-live/0.1"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def load_static():
    os.makedirs(DATA, exist_ok=True)
    for mode, (_, zname) in FEEDS.items():
        if not zname:
            continue
        path = os.path.join(DATA, zname)
        if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 86400:
            try:
                with open(path + ".tmp", "wb") as f:
                    f.write(fetch(S3 + zname, 120))
                os.replace(path + ".tmp", path)
            except Exception as e:
                print("static fetch failed", mode, e)
                if not os.path.exists(path):
                    continue
        with zipfile.ZipFile(path) as z:
            rd = lambda n: csv.DictReader(io.TextIOWrapper(z.open(n), "utf-8-sig"))
            stops[mode] = {r["stop_id"]: (float(r["stop_lat"]), float(r["stop_lon"]))
                           for r in rd("stops.txt") if r.get("stop_lat")}
            routes[mode] = {r["route_id"]: ("#" + (r.get("route_color") or "444444"), r.get("route_short_name") or r["route_id"])
                            for r in rd("routes.txt")}
        print("static loaded", mode, len(stops[mode]), "stops")


def parse(url):
    m = pb.FeedMessage()
    m.ParseFromString(fetch(url))
    return m


def vehicles_from_feed(mode, msg, now):
    out, have_pos = [], set()
    st = stops[mode]
    for e in msg.entity:
        if e.HasField("vehicle") and e.vehicle.HasField("position") and e.vehicle.position.latitude:
            v = e.vehicle
            tid = v.trip.trip_id
            have_pos.add(tid)
            out.append(dict(mode=mode, id=v.vehicle.id or tid, route=v.trip.route_id, lat=v.position.latitude,
                            lon=v.position.longitude, bearing=v.position.bearing if v.position.HasField("bearing") else None,
                            src="gps"))
    # No lat/lon (subway, most of MNR): interpolate between stops from TripUpdate times.
    for e in msg.entity:
        if not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        if tu.trip.trip_id in have_pos:
            continue
        pts = []
        for s in tu.stop_time_update:
            t = (s.arrival.time or s.departure.time) if (s.HasField("arrival") or s.HasField("departure")) else 0
            if t and s.stop_id in st:
                pts.append((t, (s.departure.time or t), st[s.stop_id]))
        if not pts:
            continue
        pos = None
        if now < pts[0][0]:
            if pts[0][0] - now <= 90:       # about to arrive at its first listed stop
                pos = pts[0][2]
        elif now > pts[-1][1]:
            continue                       # finished
        else:
            for (a_arr, a_dep, a), (b_arr, _, b) in zip(pts, pts[1:]):
                if a_arr <= now <= a_dep:
                    pos = a; break
                if a_dep < now < b_arr:
                    f = (now - a_dep) / max(1, b_arr - a_dep)
                    pos = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f); break
            if pos is None:
                pos = pts[-1][2]
        if pos:
            out.append(dict(mode=mode, id=tu.trip.trip_id, route=tu.trip.route_id, lat=pos[0], lon=pos[1],
                            bearing=None, src="interp"))
    return out


def poll_once():
    now = int(time.time())
    allv, errs = [], {}
    jobs = [(mode, u) for mode, (urls, _) in FEEDS.items() for u in urls]
    with ThreadPoolExecutor(8) as ex:
        futs = [(mode, u, ex.submit(parse, u)) for mode, u in jobs]
        for mode, u, f in futs:
            try:
                allv += vehicles_from_feed(mode, f.result(), now)
            except Exception as e:
                errs[u] = str(e)
    for v in allv:
        c, n = routes[v["mode"]].get(v["route"], (MODE_COLOR[v["mode"]], v["route"]))
        v["color"], v["label"] = (c if v["mode"] != "bus" else MODE_COLOR["bus"]), n
    with lock:
        if allv or not snap["vehicles"]:
            snap.update(t=now, vehicles=allv, errors=errs)
        else:
            snap["errors"] = errs
        tile_cache.clear()


def poller():
    n = 0
    while True:
        try:
            if n % int(6 * 3600 / POLL) == 0:
                load_static()
            poll_once()
        except Exception as e:
            print("poll error", e)
        n += 1
        time.sleep(POLL)


# ---- rendering
def lonlat_px(lat, lon, z):
    n = 256 * 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


_fonts = {}
def font(sz):
    if sz not in _fonts:
        _fonts[sz] = ImageFont.load_default(sz)
    return _fonts[sz]


def render_tile(z, x, y):
    key = (z, x, y)
    with lock:
        if key in tile_cache:
            return tile_cache[key]
        vs = list(snap["vehicles"])
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = 2 if z <= 10 else 3 if z <= 12 else 5 if z <= 14 else 8
    label = z >= 14
    ox, oy = x * 256, y * 256
    for v in sorted(vs, key=lambda v: v["mode"] == "bus", reverse=True):
        px, py = lonlat_px(v["lat"], v["lon"], z)
        px -= ox; py -= oy
        rr = r * (0.6 if v["mode"] == "bus" and z < 14 else 1)
        if -rr - 2 > px or px > 258 + rr or -rr - 2 > py or py > 258 + rr:
            continue
        box = (px - rr, py - rr, px + rr, py + rr)
        if v["mode"] in ("lirr", "mnr"):
            d.rectangle(box, fill=v["color"], outline="white")
        else:
            d.ellipse(box, fill=v["color"], outline="white")
        if label and v["mode"] != "bus" or (label and z >= 16):
            d.text((px, py), str(v["label"])[:2], fill="white", font=font(int(rr * 1.4)), anchor="mm")
    buf = io.BytesIO(); img.save(buf, "PNG", optimize=False)
    data = buf.getvalue()
    with lock:
        tile_cache[key] = data
    return data


def geojson(modes=None):
    with lock:
        vs, t = list(snap["vehicles"]), snap["t"]
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(v["lon"], 5), round(v["lat"], 5)]},
              "properties": {k: v[k] for k in ("mode", "id", "route", "label", "color", "src", "bearing")}}
             for v in vs if not modes or v["mode"] in modes]
    return json.dumps({"type": "FeatureCollection", "t": t, "features": feats}, separators=(",", ":")).encode()


def gpx(modes=None):
    with lock:
        vs, t = list(snap["vehicles"]), snap["t"]
    rows = "".join('<wpt lat="%.5f" lon="%.5f"><name>%s %s</name><type>%s</type></wpt>' % (
        v["lat"], v["lon"], v["mode"], v["label"], v["mode"]) for v in vs if not modes or v["mode"] in modes)
    return ('<?xml version="1.0"?><gpx version="1.1" creator="nyc-transit-live" xmlns="http://www.topografix.com/GPX/1/1">'
            + rows + "</gpx>").encode()


def osmand_link():
    tpl = PUBLIC_URL + "/tiles/{0}/{1}/{2}.png"
    from urllib.parse import urlencode
    q = urlencode(dict(name="NYC live transit", url_template=tpl, ext=".png", min_zoom=8, max_zoom=18,
                       tile_size=256, expiration_time_minutes=1), safe="{}/:")
    return "https://osmand.net/add-tile-source?" + q


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, body, ctype, cache="no-cache"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path); p = u.path
        modes = set(parse_qs(u.query).get("modes", [""])[0].split(",")) - {""}
        try:
            if p == "/healthz":
                return self.send(json.dumps({"vehicles": len(snap["vehicles"]), "age": int(time.time()) - snap["t"],
                                             "errors": snap["errors"]}).encode(), "application/json")
            if p.startswith("/tiles/"):
                z, x, y = p[7:].rsplit(".", 1)[0].split("/")
                z, x, y = int(z), int(x), int(y)
                return self.send(render_tile(z, x, y), "image/png", "public, max-age=%d" % (POLL * 2 // 3))
            if p == "/api/vehicles.geojson":
                return self.send(geojson(modes), "application/geo+json")
            if p == "/vehicles.gpx":
                return self.send(gpx(modes), "application/gpx+xml")
            if p == "/osmand":
                l = osmand_link()
                return self.send(('<!doctype html><meta name=viewport content="width=device-width"><title>OsmAnd</title>'
                                  '<h2>Open in OsmAnd+</h2><p><a href="%s">Add "NYC live transit" overlay to OsmAnd</a></p>'
                                  '<p>Then Menu - Configure map - Overlay map - NYC live transit.</p><code>%s</code>'
                                  % (l.replace("&", "&amp;"), l.replace("&", "&amp;"))).encode(), "text/html")
            if p in ("/", "/index.html"):
                return self.send(open(os.path.join(os.path.dirname(__file__), "static", "index.html"), "rb").read(), "text/html")
        except Exception as e:
            self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode()); return
        self.send_response(404); self.end_headers()


if __name__ == "__main__":
    threading.Thread(target=poller, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
