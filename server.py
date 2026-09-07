#!/usr/bin/env python3
"""Fantasy Football War Room - local server.

Serves the dashboard and proxies/caches public data sources:
  - Sleeper API (leagues, rosters, drafts, players, projections, stats, schedule)
  - Fantasy Football Calculator ADP (public JSON, includes stdev)
No auth, no dependencies beyond the Python standard library.
"""
import json, os, sys, time, threading, gzip, io, hashlib
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)
PORT = int(os.environ.get("PORT", "8765"))
POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]
UA = "fantasy-war-room/1.0 (personal use)"

_mem = {}
_lock = threading.Lock()


def fetch_json(url, ttl=60, disk=False):
    """GET url as JSON with in-memory (and optional on-disk) TTL cache."""
    now = time.time()
    with _lock:
        hit = _mem.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    key = os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + ".json")
    if disk and os.path.exists(key) and now - os.path.getmtime(key) < ttl:
        with open(key) as f:
            data = json.load(f)
        with _lock:
            _mem[url] = (now, data)
        return data
    req = Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urlopen(req, timeout=60) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    data = json.loads(raw)
    with _lock:
        _mem[url] = (now, data)
    if disk:
        with open(key, "w") as f:
            json.dump(data, f)
    return data


def sleeper_v1(path, ttl=60):
    return fetch_json("https://api.sleeper.app/v1/" + path.lstrip("/"), ttl=ttl)


def players_trimmed():
    """The full player DB is ~15MB; keep the fantasy-relevant subset and fields."""
    data = fetch_json("https://api.sleeper.app/v1/players/nfl", ttl=6 * 3600, disk=True)
    out = {}
    for pid, p in data.items():
        pos = p.get("position")
        if pos not in POSITIONS:
            continue
        if not p.get("active") and pos != "DEF":
            continue
        out[pid] = {
            "id": pid,
            "name": p.get("full_name") or (p.get("first_name", "") + " " + p.get("last_name", "")).strip(),
            "pos": pos,
            "fpos": p.get("fantasy_positions") or [pos],
            "team": p.get("team"),
            "age": p.get("age"),
            "exp": p.get("years_exp"),
            "inj": p.get("injury_status"),
            "injNote": p.get("injury_body_part"),
            "status": p.get("status"),
            "depth": p.get("depth_chart_order"),
            "rank": p.get("search_rank"),
            "num": p.get("number"),
        }
    return out


def projections(season, week=None, ttl=3600):
    q = "season_type=regular&" + "&".join("position[]=" + p for p in POSITIONS) + "&order_by=pts_ppr"
    if week:
        url = f"https://api.sleeper.com/projections/nfl/{season}/{week}?{q}"
    else:
        url = f"https://api.sleeper.com/projections/nfl/{season}?{q}"
    data = fetch_json(url, ttl=ttl, disk=True)
    return [
        {"id": r["player_id"], "team": r.get("team"), "pos": (r.get("player") or {}).get("position"),
         "opp": r.get("opponent"), "stats": r.get("stats") or {}, "company": r.get("company")}
        for r in data if r.get("stats")
    ]


def stats(season, ttl=6 * 3600):
    q = "season_type=regular&" + "&".join("position[]=" + p for p in POSITIONS) + "&order_by=pts_ppr"
    data = fetch_json(f"https://api.sleeper.com/stats/nfl/{season}?{q}", ttl=ttl, disk=True)
    return [{"id": r["player_id"], "stats": r.get("stats") or {}} for r in data if r.get("stats")]


def ros(season, from_week):
    """Rest-of-season projection = sum of weekly projections from from_week..18."""
    weeks = list(range(int(from_week), 19))
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda w: (w, projections(season, w)), weeks))
    agg = {}
    for w, rows in results:
        for r in rows:
            a = agg.setdefault(r["id"], {"id": r["id"], "stats": {}, "weeks": 0})
            a["weeks"] += 1
            for k, v in r["stats"].items():
                if isinstance(v, (int, float)):
                    a["stats"][k] = a["stats"].get(k, 0) + v
    return {"from_week": int(from_week), "weeks": weeks, "players": list(agg.values())}


def byes(season):
    games = fetch_json(f"https://api.sleeper.app/schedule/nfl/regular/{season}", ttl=24 * 3600, disk=True)
    teams, by_week = set(), {}
    for g in games:
        teams.add(g["home"]); teams.add(g["away"])
        by_week.setdefault(g["week"], set()).update([g["home"], g["away"]])
    out = {}
    for w in sorted(by_week):
        if w > 18: continue
        for t in teams - by_week[w]:
            out.setdefault(t, w)
    return {"byes": out, "games": [g for g in games if g.get("week", 0) <= 18]}


def adp(fmt, teams):
    fmt = {"ppr": "ppr", "half": "half-ppr", "half-ppr": "half-ppr", "std": "standard",
           "standard": "standard", "2qb": "2qb"}.get(fmt, "ppr")
    url = f"https://fantasyfootballcalculator.com/api/v1/adp/{fmt}?teams={teams}&year=2026"
    data = fetch_json(url, ttl=6 * 3600, disk=True)
    return data


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(ROOT, "static"), **k)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                gz.write(body)
            body = buf.getvalue()
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            if u.path == "/":
                self.path = "/index.html"
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            path = u.path[len("/api/"):]
            if path == "state":
                return self.send_json(sleeper_v1("state/nfl", ttl=300))
            if path == "players":
                return self.send_json(players_trimmed())
            if path == "projections/season":
                return self.send_json(projections(q.get("season", "2026")))
            if path == "projections/week":
                return self.send_json(projections(q.get("season", "2026"), q.get("week", "1"), ttl=900))
            if path == "projections/ros":
                return self.send_json(ros(q.get("season", "2026"), q.get("from", "1")))
            if path == "stats/season":
                return self.send_json(stats(q.get("season", "2025")))
            if path == "byes":
                return self.send_json(byes(q.get("season", "2026")))
            if path == "adp":
                return self.send_json(adp(q.get("format", "ppr"), q.get("teams", "12")))
            if path == "trending":
                return self.send_json(sleeper_v1(
                    f"players/nfl/trending/{q.get('type','add')}?lookback_hours={q.get('hours','48')}&limit=100", ttl=900))
            if path.startswith("sleeper/"):
                sub = path[len("sleeper/"):]
                ttl = 5 if "/picks" in sub else 60
                return self.send_json(sleeper_v1(sub, ttl=ttl))
            return self.send_json({"error": "unknown endpoint"}, 404)
        except HTTPError as e:
            return self.send_json({"error": f"upstream {e.code}", "url": e.url}, 502)
        except URLError as e:
            return self.send_json({"error": f"network: {e.reason}"}, 502)
        except Exception as e:  # noqa
            return self.send_json({"error": repr(e)}, 500)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"War Room running at http://localhost:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
