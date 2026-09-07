#!/usr/bin/env python3
"""Fantasy Football War Room - local server.

Serves the dashboard and proxies/caches public data sources:
  - Sleeper API (leagues, rosters, drafts, players, projections, stats, schedule)
  - Fantasy Football Calculator ADP (public JSON, includes stdev)
No auth, no dependencies beyond the Python standard library.
"""
import json, os, sys, time, threading, gzip, io, hashlib, sqlite3, math
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


# ---------------------------------------------------------------- ESPN projections (2nd source)
ESPN_TEAMS = {1:'ATL',2:'BUF',3:'CHI',4:'CIN',5:'CLE',6:'DAL',7:'DEN',8:'DET',9:'GB',10:'TEN',11:'IND',12:'KC',13:'LV',14:'LAR',15:'MIA',16:'MIN',17:'NE',18:'NO',19:'NYG',20:'NYJ',21:'PHI',22:'ARI',23:'PIT',24:'LAC',25:'SF',26:'SEA',27:'TB',28:'WAS',29:'CAR',30:'JAX',33:'BAL',34:'HOU'}
ESPN_POS = {1: 'QB', 2: 'RB', 3: 'WR', 4: 'TE', 5: 'K', 16: 'DEF'}
ESPN_STAT = {'3': 'pass_yd', '4': 'pass_td', '19': 'pass_2pt', '20': 'pass_int', '24': 'rush_yd', '25': 'rush_td', '26': 'rush_2pt',
             '42': 'rec_yd', '43': 'rec_td', '44': 'rec_2pt', '53': 'rec', '58': 'rec_tgt', '72': 'fum_lost', '68': 'fum',
             '86': 'xpm', '88': 'xpmiss', '74': 'fgm_50p', '77': 'fgm_40_49', '80': 'fgm_30_39', '85': 'fgmiss',
             '99': 'sack', '95': 'int', '96': 'fum_rec', '97': 'blk_kick', '98': 'safe'}
DEFAULT_SCORING = {
    'ppr': {'pass_yd': .04, 'pass_td': 4, 'pass_int': -1, 'pass_2pt': 2, 'rush_yd': .1, 'rush_td': 6, 'rush_2pt': 2, 'rec': 1, 'rec_yd': .1, 'rec_td': 6, 'rec_2pt': 2, 'fum_lost': -2,
            'xpm': 1, 'fgm_0_19': 3, 'fgm_20_29': 3, 'fgm_30_39': 3, 'fgm_40_49': 4, 'fgm_50p': 5, 'fgmiss': -1,
            'sack': 1, 'int': 2, 'fum_rec': 2, 'safe': 2, 'blk_kick': 2, 'def_td': 6, 'pts_allow_0': 10, 'pts_allow_1_6': 7, 'pts_allow_7_13': 4, 'pts_allow_14_20': 1, 'pts_allow_28_34': -1, 'pts_allow_35p': -4}}
DEFAULT_SCORING['half'] = dict(DEFAULT_SCORING['ppr'], rec=0.5)
DEFAULT_SCORING['std'] = dict(DEFAULT_SCORING['ppr'], rec=0)


def score(stats, scoring):
    s = 0.0
    for k, w in scoring.items():
        v = stats.get(k)
        if v:
            s += w * v
    return s


def norm_name(s):
    import re
    s = (s or "").lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


def espn_index():
    """sleeper player id by espn id, with a name+position (and name+position+team) fallback,
    because Sleeper's espn_id coverage is patchy for recent draft classes."""
    raw = fetch_json("https://api.sleeper.app/v1/players/nfl", ttl=6 * 3600, disk=True)
    by_espn, by_name, by_name_team = {}, {}, {}
    for pid, pl in raw.items():
        pos = pl.get('position')
        if pos not in POSITIONS or pos == 'DEF':
            continue
        if pl.get('espn_id'):
            by_espn[str(pl['espn_id'])] = pid
        if not pl.get('active'):
            continue
        key = norm_name(pl.get('full_name') or (pl.get('first_name', '') + pl.get('last_name', ''))) + ':' + pos
        by_name.setdefault(key, []).append(pid)
        by_name_team[key + ':' + (pl.get('team') or '')] = pid
    return {"espn": by_espn, "name": by_name, "name_team": by_name_team}


