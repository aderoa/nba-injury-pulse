#!/usr/bin/env python3
"""
NBA Injury Pulse — poller.

1. Discovers the newest official injury-report PDF:
     https://ak-static.cms.nba.com/referee/injury/Injury-Report_{YYYY-MM-DD}_{HH_MM}{AM|PM}.pdf
   by probing 15-minute timestamps backward from now (ET).
2. Parses the PDF table (Game Date | Game Time | Matchup | Team | Player Name
   | Current Status | Reason) with pdfplumber, handling carried-forward blank
   columns, wrapped multi-line reasons, and NOT YET SUBMITTED teams.
3. Diffs against data/injury_state.json and emits:
     - data/injury_pulse_live.json  (front-end contract: teams + recent_changes)
     - data/injury_state.json      (snapshot + bookkeeping)

Runs on GitHub Actions (cron) or locally:  python scripts/poll_injuries.py
"""

import io, json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

import pdfplumber

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STATE_PATH = os.path.join(DATA, "injury_state.json")
LIVE_PATH = os.path.join(DATA, "injury_pulse_live.json")

PDF_URL = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{hh}_{mm}{ap}.pdf"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://official.nba.com/",
    "Accept": "application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_PROBES = 130          # 15-min slots ≈ 32h of lookback
PROBE_DELAY = 0.15
RECENT_CHANGES_MAX = 300
RECENT_CHANGES_DAYS = 10

TEAMS = {  # full name -> (abbr, short)
    "Atlanta Hawks":("ATL","Hawks"),"Boston Celtics":("BOS","Celtics"),
    "Brooklyn Nets":("BKN","Nets"),"Charlotte Hornets":("CHA","Hornets"),
    "Chicago Bulls":("CHI","Bulls"),"Cleveland Cavaliers":("CLE","Cavaliers"),
    "Dallas Mavericks":("DAL","Mavericks"),"Denver Nuggets":("DEN","Nuggets"),
    "Detroit Pistons":("DET","Pistons"),"Golden State Warriors":("GSW","Warriors"),
    "Houston Rockets":("HOU","Rockets"),"Indiana Pacers":("IND","Pacers"),
    "LA Clippers":("LAC","Clippers"),"Los Angeles Clippers":("LAC","Clippers"),
    "Los Angeles Lakers":("LAL","Lakers"),"Memphis Grizzlies":("MEM","Grizzlies"),
    "Miami Heat":("MIA","Heat"),"Milwaukee Bucks":("MIL","Bucks"),
    "Minnesota Timberwolves":("MIN","Timberwolves"),"New Orleans Pelicans":("NOP","Pelicans"),
    "New York Knicks":("NYK","Knicks"),"Oklahoma City Thunder":("OKC","Thunder"),
    "Orlando Magic":("ORL","Magic"),"Philadelphia 76ers":("PHI","Sixers"),
    "Phoenix Suns":("PHX","Suns"),"Portland Trail Blazers":("POR","Trail Blazers"),
    "Sacramento Kings":("SAC","Kings"),"San Antonio Spurs":("SAS","Spurs"),
    "Toronto Raptors":("TOR","Raptors"),"Utah Jazz":("UTA","Jazz"),
    "Washington Wizards":("WAS","Wizards"),
}
STATUSES = {"out","doubtful","questionable","probable","available"}

HEADER_LABELS = ["Game Date","Game Time","Matchup","Team","Player Name","Current Status","Reason"]
COL_KEYS      = ["game_date","game_time","matchup","team","player","status","reason"]


def log(m): print(m, flush=True)

def now_et():
    return datetime.now(ET) if ET else datetime.now(timezone.utc) - timedelta(hours=5)


# ------------------------------------------------------------ discovery

def slot_url(dt):
    return PDF_URL.format(date=dt.strftime("%Y-%m-%d"),
                          hh=dt.strftime("%I"), mm=dt.strftime("%M"),
                          ap=dt.strftime("%p"))

