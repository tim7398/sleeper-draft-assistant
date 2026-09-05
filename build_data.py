#!/usr/bin/env python3
"""Build players.json for the draft assistant.

Joins FantasyPros half-PPR 10-team $200 auction values (1QB market) with
FantasyPros superflex ECR ranks, reprices QBs off the superflex ordering,
and maps everyone to Sleeper player_ids so live picks can be matched.

Inputs (fetched fresh each run):
  - https://draftwizard.fantasypros.com/auction/fp_nfl.jsp?scoring=HALF&teams=10&tb=200
  - https://www.fantasypros.com/nfl/rankings/half-point-ppr-superflex-cheatsheets.php
  - https://api.sleeper.app/v1/players/nfl

Output: players.json  (list sorted by superflex rank)
"""
import json
import re
import sys
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
BUDGET_POOL = 10 * 200
ROSTERED = 10 * 17

AAV_URL = "https://draftwizard.fantasypros.com/auction/fp_nfl.jsp?sport=nfl&scoring=HALF&teams=10&tb=200"
ECR_URL = "https://www.fantasypros.com/nfl/rankings/half-point-ppr-superflex-cheatsheets.php"
K_URL = "https://www.fantasypros.com/nfl/rankings/k.php"
DST_URL = "https://www.fantasypros.com/nfl/rankings/dst.php"
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

TEAM_FIX = {"JAC": "JAX", "WSH": "WAS", "LAR": "LA", "OAK": "LV", "SD": "LAC"}
NAME_ALIASES = {
    "hollywood brown": "marquise brown",
    "bam knight": "zonovan knight",
}


def fetch(url: str) -> str:
    """Fetch with retry; on total failure fall back to last good copy in cache/."""
    import hashlib
    import os
    import time

    os.makedirs("cache", exist_ok=True)
    cache_path = os.path.join("cache", hashlib.sha1(url.encode()).hexdigest()[:16])
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", errors="replace")
            with open(cache_path, "w") as f:
                f.write(body)
            return body
        except Exception as e:  # noqa: BLE001 - retry anything transient
            last_err = e
            time.sleep(2 * (attempt + 1))
    if os.path.exists(cache_path):
        print(f"WARN: using cached copy for {url} ({last_err})")
        return open(cache_path).read()
    raise last_err


def norm_name(name: str) -> str:
    n = name.lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\.?$", "", n.strip())
    n = re.sub(r"[^a-z ]", "", n)
    return " ".join(n.split())


def parse_aav(html: str) -> dict[str, dict]:
    i = html.find("id='OverallTable'")
    j = html.find("</table>", i)
    rows = re.findall(
        r"<tr pid='(\d+)' v='(\d+)' pts='(\d+)'[^>]*>.*?<td>([^<(]+) \(([A-Z]+) - ([A-Z]+)\)"
        r"(?:<span class='injury-tag'[^>]*>([A-Z]+)</span>)?",
        html[i:j],
    )
    return {
        pid: {"value": int(v), "pts": int(pts), "name": n.strip(), "team": t, "pos": pos, "inj": inj or None}
        for pid, v, pts, n, t, pos, inj in rows
    }


def parse_ecr(html: str) -> list[dict]:
    m = re.search(r"var ecrData = (\{.*?\});\n", html)
    if not m:
        sys.exit("ecrData not found — FantasyPros page layout changed")
    return json.loads(m.group(1))["players"]


def parse_var(html: str, name: str) -> dict:
    """Extract an embedded `var <name> = {...};` JSON blob (best-effort)."""
    m = re.search(rf"var {name} = (\{{.*?\}});", html)
    return json.loads(m.group(1)) if m else {}


