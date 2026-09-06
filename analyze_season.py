#!/usr/bin/env python3
"""Season win-odds simulator for the league.

Corrections over the naive version:
- Weekly lineups: a player scores 0 on his bye week; the optimal lineup is
  re-solved per week from whoever is actually playing (start/sit).
- Real-life matchup strength: each player's ppg is scaled by his team's
  FantasyPros season SOS vs his position (stars, 3.0 = neutral, +/-1 star
  = +/-2% weekly) — the aggregate of actual NFL opponents faced.
- Playoff structure read from league settings (playoff_week_start, playoff_teams).

Usage: python3 analyze_season.py [--sims 5000]
"""
import json
import math
import random
import sys
import urllib.request

LEAGUE = "1399865638406627328"
MY_USER = "1399880454529658880"
SIMS = int(sys.argv[sys.argv.index("--sims") + 1]) if "--sims" in sys.argv else 5000
K_PPG, DST_PPG = 8.0, 1.5   # DST is TD-only in this league
PLAYER_WEEKLY_CV = 0.60          # audited: real weekly RB/WR CV ~60-80%
TEAM_SEASON_SHOCK = 0.12         # correlated projection error at team level
REPLACEMENT = {"QB": 11.0, "RB": 6.0, "WR": 6.0, "TE": 5.0, "FLEX": 6.0,
               "SFLX": 8.0, "K": 7.5, "DEF": 1.2}
SOS_PCT_PER_STAR = 0.02          # +/-2% ppg per SOS star away from neutral

SLOTS = [("QB", ["QB"]), ("RB", ["RB"]), ("RB", ["RB"]), ("WR", ["WR"]), ("WR", ["WR"]),
         ("TE", ["TE"]), ("FLEX", ["RB", "WR", "TE"]), ("SFLX", ["QB", "RB", "WR", "TE"]),
         ("K", ["K"]), ("DEF", ["DST"])]


def get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def load():
    league = get(f"https://api.sleeper.app/v1/league/{LEAGUE}")
    users = {u["user_id"]: u["display_name"] for u in get(f"https://api.sleeper.app/v1/league/{LEAGUE}/users")}
    rosters = get(f"https://api.sleeper.app/v1/league/{LEAGUE}/rosters")
    pw = league["settings"].get("playoff_week_start", 15)
    pteams = league["settings"].get("playoff_teams", 6)
    sched = {}
    for w in range(1, pw):
        pairs = {}
        for m in get(f"https://api.sleeper.app/v1/league/{LEAGUE}/matchups/{w}"):
            pairs.setdefault(m["matchup_id"], []).append(m["roster_id"])
        sched[w] = [tuple(v) for v in pairs.values() if len(v) == 2]
    return league, users, rosters, sched, pw, pteams


def build_players():
    ps = json.load(open("players.json"))
    by_sid = {}
    for p in ps:
        if not p["sleeper_id"]:
            continue
        if p["pos"] == "K":
            base = K_PPG
        elif p["pos"] == "DST":
            base = DST_PPG
        else:
            base = p["pts"] / 17 if p["pts"] else 0.0
        sos = p.get("sos") or 3.0
        ppg = base * (1 + (sos - 3.0) * SOS_PCT_PER_STAR)
        bye = int(p["bye"]) if p.get("bye") and str(p["bye"]).isdigit() else 0
        by_sid[p["sleeper_id"]] = {"name": p["name"], "pos": p["pos"], "ppg": ppg, "bye": bye}
    return by_sid


def weekly_lineup(roster_sids, by_sid, week):
    """Optimal lineup for a week: bye players unavailable. Returns (mean, sd, lineup)."""
    avail = sorted(
        (dict(by_sid[s], sid=s) for s in roster_sids if s in by_sid and by_sid[s]["bye"] != week),
        key=lambda x: -x["ppg"])
    used, mean, var, lineup = set(), 0.0, 0.0, []
    for slot, elig in SLOTS:
        best = next((x for x in avail if x["sid"] not in used and x["pos"] in elig), None)
        if best:
            used.add(best["sid"])
            mean += best["ppg"]
            var += (PLAYER_WEEKLY_CV * best["ppg"]) ** 2
            lineup.append((slot, best["name"], round(best["ppg"], 1)))
        else:
            # waiver replacement — an empty slot never scores 0 in real life
            rep = REPLACEMENT.get(slot, 6.0)
            mean += rep
            var += (PLAYER_WEEKLY_CV * rep) ** 2
            lineup.append((slot, "(waiver fill)", rep))
    return mean, math.sqrt(var), lineup