def espn_projections(season):
    """{'season': {sid: stats}, 'weeks': {w: {sid: stats}}}; stats carry Sleeper keys plus pts_ppr=ESPN applied total."""
    ids = ["10%s" % season] + ["11%s%d" % (season, w) for w in range(1, 19)]
    flt = {"players": {"filterSlotIds": {"value": [0, 2, 4, 6, 16, 17, 23]}, "filterStatsForSourceIds": {"value": [1]},
                       "limit": 1500, "offset": 0, "sortAppliedStatTotal": {"sortAsc": False, "sortPriority": 1, "value": "10%s" % season},
                       "filterStatsForTopScoringPeriodIds": {"value": 20, "additionalValue": ids}}}
    url = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/%s/segments/0/leaguedefaults/3?view=kona_player_info" % season
    key = os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + ".json")
    now = time.time()
    if os.path.exists(key) and now - os.path.getmtime(key) < 3600:
        with open(key) as f:
            return json.load(f)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip", "X-Fantasy-Filter": json.dumps(flt)})
    with urlopen(req, timeout=90) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    data = json.loads(raw)
    idx = espn_index()
    out = {"season": {}, "weeks": {}}
    for e in data.get("players", []):
        pl = e.get("player") or {}
        pos = ESPN_POS.get(pl.get("defaultPositionId"))
        if not pos:
            continue
        if pos == 'DEF':
            sid = ESPN_TEAMS.get(pl.get("proTeamId"))
        else:
            sid = idx["espn"].get(str(pl.get("id")))
            if not sid:
                key = norm_name(pl.get("fullName")) + ':' + pos
                sid = idx["name_team"].get(key + ':' + (ESPN_TEAMS.get(pl.get("proTeamId")) or ''))
                if not sid and len(idx["name"].get(key, [])) == 1:
                    sid = idx["name"][key][0]
        if not sid:
            continue
        for st in pl.get("stats") or []:
            if st.get("statSourceId") != 1 or str(st.get("seasonId")) != str(season):
                continue
            stats = {}
            for k, v in (st.get("stats") or {}).items():
                if k in ESPN_STAT and v:
                    stats[ESPN_STAT[k]] = v
            if pos in ('RB', 'WR', 'TE') and stats.get('rec'):
                stats['bonus_rec_' + pos.lower()] = stats['rec']
            stats['pts_ppr'] = st.get("appliedTotal") or 0
            if st.get("statSplitTypeId") == 0:
                out["season"][sid] = stats
            else:
                w = st.get("scoringPeriodId")
                if w and 1 <= w <= 18:
                    out["weeks"].setdefault(str(w), {})[sid] = stats
    with open(key, "w") as f:
        json.dump(out, f)
    return out


# ---------------------------------------------------------------- accuracy log (sqlite)
DB = os.path.join(ROOT, "warroom.db")


def db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS proj_snap (season TEXT, week INTEGER, source TEXT, player_id TEXT, pos TEXT, pts REAL, taken_at REAL, PRIMARY KEY(season, week, source, player_id))")
    c.execute("CREATE TABLE IF NOT EXISTS actual (season TEXT, week INTEGER, player_id TEXT, pts REAL, PRIMARY KEY(season, week, player_id))")
    return c


def completed_weeks(season):
    games = fetch_json("https://api.sleeper.app/schedule/nfl/regular/%s" % season, ttl=1800, disk=True)
    by_week = {}
    for g in games:
        by_week.setdefault(g["week"], []).append(g.get("status"))
    return sorted(w for w, sts in by_week.items() if w <= 18 and sts and all(s == "complete" for s in sts))


def snapshot(season, week, source, rows, pos_of):
    """Freeze this source's projection for a week that has not been completed yet."""
    c = db()
    c.executemany("INSERT OR REPLACE INTO proj_snap VALUES (?,?,?,?,?,?,?)",
                  [(str(season), int(week), source, pid, pos_of.get(pid), float(pts), time.time()) for pid, pts in rows.items()])
    c.commit(); c.close()