def probe(url):
    try:
        req = Request(url, headers=HEADERS, method="HEAD")
        with urlopen(req, timeout=20) as r:
            return r.status == 200
    except HTTPError as e:
        if e.code == 405:  # HEAD not allowed somewhere — try ranged GET
            try:
                req = Request(url, headers={**HEADERS, "Range": "bytes=0-0"})
                with urlopen(req, timeout=20) as r:
                    return r.status in (200, 206)
            except Exception:
                return False
        return False
    except (URLError, TimeoutError):
        return False

def find_latest_pdf(state):
    t = now_et().replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 15)
    known = state.get("report_url")
    for _ in range(MAX_PROBES):
        url = slot_url(t)
        if probe(url):
            return url, t
        if known and url == known:
            break                      # we've reached the last known report
        t -= timedelta(minutes=15)
        time.sleep(PROBE_DELAY)
    if known:
        return known, None             # nothing newer than last time
    return None, None

def download(url):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=60) as r:
        return r.read()


# ------------------------------------------------------------ parsing

# The NBA PDF renders each header label as a single space-less token
# ("GameDate", "PlayerName", "CurrentStatus", ...). Map those tokens to keys.
HEADER_TOKENS = {
    "GameDate": "game_date", "GameTime": "game_time", "Matchup": "matchup",
    "Team": "team", "PlayerName": "player", "CurrentStatus": "status", "Reason": "reason",
}

def column_bounds(words):
    """Locate the header row by its single-token labels; return [(x_start,key)]."""
    found = {}
    header_top = None
    for w in words:
        key = HEADER_TOKENS.get(w["text"])
        if key:
            found[key] = w["x0"]
            header_top = w["top"]
    if len(found) < 5:
        return None, None
    cols = sorted((x, k) for k, x in found.items())
    return cols, header_top

def assign_col(cols, x):
    key = cols[0][1]
    for cx, ck in cols:
        if x >= cx - 4:
            key = ck
        else:
            break
    return key

def _respace(t):
    """The PDF has no space glyphs; reconstruct readable spacing."""
    if not t:
        return t
    # space between a lowercase/']' and an uppercase letter:  NewYork -> New York
    t = re.sub(r'(?<=[a-z\)\]\.;])(?=[A-Z])', ' ', t)
    # space between a letter and a digit:  Right5th -> Right 5th
    t = re.sub(r'(?<=[A-Za-z])(?=\d)', ' ', t)
    # digit followed by a letter, but NOT ordinal suffixes (5th, 2nd, 3rd, 1st)
    t = re.sub(r'(?<=\d)(?!(?:st|nd|rd|th)\b)(?=[A-Za-z])', ' ', t)
    # space after a comma if missing:  Robinson,Mitchell -> Robinson, Mitchell
    t = re.sub(r',(?=\S)', ', ', t)
    # space around a dash used as separator:  Illness-RightHand -> Illness - Right Hand
    t = re.sub(r'\s*-\s*', ' - ', t)
    # collapse doubles
    t = re.sub(r'\s{2,}', ' ', t)
    return t.strip()

ASSIGN_TOL = 30  # x tolerance for column assignment