def build() -> list[dict]:
    aav = parse_aav(fetch(AAV_URL))
    ecr_html = fetch(ECR_URL)
    ecr = parse_ecr(ecr_html)
    # upside/bust (0-10, keyed by FP player_id) and per-team-per-position SOS
    # stars (0-5) ship embedded in the free cheat-sheet page
    sentiment = parse_var(ecr_html, "sentimentScores")
    sos = parse_var(ecr_html, "sosData")
    sleeper = json.loads(fetch(SLEEPER_PLAYERS_URL))

    def sos_stars(team: str, pos: str) -> float | None:
        t = sos.get(team) or {}
        v = t.get({"DST": "dst_stars"}.get(pos, pos.lower() + "_stars"))
        return round(v, 1) if v else None

    # Sleeper lookup: normalized name -> candidates
    by_name: dict[str, list[tuple[str, dict]]] = {}
    for sid, p in sleeper.items():
        fps = set(p.get("fantasy_positions") or [p.get("position")])
        if not fps & {"QB", "RB", "WR", "TE", "K", "DEF"}:
            continue
        key = norm_name(p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}")
        by_name.setdefault(key, []).append((sid, p))

    def sleeper_match(name: str, pos: str, team: str) -> str | None:
        key = norm_name(name)
        key = NAME_ALIASES.get(key, key)
        if pos == "DST":
            # Sleeper DEF ids are team abbreviations
            return TEAM_FIX.get(team, team)
        cands = by_name.get(key, [])
        if len(cands) == 1:
            return cands[0][0]
        team = TEAM_FIX.get(team, team)
        def has_pos(p):
            return pos == p.get("position") or pos in (p.get("fantasy_positions") or [])

        for sid, p in cands:
            if has_pos(p) and (p.get("team") or "") == team:
                return sid
        for sid, p in cands:
            if has_pos(p):
                return sid
        return None

    # Dollar ladder from the 1QB market, extended with $1s to full rostered depth
    ladder = sorted((a["value"] for a in aav.values()), reverse=True)
    ladder += [1] * (ROSTERED - len(ladder))

    players = []
    for e in sorted(ecr, key=lambda x: x["rank_ecr"]):
        pid = str(e["player_id"])
        pos = e["player_position_id"]
        base = aav.get(pid)
        sf_rank = e["rank_ecr"]
        if pos == "QB":
            # reprice QBs by superflex ordering on the market ladder
            value = ladder[sf_rank - 1] if sf_rank <= len(ladder) else 1
            value = max(value, base["value"] if base else 1)
        else:
            value = base["value"] if base else 1
        players.append(
            {
                "fp_id": pid,
                "name": e["player_name"],
                "team": e["player_team_id"],
                "pos": pos,
                "sf_rank": sf_rank,
                "pos_rank": e["pos_rank"],
                "tier": e["tier"],
                "bye": e.get("player_bye_week"),
                "value": value,
                "pts": base["pts"] if base else 0,
                "rk_min": int(e.get("rank_min") or 0) or None,
                "rk_max": int(e.get("rank_max") or 0) or None,
                "up": (sentiment.get(pid) or {}).get("upside"),
                "bust": (sentiment.get(pid) or {}).get("bust"),
                "sos": sos_stars(e["player_team_id"], pos),
                "inj": base["inj"] if base else None,
                "sleeper_id": sleeper_match(e["player_name"], pos, e["player_team_id"]),
            }
        )

    # K and DST come from their own ranking pages (superflex sheet excludes them);
    # they're $1-2 players, ranks only matter for late-draft search.
    for url, pos in ((K_URL, "K"), (DST_URL, "DST")):
        for e in sorted(parse_ecr(fetch(url)), key=lambda x: x["rank_ecr"]):
            players.append(
                {
                    "fp_id": str(e["player_id"]),
                    "name": e["player_name"],
                    "team": e["player_team_id"],
                    "pos": pos,
                    "sf_rank": 1000 + len(players),
                    "pos_rank": e["pos_rank"],
                    "tier": e.get("tier", 1),
                    "bye": e.get("player_bye_week"),
                    "value": 1,
                    "pts": 0,
                    "up": None,
                    "bust": None,
                    "sos": sos_stars(e["player_team_id"], pos),
                    "inj": None,
                    "sleeper_id": sleeper_match(e["player_name"], pos, e["player_team_id"]),
                }
            )

    # Renormalize so real-money values (>$1) sum to the pool minus $1 slots
    money = [p for p in players if p["value"] > 1]
    ones = ROSTERED - len(money)
    target = BUDGET_POOL - ones
    scale = target / sum(p["value"] for p in money)
    for p in money:
        p["value"] = max(2, round(p["value"] * scale))
    return players


def sanity(players: list[dict]) -> None:
    errs = []
    top200 = players[:200]
    unmatched = [p["name"] for p in top200 if not p["sleeper_id"]]
    if len(unmatched) > 6:
        errs.append(f"too many unmatched sleeper ids in top 200: {unmatched}")
    qbs = [p for p in players if p["pos"] == "QB"][:12]
    if not all(q["value"] >= 10 for q in qbs[:8]):
        errs.append(f"top-8 QB values look too low for superflex: {[(q['name'], q['value']) for q in qbs[:8]]}")
    total = sum(p["value"] for p in players[:ROSTERED])
    if not (0.75 * BUDGET_POOL < total < 1.25 * BUDGET_POOL):
        errs.append(f"value pool off: top-{ROSTERED} sums to ${total} vs ${BUDGET_POOL}")
    if errs:
        sys.exit("SANITY FAIL:\n" + "\n".join(errs))
    print(f"sanity ok — {len(players)} players, {len(unmatched)} unmatched in top200: {unmatched}")
    print("top 10:", [(p["name"], p["pos"], p["value"]) for p in players[:10]])
    print("QB1-10:", [(p["name"], p["value"]) for p in players if p["pos"] == "QB"][:10])


if __name__ == "__main__":
    ps = build()
    sanity(ps)
    json.dump(ps, open("players.json", "w"))
    print(f"wrote players.json ({len(ps)} players)")