def actuals(season, week):
    c = db()
    n = c.execute("SELECT COUNT(*) FROM actual WHERE season=? AND week=?", (str(season), int(week))).fetchone()[0]
    if n == 0:
        q = "season_type=regular&" + "&".join("position[]=" + p for p in POSITIONS) + "&order_by=pts_ppr"
        data = fetch_json("https://api.sleeper.com/stats/nfl/%s/%s?%s" % (season, week, q), ttl=24 * 3600, disk=True)
        c.executemany("INSERT OR REPLACE INTO actual VALUES (?,?,?,?)",
                      [(str(season), int(week), r["player_id"], float((r.get("stats") or {}).get("pts_ppr") or 0)) for r in data])
        c.commit()
    rows = dict(c.execute("SELECT player_id, pts FROM actual WHERE season=? AND week=?", (str(season), int(week))).fetchall())
    c.close()
    return rows


def accuracy(season):
    done = completed_weeks(season)
    c = db()
    out = {"weeks": done, "sources": {}, "weights": {"sleeper": 0.5, "espn": 0.5}}
    for w in done:
        act = actuals(season, w)
        snaps = c.execute("SELECT source, player_id, pos, pts FROM proj_snap WHERE season=? AND week=?", (str(season), w)).fetchall()
        for source, pid, pos, pts in snaps:
            if pts < 5 or pid not in act:
                continue
            s = out["sources"].setdefault(source, {})
            for key in (pos or 'ALL', 'ALL'):
                b = s.setdefault(key, {"n": 0, "ae": 0.0, "se": 0.0, "bias": 0.0})
                err = act[pid] - pts
                b["n"] += 1; b["ae"] += abs(err); b["se"] += err * err; b["bias"] += err
    c.close()
    for source, per in out["sources"].items():
        for key, b in per.items():
            if b["n"]:
                b["mae"] = b["ae"] / b["n"]; b["rmse"] = math.sqrt(b["se"] / b["n"]); b["bias"] = b["bias"] / b["n"]
            del b["ae"]; del b["se"]
    # inverse-MSE weights when both sources have enough completed data
    inv = {s: 1.0 / (per["ALL"]["rmse"] ** 2) for s, per in out["sources"].items() if per.get("ALL", {}).get("n", 0) >= 100}
    if len(inv) >= 2:
        tot = sum(inv.values())
        out["weights"] = {s: v / tot for s, v in inv.items()}
        out["weights_source"] = "inverse MSE over %d completed weeks" % len(done)
    else:
        out["weights_source"] = "equal (not enough completed weeks yet)"
    return out