def parse_pdf(pdf_bytes):
    """Return (entries, not_submitted_teams) parsed from the real NBA layout.

    The report has no space glyphs and wraps long Reason text across lines that
    can sit slightly ABOVE or BELOW the player's row. Strategy: identify player
    rows (a token in the Player column), then for each, gather Status from the
    same line and Reason from every reason-column token within a vertical band.
    """
    entries, not_submitted = [], []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=True, keep_blank_chars=False)
            hdr = [{"text": w["text"], "x0": w["x0"], "top": w["top"]} for w in words]
            cols, header_top = column_bounds(hdr)
            if not cols:
                continue
            colx = {k: x for x, k in cols}

            def col_of(x):
                best, bestd = None, 1e9
                for cx, ck in cols:
                    d = abs(x - cx)
                    if d < bestd:
                        bestd, best = d, ck
                return best

            body = [w for w in words if header_top is None or w["top"] > header_top + 3]
            body = [w for w in body if not w["text"].lower().startswith("page")]

            # player-row anchors: tokens assigned to the player column
            players = [w for w in body if col_of(w["x0"]) == "player"]
            players.sort(key=lambda w: w["top"])

            carry = {"game_date": "", "game_time": "", "matchup": "", "team": ""}
            for pw in players:
                ptop = pw["top"]
                same = [w for w in body if abs(w["top"] - ptop) <= 4]
                cell = {}
                for w in same:
                    k = col_of(w["x0"])
                    cell.setdefault(k, []).append(w["text"])
                def get(k):
                    return _respace(" ".join(cell.get(k, [])).strip())
                player = get("player")
                status = get("status")
                team = get("team")
                gd, gt, mu = get("game_date"), get("game_time"), get("matchup")
                # NOT YET SUBMITTED appears in the player/status area
                joined = (player + " " + status).lower()
                if "not yet submitted" in joined:
                    tm = team or carry["team"]
                    if tm:
                        not_submitted.append(tm)
                    if team: carry["team"] = team
                    continue
                if not player or not status:
                    continue
                # Reason: all reason-column tokens within a vertical band around the row
                rtoks = [w for w in body
                         if col_of(w["x0"]) == "reason" and (ptop - 9) <= w["top"] <= (ptop + 13)]
                rtoks.sort(key=lambda w: w["top"])
                reason = _respace(" ".join(t["text"] for t in rtoks))
                reason = reason.replace('\" \"', "").replace('""', "")
                # collapse "Injury/Illness - ; Illness" -> "Injury/Illness - Illness"
                reason = re.sub(r'-\s*;', '-', reason)
                reason = re.sub(r'\s{2,}', ' ', reason)
                reason = re.sub(r'-\s*-', '-', reason).strip(' -;')
                # carry-forward game columns
                for k, v in (("game_date", gd), ("game_time", gt), ("matchup", mu), ("team", team)):
                    if v:
                        carry[k] = v
                entries.append({
                    "game_date": carry["game_date"], "game_time": carry["game_time"],
                    "matchup": carry["matchup"], "team": team or carry["team"],
                    "player": player, "status": status.title(), "reason": reason,
                })
    return entries, not_submitted

def fix_name(n):
    """'James, LeBron' -> 'LeBron James' (suffix-aware)."""
    m = re.match(r"^([^,]+),\s*(.+)$", n.strip())
    if not m: return n.strip()
    last, first = m.group(1).strip(), m.group(2).strip()
    sfx = ""
    for s in (" Jr.", " Sr.", " III", " II", " IV", " Jr", " Sr"):
        if last.endswith(s):
            last, sfx = last[: -len(s)].strip(), s.strip()
            break
    return f"{first} {last}" + (f" {sfx}" if sfx else "")


# ------------------------------------------------------------ diff & emit

def build_snapshot(entries):
    """team_full -> {player -> {status, reason, game_date, matchup}} (earliest
    game date wins if a player is listed for multiple games)."""
    snap = {}
    for e in entries:
        team = e["team"]
        if team not in TEAMS:   # tolerate stray parse noise
            continue
        name = fix_name(e["player"])
        if not name or e["status"].lower() not in STATUSES:
            continue
        t = snap.setdefault(team, {})
        if name in t and t[name].get("game_date","") <= e["game_date"]:
            continue
        t[name] = {"status": e["status"], "reason": e["reason"],
                   "game_date": e["game_date"], "matchup": e["matchup"]}
    return snap