def main():
    league, users, rosters, sched, pw, pteams = load()
    by_sid = build_players()
    own = {r["roster_id"]: users.get(r["owner_id"], str(r["roster_id"])) for r in rosters}
    roster_sids = {r["roster_id"]: [s for s in (r.get("players") or [])
                    if s not in set(r.get("reserve") or []) | set(r.get("taxi") or [])]
                   for r in rosters}
    my_rid = next(r["roster_id"] for r in rosters if r["owner_id"] == MY_USER)

    # per-team per-week strengths (byes + SOS baked in)
    weeks = sorted(sched)
    strength = {rid: {w: weekly_lineup(roster_sids[rid], by_sid, w)[:2] for w in weeks}
                for rid in roster_sids}
    playoff_r1_strength = {rid: weekly_lineup(roster_sids[rid], by_sid, pw)[:2] for rid in roster_sids}
    playoff_wk_strength = {rid: weekly_lineup(roster_sids[rid], by_sid, 99)[:2] for rid in roster_sids}

    print(f"settings: {len(weeks)}-week regular season, {pteams}-team playoff from week {pw}")
    print(f'{"team":17s} {"season ppw":>10s} {"worst bye week":>22s}')
    for rid in sorted(strength, key=lambda r: -sum(strength[r][w][0] for w in weeks)):
        means = [strength[rid][w][0] for w in weeks]
        wmin = weeks[means.index(min(means))]
        print(f'{own[rid]:17s} {sum(means)/len(means):10.1f} '
              f'{"wk" + str(wmin):>8s} ({min(means):5.1f} vs avg {sum(means)/len(means):5.1f})')

    random.seed(11)
    playoffs = {r: 0 for r in strength}
    titles = {r: 0 for r in strength}
    expw = {r: 0.0 for r in strength}
    for _ in range(SIMS):
        shock = {r: random.gauss(1.0, TEAM_SEASON_SHOCK) for r in strength}
        wins = {r: 0 for r in strength}
        pts = {r: 0.0 for r in strength}
        for w in weeks:
            wk_scores = {}
            for a, b in sched[w]:
                sa = random.gauss(strength[a][w][0]*shock[a], strength[a][w][1])
                sb = random.gauss(strength[b][w][0]*shock[b], strength[b][w][1])
                pts[a] += sa
                pts[b] += sb
                wk_scores[a] = sa
                wk_scores[b] = sb
                wins[a if sa > sb else b] += 1
            # league_average_match: beat the weekly median for a second W
            med = sorted(wk_scores.values())[len(wk_scores)//2]
            for rid, sc in wk_scores.items():
                if sc > med:
                    wins[rid] += 1
        order = sorted(strength, key=lambda r: (wins[r], pts[r]), reverse=True)
        seeds = order[:pteams]
        for rid in seeds:
            playoffs[rid] += 1
        for rid in strength:
            expw[rid] += wins[rid]

        def game(x, y, table, two_week=False):
            n = 2 if two_week else 1
            sx = sum(random.gauss(table[x][0]*shock[x], table[x][1]) for _ in range(n))
            sy = sum(random.gauss(table[y][0]*shock[y], table[y][1]) for _ in range(n))
            return x if sx > sy else y

        if pteams == 6:
            w1 = game(seeds[2], seeds[5], playoff_r1_strength)   # wk14: byes apply
            w2 = game(seeds[3], seeds[4], playoff_r1_strength)
            f1 = game(seeds[0], w2, playoff_wk_strength)
            f2 = game(seeds[1], w1, playoff_wk_strength)
            champ = game(f1, f2, playoff_wk_strength, two_week=True)  # playoff_round_type 1
        else:
            champ = game(game(seeds[0], seeds[3], playoff_r1_strength),
                         game(seeds[1], seeds[2], playoff_r1_strength),
                         playoff_wk_strength, two_week=True)
        titles[champ] += 1

    print()
    print(f'{"team":17s} {"projW/26":>8s} {"playoff%":>9s} {"title%":>7s}   (record incl. median match)')
    for rid in sorted(strength, key=lambda r: -titles[r]):
        print(f'{own[rid]:17s} {expw[rid]/SIMS:8.1f} {100*playoffs[rid]/SIMS:8.1f}% {100*titles[rid]/SIMS:6.1f}%')

    print()
    print("WEEK 1 MATCHUPS:")
    for a, b in sched[1]:
        (ma, sa), (mb, sb) = strength[a][1], strength[b][1]
        p = 0.5 * (1 + math.erf((ma - mb) / (math.sqrt(sa*sa + sb*sb) * math.sqrt(2))))
        fav = own[a] if p >= .5 else own[b]
        print(f'  {own[a]:17s} {ma:5.1f}  vs  {mb:5.1f} {own[b]:17s} -> {fav} {max(p,1-p)*100:.0f}%')

    print()
    print("MY WEEK 1 START/SIT (optimal):")
    _, _, lineup = weekly_lineup(roster_sids[my_rid], by_sid, 1)
    started = {n for _, n, _ in lineup}
    for slot, name, g in lineup:
        print(f'  START {slot:5s} {name} ({g})')
    bench = sorted((dict(by_sid[s]) for s in roster_sids[my_rid]
                    if s in by_sid and by_sid[s]["name"] not in started),
                   key=lambda x: -x["ppg"])
    for x in bench:
        tag = "BYE" if x["bye"] == 1 else ""
        print(f'  sit   {x["pos"]:5s} {x["name"]} ({x["ppg"]:.1f}) {tag}')
    print()
    print("MY BYE-WEEK HITS (starter ppw by week):")
    for w in weeks:
        m = strength[my_rid][w][0]
        byes = [by_sid[s]["name"] for s in roster_sids[my_rid]
                if s in by_sid and by_sid[s]["bye"] == w and by_sid[s]["ppg"] >= 9]
        if byes:
            print(f'  wk{w:<3d} {m:5.1f} ppw  (out: {", ".join(byes)})')


if __name__ == "__main__":
    main()