def matrix(season, scoring, weights=None):
    """Consensus points per player per week (1..18) and per season, scored with the league's settings."""
    sl_season = {r["id"]: r["stats"] for r in projections(season)}
    with ThreadPoolExecutor(max_workers=6) as ex:
        sl_weeks = dict(ex.map(lambda w: (w, {r["id"]: (r["stats"], r["opp"]) for r in projections(season, w)}), range(1, 19)))
    try:
        es = espn_projections(season)
    except Exception as e:  # ESPN is optional
        es = {"season": {}, "weeks": {}}
        sys.stderr.write("espn unavailable: %r\n" % e)
    pos_of = {}
    raw_players = players_trimmed()
    for pid, pl in raw_players.items():
        pos_of[pid] = pl["pos"]
    if not weights:
        try:
            weights = accuracy(season)["weights"]
        except Exception:
            weights = {"sleeper": 0.5, "espn": 0.5}
    w_sl, w_es = weights.get("sleeper", 0.5), weights.get("espn", 0.5)

    def pts(stats, pid, src):
        if pos_of.get(pid) in ('K', 'DEF') and src == 'espn':
            return stats.get('pts_ppr') or 0
        return score(stats, scoring)

    def blend(a, b):
        if a is None: return b
        if b is None: return a
        return (w_sl * a + w_es * b) / (w_sl + w_es)

    ids = set(sl_season) | set(es["season"])
    for w in sl_weeks: ids |= set(sl_weeks[w])
    ids = [i for i in ids if i in pos_of]
    players, seasonc, src = {}, {}, {"sleeper": {"season": {}, "weeks": {}}, "espn": {"season": {}, "weeks": {}}}
    injuries, opp = {}, {}
    for pid in ids:
        a = pts(sl_season[pid], pid, 'sleeper') if pid in sl_season else None
        b = pts(es["season"][pid], pid, 'espn') if pid in es["season"] else None
        if a is not None: src["sleeper"]["season"][pid] = round(a, 1)
        if b is not None: src["espn"]["season"][pid] = round(b, 1)
        seasonc[pid] = round(blend(a, b) or 0, 1)
        row = []
        for w in range(1, 19):
            sw = sl_weeks[w].get(pid)
            a = pts(sw[0], pid, 'sleeper') if sw else None
            ew = es["weeks"].get(str(w), {}).get(pid)
            b = pts(ew, pid, 'espn') if ew else None
            row.append(round(blend(a, b) or 0, 1))
            if a is not None: src["sleeper"]["weeks"].setdefault(str(w), {})[pid] = round(a, 1)
            if b is not None: src["espn"]["weeks"].setdefault(str(w), {})[pid] = round(b, 1)
            if sw and sw[1]: opp.setdefault(str(w), {})[pid] = sw[1]
        players[pid] = row
    # freeze projections for weeks that have not finished, on PPR basis (comparable across leagues)
    try:
        done = set(completed_weeks(season))
        state = sleeper_v1("state/nfl", ttl=300)
        cur = state.get("week") or 1
        for w in range(max(1, cur), 19):
            if w in done: continue
            snapshot(season, w, "sleeper", {pid: (s[0].get("pts_ppr") or 0) for pid, s in sl_weeks[w].items()}, pos_of)
            if str(w) in es["weeks"]:
                snapshot(season, w, "espn", {pid: (s.get("pts_ppr") or 0) for pid, s in es["weeks"][str(w)].items()}, pos_of)
    except Exception as e:
        sys.stderr.write("snapshot failed: %r\n" % e)
    # fresh injury status from the current-week feed
    for r in fetch_json("https://api.sleeper.com/projections/nfl/%s/%s?season_type=regular&%s" % (season, max(1, cur), "&".join("position[]=" + p for p in POSITIONS)), ttl=900, disk=True):
        pl = r.get("player") or {}
        if pl.get("injury_status"):
            injuries[r["player_id"]] = {"status": pl["injury_status"], "part": pl.get("injury_body_part"), "note": pl.get("injury_notes")}
    return {"weeks": list(range(1, 19)), "players": players, "season": seasonc, "src": src, "weights": weights, "injuries": injuries, "opp": opp,
            "espn_weeks": sorted(int(w) for w in es["weeks"]), "espn_ok": bool(es["season"])}


# ---------------------------------------------------------------- league schedule + FAAB history
def league_chain(league_id, depth=3):
    out = []
    lid = league_id
    while lid and len(out) < depth:
        try:
            lg = sleeper_v1("league/%s" % lid, ttl=3600)
        except Exception:
            break
        out.append(lg)
        lid = lg.get("previous_league_id")
    return out


def league_schedule(league_id):
    lg = sleeper_v1("league/%s" % league_id, ttl=600)
    ps = lg.get("settings", {}).get("playoff_week_start") or 15
    with ThreadPoolExecutor(max_workers=6) as ex:
        weeks = list(ex.map(lambda w: (w, sleeper_v1("league/%s/matchups/%s" % (league_id, w), ttl=600)), range(1, ps)))
    sched = {}
    for w, ms in weeks:
        pairs = {}
        for m in ms or []:
            if m.get("matchup_id") is None:
                continue
            pairs.setdefault(m["matchup_id"], []).append(m["roster_id"])
        if pairs:
            sched[str(w)] = [v for v in pairs.values() if len(v) == 2]
    return {"playoff_week_start": ps, "schedule": sched}


