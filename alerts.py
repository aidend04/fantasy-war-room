#!/usr/bin/env python3
"""Game-day lineup alerts for a Sleeper team, runnable from cron or by hand.

  python3 alerts.py --user <sleeper username> [--league <league id>] [--notify] [--webhook URL]

Flags starters who are out, doubtful, questionable, on bye or barely projected, and the best
bench swap. --notify sends a desktop notification (notify-send); --webhook posts JSON {"content": ...}
to a Discord/Slack-style webhook. Exit code 1 when there is something to act on.
"""
import argparse, json, subprocess, sys
from urllib.request import Request, urlopen
import server

POS = server.POSITIONS
FLEX = {"FLEX": ["RB", "WR", "TE"], "SUPER_FLEX": ["QB", "RB", "WR", "TE"], "REC_FLEX": ["WR", "TE"], "WRRB_FLEX": ["RB", "WR"]}
BAD = {"Out", "IR", "PUP", "Sus", "NA", "Doubtful"}


def best_lineup(ids, pts, pos_of, slots):
    avail = sorted([i for i in ids if i in pos_of], key=lambda i: -pts.get(i, 0))
    used, out = set(), []
    def take(elig):
        for i in avail:
            if i not in used and pos_of[i] in elig:
                used.add(i); return i
        return None
    for s in slots:
        if s in POS: out.append((s, take([s])))
    for s in slots:
        if s in FLEX: out.append((s, take(FLEX[s])))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True); ap.add_argument("--league"); ap.add_argument("--week", type=int)
    ap.add_argument("--notify", action="store_true"); ap.add_argument("--webhook")
    a = ap.parse_args()
    state = server.sleeper_v1("state/nfl", ttl=300); season = state["season"]; week = a.week or state.get("display_week") or state.get("week") or 1
    user = server.sleeper_v1("user/%s" % a.user)
    leagues = server.sleeper_v1("user/%s/leagues/nfl/%s" % (user["user_id"], season))
    league = next((l for l in leagues if l["league_id"] == a.league), leagues[0] if leagues else None)
    if not league:
        print("no league found"); return 0
    rosters = server.sleeper_v1("league/%s/rosters" % league["league_id"], ttl=60)
    mine = next((r for r in rosters if r.get("owner_id") == user["user_id"] or user["user_id"] in (r.get("co_owners") or [])), None)
    if not mine:
        print("you have no roster in", league["name"]); return 0
    players = server.players_trimmed()
    byes = server.byes(season)["byes"]
    scoring = league["scoring_settings"]
    weekly = {r["id"]: r for r in server.projections(season, week, ttl=600)}
    raw = server.fetch_json("https://api.sleeper.com/projections/nfl/%s/%s?season_type=regular&%s" % (season, week, "&".join("position[]=" + p for p in POS)), ttl=600, disk=True)
    injury = {r["player_id"]: (r.get("player") or {}) for r in raw}
    pts = {pid: server.score(weekly[pid]["stats"], scoring) if pid in weekly else 0.0 for pid in mine.get("players") or []}
    pos_of = {pid: players[pid]["pos"] for pid in mine.get("players") or [] if pid in players}
    slots = [s for s in league["roster_positions"] if s != "BN" and not s.startswith(("IDP", "DL", "LB", "DB"))]
    starters = [s for s in (mine.get("starters") or []) if s and s != "0"]
    alerts = []
    for pid in starters:
        pl = players.get(pid)
        if not pl:
            alerts.append("A starting slot is empty or holds an unknown player"); continue
        inj = (injury.get(pid) or {}).get("injury_status") or pl.get("inj")
        note = (injury.get(pid) or {}).get("injury_body_part") or ""
        if byes.get(pl["team"]) == week:
            alerts.append("%s is on bye" % pl["name"])
        elif inj in BAD:
            alerts.append("%s is %s%s" % (pl["name"], inj, " (%s)" % note if note else ""))
        elif inj == "Questionable":
            alerts.append("%s is questionable%s; check inactives" % (pl["name"], " (%s)" % note if note else ""))
        elif pts.get(pid, 0) < 2:
            alerts.append("%s projects only %.1f pts" % (pl["name"], pts.get(pid, 0)))
    opt = best_lineup(mine.get("players") or [], pts, pos_of, slots)
    opt_ids = {pid for _, pid in opt if pid}
    cur = sum(pts.get(pid, 0) for pid in starters); best = sum(pts.get(pid, 0) for pid in opt_ids)
    bench_in = [players[pid]["name"] for pid in opt_ids if pid not in starters]
    start_out = [players[pid]["name"] for pid in starters if pid in players and pid not in opt_ids]
    if bench_in and best - cur > 0.5:
        alerts.append("Start %s over %s for +%.1f pts" % (", ".join(bench_in), ", ".join(start_out) or "the empty slot", best - cur))
    head = "%s · week %s" % (league["name"], week)
    body = "\n".join("- " + x for x in alerts) if alerts else "All starters healthy, active and optimal."
    print(head); print(body)
    if alerts and a.notify:
        try: subprocess.run(["notify-send", "War Room: " + head, body], check=False)
        except FileNotFoundError: pass
    if alerts and a.webhook:
        req = Request(a.webhook, data=json.dumps({"content": "**%s**\n%s" % (head, body), "text": "%s\n%s" % (head, body)}).encode(), headers={"Content-Type": "application/json"})
        urlopen(req, timeout=20).read()
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