def diff(prev, cur, ts):
    changes = []
    teams = set(prev) | set(cur)
    for team in teams:
        abbr = TEAMS.get(team, (team, team))[0]
        p, c = prev.get(team, {}), cur.get(team, {})
        for name in c:
            if name not in p:
                changes.append({"ts": ts, "type": "added", "player": name, "team_abbr": abbr,
                                "status": c[name]["status"], "reason": c[name]["reason"]})
            else:
                if p[name]["status"] != c[name]["status"]:
                    changes.append({"ts": ts, "type": "status_change", "player": name, "team_abbr": abbr,
                                    "from_status": p[name]["status"], "to_status": c[name]["status"],
                                    "reason": c[name]["reason"]})
                elif (p[name].get("reason") or "") != (c[name].get("reason") or ""):
                    changes.append({"ts": ts, "type": "reason_change", "player": name, "team_abbr": abbr,
                                    "status": c[name]["status"], "reason": c[name]["reason"]})
        for name in p:
            if name not in c:
                changes.append({"ts": ts, "type": "cleared", "player": name, "team_abbr": abbr,
                                "previous_status": p[name]["status"]})
    return changes

def main():
    os.makedirs(DATA, exist_ok=True)
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)

    url, slot = find_latest_pdf(state)
    if not url:
        log("no injury report found in lookback window"); return
    if url == state.get("report_url") and state.get("snapshot"):
        log(f"no new report (latest is {url})")
        # still refresh polled_at so the page shows liveness
        if os.path.exists(LIVE_PATH):
            live = json.load(open(LIVE_PATH, encoding="utf-8"))
            live["polled_at_utc"] = datetime.now(timezone.utc).isoformat()
            json.dump(live, open(LIVE_PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        return

    log(f"new report: {url}")
    entries, not_submitted = parse_pdf(download(url))
    log(f"parsed {len(entries)} player listings; {len(not_submitted)} teams not yet submitted")
    if not entries and not not_submitted:
        log("PARSE WARNING: nothing extracted — format may have changed; keeping previous state")
        return

    cur = build_snapshot(entries)
    ts = datetime.now(timezone.utc).isoformat()
    changes = diff(state.get("snapshot", {}), cur, ts) if state.get("snapshot") is not None else []
    first_run = "snapshot" not in state
    if first_run:
        log("first run — establishing baseline, not emitting per-player changes")
        changes = []

    recent = state.get("recent_changes", [])
    recent = changes + recent
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_CHANGES_DAYS)).isoformat()
    recent = [c for c in recent if c["ts"] >= cutoff][:RECENT_CHANGES_MAX]

    report_ts = slot.strftime("%Y-%m-%d %I:%M %p ET") if slot else ""
    teams_out = []
    for full in sorted(TEAMS, key=lambda x: TEAMS[x][0]):
        if full == "Los Angeles Clippers":  # alias of LA Clippers
            continue
        players = cur.get(full) or cur.get("Los Angeles Clippers" if full == "LA Clippers" else "", {})
        plist = [{"name": n, "status": v["status"], "reason": v["reason"],
                  "game_date": v["game_date"], "matchup": v["matchup"]}
                 for n, v in sorted(players.items())]
        order = {"Out":0,"Doubtful":1,"Questionable":2,"Probable":3,"Available":4}
        plist.sort(key=lambda p,o=order: (o.get(p["status"],9), p["name"]))
        teams_out.append({"team_abbr": TEAMS[full][0], "team_full": full,
                          "last_submitted_ts": None,
                          "not_yet_submitted": full in not_submitted,
                          "players": plist})

    live = {"polled_at_utc": ts, "report_ts": report_ts, "report_url": url,
            "teams": teams_out, "recent_changes": recent}
    json.dump(live, open(LIVE_PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"report_url": url, "report_ts": report_ts, "snapshot": cur,
               "recent_changes": recent, "updated_at": ts},
              open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log(f"wrote live json: {sum(len(t['players']) for t in teams_out)} listings, "
        f"{len(changes)} new change(s), {len(recent)} in feed")


if __name__ == "__main__":
    main()