def faab_history(league_id):
    """Every waiver bid (won and lost) in this league and up to two previous seasons, with the
    player's positional rank by that week's projection so bids are comparable across seasons."""
    chain = league_chain(league_id)
    recs, budgets = [], {}
    for lg in chain:
        season = lg["season"]; lid = lg["league_id"]
        budgets[season] = lg.get("settings", {}).get("waiver_budget") or 100
        users = {u["user_id"]: (u.get("metadata") or {}).get("team_name") or u.get("display_name") for u in sleeper_v1("league/%s/users" % lid, ttl=3600)}
        rosters = {r["roster_id"]: users.get(r.get("owner_id"), "Team %s" % r["roster_id"]) for r in sleeper_v1("league/%s/rosters" % lid, ttl=3600)}
        with ThreadPoolExecutor(max_workers=6) as ex:
            tx = list(ex.map(lambda w: (w, sleeper_v1("league/%s/transactions/%s" % (lid, w), ttl=1800 if season == chain[0]["season"] else 7 * 86400)), range(1, 18)))
        wanted = {}
        for w, ts in tx:
            for t in ts or []:
                if t.get("type") != "waiver" or not t.get("adds"):
                    continue
                bid = (t.get("settings") or {}).get("waiver_bid")
                if bid is None:
                    continue
                for pid, rid in t["adds"].items():
                    recs.append({"season": season, "week": w, "player_id": pid, "bid": bid, "won": t.get("status") == "complete",
                                 "team": rosters.get(rid, "Team %s" % rid), "budget": budgets[season]})
                    wanted.setdefault(w, set()).add(pid)
        # positional rank of each bid target: best of that week's and the following week's projection,
        # because bids react to news that the current-week projection may not reflect yet
        def week_rank(w):
            try:
                rows = projections(season, w, ttl=7 * 86400)
            except Exception:
                return {}
            by_pos = {}
            for r in rows:
                by_pos.setdefault(r["pos"], []).append((r["id"], r["stats"].get("pts_ppr") or 0))
            rank = {}
            for pos, arr in by_pos.items():
                arr.sort(key=lambda x: -x[1])
                for i, (pid, pts) in enumerate(arr):
                    rank[pid] = (i + 1, pts, pos)
            return rank
        ranks = {}
        for w in sorted(set(list(wanted) + [w + 1 for w in wanted])):
            if w <= 18:
                ranks[w] = week_rank(w)
        for rec in recs:
            if rec["season"] != season:
                continue
            cands = [ranks.get(w, {}).get(rec["player_id"]) for w in (rec["week"], rec["week"] + 1)]
            cands = [c for c in cands if c]
            if cands:
                best = min(cands, key=lambda c: c[0])
                rec["rank"], rec["proj"], rec["pos"] = best
    players = players_trimmed()
    for rec in recs:
        pl = players.get(rec["player_id"])
        rec["name"] = pl["name"] if pl else rec["player_id"]
        rec.setdefault("pos", pl["pos"] if pl else None)
    return {"seasons": [lg["season"] for lg in chain], "budgets": budgets, "bids": recs}


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
        if "/api/" in str(args[0] if args else ""):
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

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/matrix":
                scoring = body.get("scoring") or DEFAULT_SCORING.get(body.get("scoringKey", "half"), DEFAULT_SCORING["half"])
                return self.send_json(matrix(str(body.get("season", "2026")), scoring, body.get("weights")))
            return self.send_json({"error": "unknown endpoint"}, 404)
        except Exception as e:  # noqa
            return self.send_json({"error": repr(e)}, 500)

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
            if path == "accuracy":
                return self.send_json(accuracy(q.get("season", "2026")))
            if path == "schedule":
                return self.send_json(league_schedule(q["league"]))
            if path == "faab":
                return self.send_json(faab_history(q["league"]))
            if path == "espn/status":
                es = espn_projections(q.get("season", "2026")); return self.send_json({"season_players": len(es["season"]), "weeks": sorted(int(w) for w in es["weeks"])})
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
