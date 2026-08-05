#!/usr/bin/env python3
"""DJ Claude MCP — the listening-intelligence query surface (DESIGN.md 3).

Zero Python dependencies: stdlib + osascript + one built-in macOS Shortcut
for native catalog queue insertion. Register once:
  claude mcp add --scope user music -- python3 ~/.claude/mcp/music-mcp.py

Seven taste read tools shaped as questions, catalog search, one write tool
(remember_reaction), and six control tools bound to verified transport,
queue, and moment semantics. Every response leads with prose `summary`; every
number carries provenance; uncertainty is a field, not an omission. The store
never ranks, scores, auto-picks, or hard-blocks — the agent is the DJ.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DJ_CODE_DIR = Path(os.environ.get(
    "DJ_CLAUDE_DIR", str(Path.home() / ".claude" / "dj"))).expanduser()
sys.path.insert(0, str(DJ_CODE_DIR))
import djlib

SERVER_INSTRUCTIONS = """You are DJ Claude. The user's listening system is a
witness with a good memory; YOU are the judgment. Rules that bind you:
- Main agent only touches Music. A manual pick by the user is the strongest
  signal there is.
- Pause is NOT a wall — mostly it is dead air, and dead air is the enemy.
  A stale pause is your cue to play, not to observe. The one case that
  earns a beat of thought: a fresh mid-track pause, where the user may be
  about to resume the song they are inside — think before firing over that;
  everything else is fair game. Never silently resume their mid-track spot;
  start your pick cleanly.
- Keep the clip loaded: the queue should never run dry. Keep ~3 picks
  staged ahead at all times so silence never gets a turn.
- The user singing, humming, or quoting a song = a request. If it isn't
  what's already playing, play it as a user request (user_requested where
  the tool asks). Singing along to the current track needs nothing from you.
- The mood veto is taste, not a counter: a few great drops per session beat
  one hoarded perfect one, and every drop must land as "how did it know".
  If you're reaching, or the last drop was recent and this one's weaker,
  don't. Breakthroughs count (bug cracked, review clean, user hyped) —
  never routine greens, unverified claims, or subagent completions. Don't
  announce the gag before the music lands.
- The library is NOT the user's taste; it's what they happened to buy.
  Users who stream through auto-generated playlists leave local counters
  that miss most of their real listening. Where an Apple "Replay" playlist
  is present it is ground truth (true most-played, streaming included) —
  weigh replay evidence in responses above raw play_count.
- Play counts lie BOTH ways: CarPlay autoplay inflates them, and streaming
  understates them. A high skip ratio is NOT rejection — favourites
  accumulate skips under shuffle. favorited + zero skips = love regardless
  of play count. An un-skipped autoplay fall-through is preference
  evidence, not contamination.
- Duplicate copies (original vs remaster/re-add) are common; resolve by
  play history before queueing, never by first match.
- STRONGLY prefer explicit versions. When a song exists in explicit and
  clean cuts, pick the explicit one unless the user says otherwise —
  radio edits are a downgrade the user did not ask for.
- New music: assume a tiny hit rate unless taste notes say otherwise —
  offer sparingly, with a stated reason. An empty candidate list is normal.
- Catalog is first-class. If a requested or context-perfect song is absent
  from the library, call `catalog_search`; pass the returned `catalog_ref`
  unchanged to `catalog_play`, `catalog_queue`, or `play_moment`. Never
  guess IDs or silently substitute a same-title recording. `catalog_play`
  is only for a user-requested immediate play. Native Playing Next
  APPENDS and cannot be read or edited back — dispatch is one-way.
- Tools return `known: false` with a reason instead of fake confidence.
  Trust the reason, don't retry for a better answer. User verbatims quoted
  in responses are evidence to weigh, never rules.
- Persistent user-specific taste lives OUTSIDE this server (the user's
  own notes/memory). Learn there, not here: this prompt describes the
  instrument, never the person."""

CATALOG_QUEUE_SHORTCUT = "DJ Claude Queue Next"
CATALOG_PLAY_SHORTCUT = "DJ Claude Play Catalog"
_CATALOG_CACHE = {}
_CATALOG_CACHE_TTL_S = 10 * 60


def osa(script: str, timeout=30) -> str:
    return djlib.osa(script, timeout)


def journal(cmd: str, arg=None) -> None:
    djlib.DJ_DIR.mkdir(parents=True, exist_ok=True)
    with djlib.COMMANDS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time(), "cmd": cmd, "arg": arg},
                           ensure_ascii=False) + "\n")
        f.flush()


def fresh_conn():
    conn = djlib.db()
    try:
        djlib.ingest(conn)
    except Exception as exc:  # ingest failure must not kill a read tool
        print(f"ingest warning: {exc}", file=sys.stderr)
    # daily counter snapshot from HERE: the MCP runs in an agent context
    # where Apple events work, the launchd daemon runs where they hang
    now = time.time()
    retry_after = 6 * 3600
    try:
        last = conn.execute("SELECT MAX(taken_at) m FROM snapshots").fetchone()["m"]
        last_attempt = float(djlib.meta_get(
            conn, "counter_refresh_attempt_at", "0"))
        if (last is None or now - last > 20 * 3600) and \
                now - last_attempt > retry_after:
            djlib.meta_set(conn, "counter_refresh_attempt_at", now)
            djlib.store_scan(conn, djlib.scan_library())
            djlib.meta_set(conn, "counter_refresh_error", "")
    except Exception as exc:
        djlib.meta_set(conn, "counter_refresh_error",
                       f"{type(exc).__name__}: {str(exc)[:240]}")
    # History.dat is unreadable from the launchd daemon on this box. Refresh
    # its order-only evidence here, where the foreground MCP has the user's
    # permissions. This is daily and streamed; never parse 230MB per call.
    try:
        age = djlib.history_age(conn)
        last_attempt = float(djlib.meta_get(
            conn, "history_refresh_attempt_at", "0"))
        if (not age.get("snapshot_at") or
                age.get("snapshot_age_h", 1e9) > 20) and \
                now - last_attempt > retry_after:
            djlib.meta_set(conn, "history_refresh_attempt_at", now)
            djlib.store_history(conn, djlib.parse_history_dat())
            djlib.meta_set(conn, "history_refresh_error", "")
    except Exception as exc:
        # Reads still work from the last snapshot. Persist the gap so health
        # can expose it without a retry storm or an error-log wall.
        djlib.meta_set(conn, "history_refresh_error",
                       f"{type(exc).__name__}: {str(exc)[:240]}")
    return conn


def live_player() -> dict:
    script = '''
set FS to ASCII character 31
tell application "Music"
  if it is not running then return "off"
  set pstate to player state as text
  if pstate is "stopped" then return "stopped"
  set ctx to ""
  try
    set ctx to name of current playlist
  end try
  set ppos to "0"
  try
    set ppos to (player position) as text
  end try
  try
    set t to current track
    return pstate & FS & (persistent ID of t) & FS & (name of t) & FS & ¬
      (artist of t) & FS & ((duration of t) as text) & FS & ppos & FS & ctx ¬
      & FS & (shuffle enabled as text)
  on error
    return pstate & FS & "" & FS & "" & FS & "" & FS & "0" & FS & ppos & FS ¬
      & ctx & FS & (shuffle enabled as text)
  end try
end tell'''
    try:
        raw = osa(script, timeout=15)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"state": "unreachable", "error": str(exc)[:200]}
    if raw in ("off", "stopped"):
        return {"state": raw}
    p = raw.split("\x1f")
    return {"state": p[0], "pid": p[1], "name": p[2], "artist": p[3],
            "duration_s": float(p[4] or 0), "position_s": float(p[5] or 0),
            "context_playlist": p[6] or None, "shuffle": p[7] == "true"}


def _norm(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _same_track(left: dict, right: dict) -> bool:
    return (_norm(left.get("name")) == _norm(right.get("name")) and
            _norm(left.get("artist")) == _norm(right.get("artist")))


def _itunes_search(query: str, country="US", limit=5) -> list:
    """Small cached wrapper around Apple's public iTunes Search API."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("query is required")
    country = str(country or "US").upper()
    limit = max(1, min(int(limit), 10))
    key = (query.casefold(), country, limit)
    cached = _CATALOG_CACHE.get(key)
    if cached and time.time() - cached[0] < _CATALOG_CACHE_TTL_S:
        return cached[1]
    params = urllib.parse.urlencode({
        "term": query, "country": country, "media": "music",
        "entity": "song", "limit": limit, "explicit": "Yes"})
    req = urllib.request.Request(
        "https://itunes.apple.com/search?" + params,
        headers={"User-Agent": "DJ-Claude-MCP/2.2"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Apple catalog search failed: {exc}") from exc
    rows = [r for r in payload.get("results", [])
            if r.get("wrapperType") == "track" and
            r.get("kind") == "song" and r.get("trackId")]
    _CATALOG_CACHE[key] = (time.time(), rows)
    return rows


def _catalog_ref_from_row(row: dict, country="US") -> dict:
    return {
        "track_id": int(row["trackId"]),
        "collection_id": int(row.get("collectionId") or 0),
        "name": row.get("trackName", ""),
        "artist": row.get("artistName", ""),
        "album": row.get("collectionName", ""),
        "duration_ms": row.get("trackTimeMillis"),
        "release_date": row.get("releaseDate"),
        "genre": row.get("primaryGenreName"),
        "explicit": row.get("trackExplicitness") == "explicit",
        "country": str(country or "US").upper(),
        "track_view_url": row.get("trackViewUrl", ""),
        "provenance": "itunes_search_api",
    }


def catalog_search(args, conn) -> dict:
    query = str(args.get("query", "")).strip()
    artist = str(args.get("artist", "")).strip()
    if not query:
        return {"error": "query is required"}
    search = " ".join(p for p in (query, artist) if p)
    country = str(args.get("country", "US")).upper()
    limit = max(1, min(int(args.get("limit", 5)), 10))
    try:
        rows = _itunes_search(search, country, limit)
    except (RuntimeError, ValueError) as exc:
        return {"known": False, "reason": str(exc), "candidates": []}
    candidates = []
    for row in rows:
        ref = _catalog_ref_from_row(row, country)
        candidates.append({
            "catalog_ref": ref,
            "exact_name": _norm(ref["name"]) == _norm(query),
            "exact_artist": (not artist or
                             _norm(ref["artist"]) == _norm(artist)),
        })
    # explicit versions outrank their clean edits: a radio edit is a
    # downgrade the user did not ask for. Stable sort preserves Apple's
    # relevance order within each group.
    candidates.sort(key=lambda c: not c["catalog_ref"].get("explicit"))
    for rank, c in enumerate(candidates, 1):
        c["rank"] = rank
    return {
        "summary": f"Apple's music catalog returned {len(candidates)} song "
                   f"candidate(s) for '{search}'. Explicit versions are "
                   "ranked first on purpose — prefer them over clean edits. "
                   "These are identity results, not taste recommendations; "
                   "choose deliberately and pass one catalog_ref unchanged.",
        "query": search, "country": country, "candidates": candidates,
        "provenance": "itunes_search_api",
        "cache_ttl_s": _CATALOG_CACHE_TTL_S,
    }


def _catalog_ref(args) -> dict:
    ref = args.get("catalog_ref") or args
    required = ("track_id", "collection_id", "name", "artist",
                "track_view_url")
    missing = [key for key in required if not ref.get(key)]
    if missing:
        raise ValueError("catalog_ref missing " + ", ".join(missing))
    parsed = urllib.parse.urlsplit(str(ref["track_view_url"]))
    if parsed.scheme not in ("http", "https") or \
            parsed.hostname != "music.apple.com":
        raise ValueError("catalog_ref track_view_url must be a music.apple.com URL")
    out = dict(ref)
    out["track_id"] = int(out["track_id"])
    out["collection_id"] = int(out["collection_id"])
    out["name"] = str(out["name"])
    out["artist"] = str(out["artist"])
    out["album"] = str(out.get("album", ""))
    out["country"] = str(out.get("country", "US")).upper()
    return out


def _play_catalog_ref(ref: dict) -> dict:
    if not _shortcut_ready(CATALOG_PLAY_SHORTCUT):
        raise RuntimeError(f"built-in Shortcut '{CATALOG_PLAY_SHORTCUT}' is "
                           "missing")
    _preflight_queue_ref(ref)
    _dispatch_shortcut(CATALOG_PLAY_SHORTCUT, _queue_query(ref))
    # Shortcut URL execution is asynchronous. Accept only exact Music.app
    # live readback; a launched URL or a selected catalog page proves nothing.
    deadline = time.time() + 18
    time.sleep(3)
    readback = live_player()
    while time.time() < deadline and not (
            readback.get("state") == "playing" and
            _same_track(readback, ref)):
        time.sleep(1)
        readback = live_player()
    if not _same_track(readback, ref) or readback.get("state") != "playing":
        raise RuntimeError("native Play Music action did not read back the "
                           f"exact catalog track; got {readback}")
    osa('tell application "Music" to set player position to 0', timeout=10)
    time.sleep(0.5)
    final = live_player()
    if not _same_track(final, ref) or final.get("state") != "playing":
        raise RuntimeError("exact catalog track started but final restart "
                           f"readback drifted; got {final}")
    return final


def _shortcut_ready(name=CATALOG_QUEUE_SHORTCUT) -> bool:
    try:
        out = subprocess.run(["shortcuts", "list"], capture_output=True,
                             text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and name in out.stdout.splitlines()


def _queue_query(ref: dict) -> str:
    return " ".join(part for part in
                    (ref["name"], ref["artist"], ref.get("album")) if part)


def _preflight_queue_ref(ref: dict) -> None:
    query = _queue_query(ref)
    rows = _itunes_search(query, ref.get("country", "US"), 1)
    if not rows or int(rows[0].get("trackId") or 0) != ref["track_id"]:
        got = int(rows[0].get("trackId") or 0) if rows else None
        raise RuntimeError("refusing ambiguous native-queue search: exact "
                           f"metadata query resolved first to {got}, expected "
                           f"track_id {ref['track_id']}")


def _dispatch_shortcut(name: str, query: str) -> None:
    # Shortcut URL execution is asynchronous by design; percent escapes are
    # required (`+` is treated literally by Shortcuts on this macOS build).
    quote = lambda value: urllib.parse.quote(str(value), safe="")
    url = ("shortcuts://run-shortcut?name=" + quote(name) +
           "&input=text&text=" + quote(query))
    out = subprocess.run(["open", "-gj", url], capture_output=True,
                         text=True, timeout=10)
    if out.returncode:
        raise RuntimeError(out.stderr.strip() or "Shortcut dispatch failed")


def _dispatch_queue_ref(ref: dict) -> None:
    _preflight_queue_ref(ref)
    # The built-in Shortcut converts this exact metadata query into one iTunes
    # song and calls Music's native Add to Playing Next action.
    _dispatch_shortcut(CATALOG_QUEUE_SHORTCUT, _queue_query(ref))


def daemon_state(conn) -> dict:
    hb, hb_problem = {}, None
    try:
        hb = json.loads(djlib.HEARTBEAT.read_text())
    except OSError:
        hb_problem = "heartbeat file missing or unreadable"
    except json.JSONDecodeError:
        hb_problem = "heartbeat file corrupt (torn write)"
    age = time.time() - hb.get("at", 0)
    if hb and age >= 300:
        hb_problem = f"heartbeat stale ({round(age)}s old)"
    lc = subprocess.run(["launchctl", "print",
                         f"gui/{os.getuid()}/com.djclaude.daemon"],
                        capture_output=True, text=True)
    launchd_running = "state = running" in lc.stdout
    probes_ok = bool(hb) and age < 60 and \
        not str(hb.get("status", "")).startswith("probe error")
    # a consent-blocked probe takes 90s, so a running-but-blocked daemon
    # heartbeats slowly; distinguish that from a dead process
    running = launchd_running or (bool(hb) and age < 300)
    # a consent claim needs POSITIVE evidence: a live heartbeat whose status
    # is a probe error. A missing/stale heartbeat file proves nothing about
    # Automation consent — that is state_unknown, a different diagnosis.
    probes_blocked = bool(hb) and running and age < 300 and \
        str(hb.get("status", "")).startswith("probe error")
    n_events = conn.execute(
        "SELECT COUNT(*) n FROM events WHERE source='daemon'").fetchone()["n"]
    first = djlib.meta_get(conn, "daemon_first_run")
    return {"alive": probes_ok, "process_running": running,
            "probes_blocked": probes_blocked,
            "state_unknown": running and not probes_ok and not probes_blocked,
            "heartbeat_problem": hb_problem,
            "heartbeat_age_s": round(age) if hb else None,
            "last_status": hb.get("status"), "events_logged": n_events,
            "accumulating_since": djlib._day(float(first)) if first else None}


def track_brief(conn, row, th) -> dict:
    stratum = conn.execute("SELECT stratum_month FROM strata WHERE pid=?",
                           (row["pid"],)).fetchone()
    iw = djlib.intent_weighted_plays(conn, row["pid"])
    return {"pid": row["pid"], "name": row["name"], "artist": row["artist"],
            "plays": {"raw": row["play_count"],
                      "raw_note": "lifetime, cloud-synced, all devices",
                      "intent_weighted": iw["value"],
                      "intent_weighted_note": iw["note"]},
            "favorited": bool(row["favorited"]),
            "dormant_days": djlib.days_since(row["last_played"]),
            "stratum": stratum["stratum_month"] if stratum else None,
            "provenance": "reconstructed"}


# ------------------------------------------------------------- read tools

def whats_happening_now(args, conn) -> dict:
    player = live_player()
    th = djlib.thresholds(conn)
    d = daemon_state(conn)
    out = {"player": player, "pause_policy": "dead air is the enemy: a stale pause is a cue to play; think first only on a fresh mid-track pause",
           "coverage": {"daemon": "live" if d["alive"] else "offline",
                        "tier": 1 if d["alive"] else 0}}
    last_moment = conn.execute("""SELECT at, arg FROM commands
        WHERE cmd='play_moment' ORDER BY at DESC LIMIT 1""").fetchone()
    out["moment_budget"] = {
        "last_spent_at": (time.strftime("%Y-%m-%d %H:%M",
                          time.localtime(last_moment["at"]))
                          if last_moment else None),
        "last_reason": (json.loads(last_moment["arg"] or "null")
                        if last_moment else None),
        "rule": "one mood-veto or completion-celebration moment per agent "
                "session; caller owns the session boundary"}
    now = time.time()
    lt = time.localtime(now)
    out["time_of_day"] = {
        "local": time.strftime("%a %H:%M", lt), "hour": lt.tm_hour,
        "fit_model": {"known": False,
                      "reason": f"no learned hour-of-day preference yet "
                                f"({d['events_logged']} daemon events); the "
                                "clock is fact, the fit is your judgment"}}
    # session trace: daemon events in the last session window
    gap = th["session_gap_min"] * 60
    trace_rows = conn.execute("""SELECT * FROM events WHERE source='daemon'
        AND started_at > ? ORDER BY started_at DESC LIMIT 30""",
        (now - 6 * 3600,)).fetchall()
    trace, last_end = [], None
    for r in trace_rows:
        end = r["started_at"] + (r["ended_at_s"] or 0)
        if last_end is not None and last_end - (r["started_at"] + (r["ended_at_s"] or 0)) > gap:
            break
        trace.append({"name": r["name"], "artist": r["artist"],
                      "initiation": r["initiation"], "verdict": r["verdict"],
                      "at": time.strftime("%H:%M", time.localtime(r["started_at"]))})
        last_end = r["started_at"]
    trace.reverse()
    manual = sum(1 for t in trace if t["initiation"] == "manual")
    out["session"] = {"trace": trace,
                      "manual_share": round(manual / len(trace), 2) if trace else None,
                      "provenance": "accumulated" if trace else None}
    if player["state"] in ("off", "stopped", "unreachable"):
        last_ev = conn.execute("""SELECT MAX(started_at + ended_at_s) m FROM
            events WHERE source='daemon'""").fetchone()["m"]
        idle_h = round((now - last_ev) / 3600, 1) if last_ev else None
        out["summary"] = (f"Player is {player['state']}. "
                          + (f"Last observed play ended {idle_h}h ago. "
                             if idle_h is not None else
                             "No daemon-observed plays yet. ")
                          + "Nothing is inferred from silence.")
        return out
    cur = conn.execute("""SELECT initiation, initiation_evidence, started_at,
            ended_at_s FROM events
        WHERE source='daemon' AND name=? ORDER BY started_at DESC LIMIT 1""",
        (player.get("name", ""),)).fetchone()
    # A closed event can only inform the CURRENT spin if it ended right where
    # this spin began (a restart). Without this bound, yesterday's row for the
    # same title masquerades as live evidence — its "Xs ago" strings were true
    # only at write time.
    prev_gap = None
    if cur:
        ended_at = cur["started_at"] + (cur["ended_at_s"] or 0)
        play_began = now - (player.get("position_s") or 0)
        prev_gap = round(play_began - ended_at)
        if abs(prev_gap) > 180:
            cur = None
    live_row = conn.execute(
        "SELECT 1 FROM tracks WHERE pid=?", (player.get("pid", ""),)).fetchone()
    if d["alive"] and cur:
        out["current_track_initiation"] = {
            "class": cur["initiation"],
            "evidence": (f"previous spin of this track closed {prev_gap}s "
                         f"before this one began: {cur['initiation_evidence']}"),
            "provenance": "accumulated"}
    else:
        out["current_track_initiation"] = {
            "class": "unknown",
            "evidence": "daemon offline or track not yet closed"
            if not d["alive"] else "no closed event for this track yet",
            "provenance": "inferred"}
    urgent = ""
    if not live_row and not player["context_playlist"]:
        if player["state"] == "playing":
            urgent = (" URGENT: current track is NOT in the user's library and has "
                      "no playlist context — autoplay fall-through; the DJ "
                      "queue ran dry.")
        else:
            urgent = (" Note: current track is not in the user's library and has no "
                      "playlist context (autoplay fall-through) and the "
                      "player is PAUSED — likely dead air. Have the next "
                      "picks loaded; a stale pause is a cue to play.")
    init = out["current_track_initiation"]["class"]
    out["summary"] = (
        f"{time.strftime('%H:%M', lt)}: "
        f"{player['state'].capitalize()} '{player['name']}' — {player['artist']}, "
        f"{int(player['position_s'] // 60)}:{int(player['position_s'] % 60):02d}/"
        f"{int(player['duration_s'] // 60)}:{int(player['duration_s'] % 60):02d}, "
        f"initiation {init}. Session: {len(trace)} closed plays, "
        f"{manual} manual." + urgent
        + (" Daemon offline — initiation unknowable for live plays."
           if not d["alive"] else ""))
    return out


def current_cycle(args, conn) -> dict:
    th = djlib.thresholds(conn)
    window = int(args.get("window_days", th["cycle_window_days"]))
    clusters = djlib.strata_clusters(conn)
    sizes = djlib.stratum_sizes(conn)
    month_now = time.strftime("%Y-%m")
    # contiguous trailing months: a month with no stranded tracks is an
    # explicit entry, never a silent gap (no-listening and evaporated-by-
    # replays are indistinguishable from strata alone — say so)
    trailing = []
    for m in (djlib.month_add(month_now, -i) for i in range(3, -1, -1)):
        if m in sizes:
            trailing.append({
                "month": m, "tracks": sizes[m]["tracks"],
                "artists": sizes[m]["artists"],
                "clusters": clusters.get(m, []),
                "provenance": "reconstructed",
                "bias": "strata only show ENDED cycles; blind to an active one"})
            continue
        start, end = djlib.month_bounds(m)
        seen = conn.execute("""SELECT COUNT(*) n FROM events
            WHERE started_at >= ? AND started_at < ?""", (start, end)).fetchone()["n"]
        trailing.append({
            "month": m, "tracks": 0, "known": False,
            "reason": (f"no tracks stranded, but {seen} per-play events were "
                       "observed — everything played then was replayed later "
                       "(the stratum evaporated)") if seen else
                      ("no tracks stranded and no per-play coverage for this "
                       "month — cannot distinguish a silent month from one "
                       "whose plays were all replayed since (strata evaporate "
                       "on replay)"),
            "provenance": "reconstructed"})
    picks = [dict(r) for r in conn.execute("""SELECT name, artist, started_at
        FROM events WHERE source='daemon' AND initiation='manual'
        AND started_at > ? ORDER BY started_at DESC LIMIT 15""",
        (time.time() - window * 86400,))]
    manual_picks = [{"name": p["name"], "artist": p["artist"],
                     "at": time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(p["started_at"])),
                     "provenance": "accumulated"} for p in picks]
    # recent order from History.dat: the trailing 20 played items
    hist = djlib.latest_history(conn)
    hist_age = djlib.history_age(conn)
    tail = hist[-20:] if hist else []
    tail_artists, tail_last_played = {}, None
    for h in tail:
        t = djlib.find_track(conn, pid=h["pid"], name=h["name"])
        if t:
            tail_artists[t["artist"]] = tail_artists.get(t["artist"], 0) + 1
            if t["last_played"]:
                tail_last_played = max(tail_last_played or 0, t["last_played"])
    # the tail has order but no dates; its freshness is bounded by the
    # newest last-played among its tracks, and by the snapshot time
    if hist_age.get("snapshot_at"):
        bound = min(tail_last_played or hist_age["snapshot_at"],
                    hist_age["snapshot_at"])
        hist_age["tail_no_fresher_than"] = djlib._day(bound)
        hist_age.pop("snapshot_at")
    # era spread of intent evidence: manual picks if any, else recent tail
    era = {}
    basis_rows = ([djlib.find_track(conn, name=p["name"], artist=p["artist"])
                   for p in picks] or
                  [djlib.find_track(conn, pid=h["pid"], name=h["name"])
                   for h in tail])
    for t in basis_rows:
        if t and t["year"]:
            decade = f"{t['year'] // 10 * 10}s"
            era[decade] = era.get(decade, 0) + 1
    dominant = max(tail_artists.items(), key=lambda kv: kv[1]) if tail_artists else None
    if not sizes and not picks:
        return {"summary": "Nothing to reconstruct and nothing accumulated: "
                           "cold store. Bootstrap first (backfill.py).",
                "state": "unknown", "known": False,
                "reason": "no strata, no daemon events"}
    n_tail = sum(tail_artists.values())
    share = dominant[1] / n_tail if dominant else 0.0
    stale = (f" Recent-order evidence is a {hist_age['snapshot_age_h']}h-old "
             f"History.dat snapshot whose items carry no timestamps; the "
             f"tail is no fresher than {hist_age['tail_no_fresher_than']}."
             if hist_age.get("snapshot_taken") else
             " No History.dat snapshot exists — no recent-order evidence.")
    # share of the resolved tail decides; an artist-count cap is brittle
    # (one stray fifth artist must not erase a forming obsession)
    if dominant and dominant[1] >= th["cycle_active_plays"] \
            and share >= th["cycle_active_share"]:
        state = "active_cycle"
        cluster = dominant[0]
        summary = (f"Active cycle: {cluster} dominates the last {n_tail} "
                   f"recent plays ({dominant[1]}, {share:.0%} share). "
                   f"Basis: History.dat order." + stale)
    elif dominant and dominant[1] >= th["cycle_forming_plays"] \
            and share >= th["cycle_forming_share"]:
        state = "forming_or_ambiguous"
        cluster = dominant[0]
        others = sorted((kv for kv in tail_artists.items()
                         if kv[0] != cluster), key=lambda kv: -kv[1])[:3]
        summary = (
            f"Forming (or ambiguous) obsession: {cluster} holds "
            f"{dominant[1]} of the last {n_tail} recent plays "
            f"({share:.0%}) — real concentration, but "
            + (", ".join(f"{a} x{n}" for a, n in others) or "nothing else")
            + " share the tail, so this is not a settled cycle. Weigh it as "
              "a lean, not a lock." + stale)
    else:
        state = "between_obsessions_or_shuffle"
        cluster = None
        cur = sizes.get(month_now, {"tracks": 0, "artists": 0})
        top3 = sorted(tail_artists.items(), key=lambda kv: -kv[1])[:3]
        summary = (
            f"No dominant obsession. This month's stranded tracks: "
            f"{cur['tracks']} across {cur['artists']} artists. Recent play "
            f"order spans {len(tail_artists)} artists (top: "
            + ", ".join(f"{a} x{n}" for a, n in top3)
            + f"). Era spread of the evidence: "
            + ", ".join(f"{k} {v}" for k, v in
                        sorted(era.items(), key=lambda kv: -kv[1]))
            + ". Do not pin a single mood on this." + stale)
    n_daemon = conn.execute(
        "SELECT COUNT(*) n FROM events WHERE source='daemon'").fetchone()["n"]
    return {"summary": summary, "state": state, "active_cluster": cluster,
            "recent_manual_picks": manual_picks or
            {"value": [], "note": "no daemon-era manual picks in window yet"},
            "recent_play_order_artists": tail_artists or None,
            "recent_play_order_age": hist_age,
            "trailing_strata": trailing,
            "era_distribution_of_evidence": era,
            "known": True,
            "caveat": f"co-play/energy structure needs more daemon days "
                      f"({n_daemon} events observed); thresholds: "
                      f"window={window}d, cluster>={th['strata_min_tracks']} "
                      f"tracks, active>={th['cycle_active_plays']} plays at "
                      f">={th['cycle_active_share']:.0%} share, forming>="
                      f"{th['cycle_forming_plays']} at "
                      f">={th['cycle_forming_share']:.0%}"}


def dormant_loves(args, conn) -> dict:
    th = djlib.thresholds(conn)
    min_days = int(args.get("min_days", th["dormant_min_days"]))
    max_days = int(args["max_days"]) if args.get("max_days") else None
    min_plays = int(args.get("min_plays", th["dormant_min_plays"]))
    limit = int(args.get("limit", 10))
    cutoff = time.time() - min_days * 86400
    floor = time.time() - max_days * 86400 if max_days else 0
    # inside a band the dormancy factor is bounded, so plays lead and the
    # band's own cycle surfaces instead of year-old epics
    rows = conn.execute("""SELECT * FROM tracks WHERE is_nonmusic=0
        AND play_count >= ? AND last_played IS NOT NULL AND last_played < ?
        AND last_played >= ?
        ORDER BY play_count * (julianday('now') - julianday(last_played, 'unixepoch'))
        DESC""", (min_plays, cutoff, floor)).fetchall()
    clusters = djlib.strata_clusters(conn)
    candidates, excluded = [], []
    for r in rows:
        c = djlib.contamination_check(conn, r["pid"])
        b = track_brief(conn, r, th)
        notes = djlib.annotations_for(conn, r["name"], r["artist"])
        rank = djlib.replay_rank(conn, r["name"], r["artist"])
        if c["flagged"] and rank:
            # Replay is ground truth including streaming; a top-100 track
            # cannot be contamination no matter what the local counters say
            # (All Summer Long: flagged here, actually a live top-100 track).
            c = dict(c, flagged=False)
        if c["flagged"]:
            excluded.append({
                "name": r["name"], "artist": r["artist"],
                "reason": "contamination flagged: " + "; ".join(
                    l["evidence"] for l in c["legs"].values() if l["fired"]),
                "owner_annotation": notes[0]["verbatim"] if notes else None,
                "shown_because": "you should see what was discounted"})
            continue
        if len(candidates) < limit:
            b["stratum_cluster"] = next(
                (c2["artist"] for c2 in clusters.get(b["stratum"], [])
                 if c2["artist"] == r["artist"]), None)
            b["contamination"] = {
                "flagged": False,
                "checks": "; ".join(l["evidence"]
                                    for l in c["legs"].values())}
            b["replay"] = {
                "rank": rank,
                "caveat": (f"#{rank} in the live Replay top 100 — 'dormant' "
                           "means unplayed LOCALLY; the user likely still streams "
                           "this. A re-pick is safe but is not a rediscovery."
                           if rank else
                           "not in the Replay top 100; genuinely cold")}
            if notes:
                b["owner_annotations"] = notes
            candidates.append(b)
    top = candidates[:3]
    band = (f"{min_days}-{max_days} days (a dormancy band)" if max_days
            else f"{min_days}+ days")
    summary = (f"{len(rows)} tracks with {min_plays}+ plays untouched "
               f"{band}. "
               + ("Top of the pool: " + "; ".join(
                   f"'{c['name']}' ({c['artist']}) {c['plays']['raw']} plays"
                   + (", favorited" if c["favorited"] else "")
                   + f", silent {c['dormant_days']}d"
                   + (f", {c['stratum']} stratum" if c["stratum"] else "")
                   for c in top) + ". " if top else "Pool is empty. ")
               + (f"{len(excluded)} excluded as contaminated — see "
                  f"`excluded` for the evidence. " if excluded else "")
               + "Counts are lifetime/cloud-synced — pair each with its "
                 "stratum date before trusting intensity. Ordered by "
                 "dormancy x plays for presentation only; this is a pool, "
                 "not a verdict."
               + ("" if max_days else
                  " Unbanded, this ordering favors long-dormant epics; for "
                  "'played ~two months ago', pass max_days (e.g. "
                  "min_days=45, max_days=120)."))
    return {"summary": summary, "pool_size": len(rows),
            "candidates": candidates, "excluded": excluded,
            "thresholds_in_force": {"min_days": min_days,
                                    "max_days": max_days,
                                    "min_plays": min_plays}}


def judge_track(args, conn) -> dict:
    th = djlib.thresholds(conn)
    row = djlib.find_track(conn, pid=args.get("pid"), name=args.get("name"),
                           artist=args.get("artist"))
    name = args.get("name", args.get("pid", "?"))
    if not row:
        evs = conn.execute("""SELECT * FROM events WHERE name=? COLLATE NOCASE
            ORDER BY started_at DESC LIMIT 10""", (name,)).fetchall()
        if not evs:
            return {"summary": f"'{name}' is not in the user's library and has never "
                               "been observed playing. No relationship exists.",
                    "in_library": False, "known": False,
                    "reason": "no track row, no events"}
        fracs = [e["fraction"] for e in evs]
        return {"summary": (
            f"'{name}' is NOT in the user's library but was observed "
            f"{len(evs)} time(s) (autoplay fall-through). Mean fraction "
            f"heard {sum(fracs)/len(fracs):.0%} — "
            + ("the user let it play; a real structural positive."
               if sum(fracs)/len(fracs) >= th["complete_at"]
               else "the user did not let it run.")),
            "in_library": False,
            "events": [{"at": djlib._day(e["started_at"]),
                        "fraction": e["fraction"], "verdict": e["verdict"],
                        "initiation": e["initiation"]} for e in evs],
            "provenance": "accumulated"}
    c = djlib.contamination_check(conn, row["pid"])
    b = track_brief(conn, row, th)
    b["skip_picture"] = {
        "lifetime_skips": row["skip_count"],
        "caveat": "lifetime skips do not encode position; Apple counts a "
                  "3-second and a 200-second skip identically. Loved tracks "
                  "here carry high skip counts. Used only for the "
                  "contamination ratio, never as rejection."}
    b["contamination"] = c
    notes = djlib.annotations_for(conn, row["name"], row["artist"])
    if notes:
        b["owner_annotations"] = notes
    evs = conn.execute("""SELECT * FROM events WHERE pid=? ORDER BY
        started_at DESC LIMIT 8""", (row["pid"],)).fetchall()
    b["recent_events"] = [{"at": djlib._day(e["started_at"]),
                           "initiation": e["initiation"],
                           "verdict": e["verdict"], "source": e["source"]}
                          for e in evs] or None
    b["in_library"] = True
    tensions = []
    if c["flagged"]:
        tensions.append("play count is contamination-flagged — raw plays "
                        "overstate the welcome")
    if row["favorited"]:
        tensions.append("favorited")
    if notes:
        tensions.append(f"user said: \"{notes[-1]['verbatim'][:80]}\"")
    # Replay outranks local counters: streaming happens via generated playlists, so a
    # loved track can show few local plays and many skips (In My Life is #33).
    rank = djlib.replay_rank(conn, row["name"], row["artist"])
    b["replay"] = {
        "rank": rank,
        "note": (f"#{rank} of the user's true top 100 (Apple Replay, streaming "
                 "included) — outranks play_count and skip ratio"
                 if rank else "not in the Replay top 100; absence is weak "
                 "evidence, the list is only 100 long")}
    if rank:
        tensions.append(f"Replay #{rank}")
    dd = b["dormant_days"]
    b["summary"] = (
        f"'{row['name']}' — {row['artist']}: {row['play_count']} raw plays"
        f"{' (fav)' if row['favorited'] else ''}, last played "
        f"{djlib._day(row['last_played'])}"
        + (f" ({dd}d ago)" if dd is not None else "")
        + (f", {b['stratum']} stratum" if b["stratum"] else "")
        + (f", REPLAY #{rank}" if rank else "")
        + ". " + ("Tensions: " + "; ".join(tensions) + "."
                  if tensions else "No tensions on record."))
    return b


def _out_of_library_anchor(conn, player) -> dict:
    """The current track exists but is not the user's: say exactly that, and
    surface what the daemon HAS seen of it (fraction, recurrence)."""
    evs = conn.execute("""SELECT * FROM events WHERE pid=? OR
        (name=? COLLATE NOCASE AND artist=? COLLATE NOCASE)
        ORDER BY started_at DESC LIMIT 10""",
        (player.get("pid", ""), player["name"],
         player.get("artist", ""))).fetchall()
    obs = None
    if evs:
        fr = [e["fraction"] or 0 for e in evs]
        obs = {"observed_plays": len(evs),
               "mean_fraction": round(sum(fr) / len(fr), 2),
               "last_seen": djlib._day(evs[0]["started_at"]),
               "initiations": sorted({e["initiation"] for e in evs}),
               "provenance": "accumulated"}
    return {"summary": (
        f"Anchor is the current track ({player['state']}): "
        f"'{player['name']}' — {player.get('artist', '?')}, which is NOT in "
        "the user's library. "
        + (f"The daemon has observed it {obs['observed_plays']} time(s), "
           f"mean fraction heard {obs['mean_fraction']:.0%}, last "
           f"{obs['last_seen']}. " if obs else
           "The daemon has never observed it. ")
        + "No library metadata exists to anchor era/genre adjacency on; "
          "judge_track has the full fall-through picture."),
        "known": False, "reason": "current track not in library",
        "player_state": player["state"], "anchor_observed": obs}


def adjacent_to(args, conn) -> dict:
    th = djlib.thresholds(conn)
    anchor = None
    if args.get("anchor") == "session" or not (
            args.get("pid") or args.get("name")):
        player = live_player()
        if not player.get("name"):
            return {"summary": f"No anchor: the player is {player['state']} "
                               "and no track was given.",
                    "known": False,
                    "reason": f"player {player['state']}, no explicit anchor"}
        anchor = djlib.find_track(conn, pid=player.get("pid"),
                                  name=player.get("name"))
        if not anchor:
            return _out_of_library_anchor(conn, player)
    else:
        anchor = djlib.find_track(conn, pid=args.get("pid"),
                                  name=args.get("name"),
                                  artist=args.get("artist"))
    if not anchor:
        return {"summary": f"Anchor not in library.", "known": False,
                "reason": "anchor unresolved"}
    # co-play (tier 1): tracks sharing daemon sessions with the anchor
    co = conn.execute("""SELECT e2.name, e2.artist, COUNT(*) n,
            AVG(e2.fraction) af
        FROM events e1 JOIN events e2 ON e1.session_id = e2.session_id
        WHERE e1.pid = ? AND e2.pid != ? AND e1.session_id IS NOT NULL
          AND e2.initiation NOT LIKE 'autoplay%'
        GROUP BY e2.pid ORDER BY n DESC LIMIT 12""",
        (anchor["pid"], anchor["pid"])).fetchall()
    n_sessions = conn.execute(
        "SELECT COUNT(*) n FROM sessions").fetchone()["n"]
    neighbours = []
    if len(co) >= 3:
        basis = f"co_play — {n_sessions} daemon sessions observed"
        for r in co:
            neighbours.append({
                "name": r["name"], "artist": r["artist"],
                "why": f"shared {r['n']} session(s), mean completion "
                       f"{r['af']:.0%}",
                "provenance": "accumulated"})
    else:
        basis = (f"tag_and_era — co-play data insufficient "
                 f"({n_sessions} daemon sessions observed)")
        stratum = conn.execute("SELECT stratum_month FROM strata WHERE pid=?",
                               (anchor["pid"],)).fetchone()
        rows = conn.execute("""SELECT t.*, s.stratum_month FROM tracks t
            LEFT JOIN strata s ON s.pid = t.pid
            WHERE t.pid != ? AND t.is_nonmusic = 0 AND t.play_count > 3
            AND (ABS(COALESCE(t.year,0) - ?) <= 6 OR t.genre = ?
                 OR s.stratum_month = ?)
            ORDER BY t.play_count DESC LIMIT 12""",
            (anchor["pid"], anchor["year"] or 0, anchor["genre"],
             stratum["stratum_month"] if stratum else "")).fetchall()
        for r in rows:
            why = []
            if stratum and r["stratum_month"] == stratum["stratum_month"]:
                why.append(f"shared {r['stratum_month']} stratum")
            if r["genre"] == anchor["genre"]:
                why.append(f"genre '{r['genre']}'")
            if anchor["year"] and r["year"] and abs(r["year"] - anchor["year"]) <= 6:
                why.append(f"era ({r['year']})")
            notes = djlib.annotations_for(conn, r["name"], r["artist"])
            nb = {"name": r["name"], "artist": r["artist"],
                  "why": "; ".join(why), "plays_raw": r["play_count"],
                  "provenance": "reconstructed"}
            if notes:
                nb["owner_verbatim"] = notes[-1]["verbatim"]
                nb["provenance"] += "+owner_verbatim"
            neighbours.append(nb)
    anchor_notes = djlib.annotations_for(conn, anchor["name"], anchor["artist"])
    return {
        "summary": f"Adjacency for '{anchor['name']}' — {anchor['artist']}: "
                   f"{len(neighbours)} neighbours by {basis.split(' —')[0]}. "
                   + ("Tag/era adjacency is a GUESS here — see warning."
                      if basis.startswith("tag") else
                      "Grounded in actual co-play behaviour."),
        "basis": basis, "neighbours": neighbours,
        "anchor_annotations": anchor_notes or None,
        "warning": "tag adjacency failed before: shoegaze picks tagged "
                   "'alternative' were rejected ('too weird hippy'). Weigh "
                   "groove/energy yourself — the store has no energy axis "
                   "and will not fake one."}


def untouched_but_promising(args, conn) -> dict:
    th = djlib.thresholds(conn)
    limit = int(args.get("limit", 5))
    # honest sources only: added-but-unworn library tracks + un-skipped
    # autoplay fall-throughs. Empty is a normal answer.
    rows = conn.execute("""SELECT * FROM tracks WHERE is_nonmusic=0
        AND play_count <= 1 ORDER BY date_added DESC""").fetchall()
    hist = djlib.latest_history(conn)
    recent_artists = set()
    for h in hist[-30:]:
        t = djlib.find_track(conn, pid=h["pid"], name=h["name"])
        if t:
            recent_artists.add(t["artist"])
    recent_eras = {t["year"] // 10 * 10 for t in
                   (djlib.find_track(conn, pid=h["pid"], name=h["name"])
                    for h in hist[-30:]) if t and t["year"]}
    picks = []
    for r in rows:
        if len(picks) >= limit:
            break
        reasons = []
        if r["artist"] in recent_artists:
            reasons.append(f"artist '{r['artist']}' is in the user's recent play order")
        if r["year"] and (r["year"] // 10 * 10) in recent_eras:
            reasons.append(f"era-adjacent to recent plays ({r['year']})")
        if not reasons:
            continue
        picks.append({
            "name": r["name"], "artist": r["artist"],
            "plays_raw": r["play_count"],
            "added": djlib._day(r["date_added"]),
            "reason": "the user added it (dateAdded is intent) but never wore it "
                      "in; " + "; ".join(reasons),
            "provenance": "reconstructed"})
    falls = conn.execute("""SELECT name, artist, COUNT(*) n, AVG(fraction) af
        FROM events WHERE in_library=0 AND initiation='autoplay_falloff'
        GROUP BY name, artist HAVING af >= ?""",
        (th["complete_at"],)).fetchall()
    for r in falls[: max(0, limit - len(picks))]:
        picks.append({"name": r["name"], "artist": r["artist"],
                      "reason": f"autoplay fall-through allowed to play to "
                                f"{r['af']:.0%} across {r['n']} play(s) — a "
                                "structural positive, not in the user's library",
                      "provenance": "accumulated"})
    zero = conn.execute("""SELECT COUNT(*) n FROM tracks WHERE is_nonmusic=0
        AND play_count = 0""").fetchone()["n"]
    return {"summary": (
        f"{len(picks)} candidate(s) from the {zero}-track unheard tail "
        "plus un-skipped fall-throughs."
        + (" Empty list: nothing currently clears the bar — that is a "
           "normal answer, not a failure; "
           "do not pad." if not picks else
           " Each has its reason; a pick "
           "still needs your judgment on fit.")),
        "candidates": picks,
        "recent_play_order_age": djlib.history_age(conn),
        "note": "an empty list is a first-class outcome"}


def system_health(args, conn) -> dict:
    th = djlib.thresholds(conn)
    d = daemon_state(conn)
    kc = djlib.knowledgec_status()
    plist = Path.home() / "Library" / "LaunchAgents" / "com.djclaude.daemon.plist"
    gaps = [dict(g) for g in conn.execute(
        "SELECT * FROM coverage_gaps ORDER BY started_at DESC LIMIT 5")]
    backfill_at = djlib.meta_get(conn, "backfill_at")
    last_ingest = djlib.meta_get(conn, "last_ingest")
    history_refresh_error = djlib.meta_get(conn, "history_refresh_error")
    counter_refresh_error = djlib.meta_get(conn, "counter_refresh_error")
    counts = {t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
              for t in ("tracks", "events", "sessions", "snapshots",
                        "history_snapshots", "annotations")}
    queue_shortcut_ready = _shortcut_ready(CATALOG_QUEUE_SHORTCUT)
    play_shortcut_ready = _shortcut_ready(CATALOG_PLAY_SHORTCUT)
    tier = 0
    if d["alive"]:
        tier = 1
    if kc["readable"]:
        tier = 2
    next_grant = None
    if not d["process_running"]:
        next_grant = ("Daemon not running. Install: python3 "
                      "~/.claude/dj/daemon.py install")
    elif d["probes_blocked"]:
        next_grant = ("Daemon process is running but cannot reach Music: "
                      "System Settings > Privacy & Security > Automation > "
                      "python3 (or Python/osascript) > enable Music. Unlocks "
                      "tier 1: per-play events, manual-pick detection, real "
                      "skip semantics. No restart needed — it retries.")
    elif d["state_unknown"]:
        next_grant = (f"Daemon appears running but its state is unknown "
                      f"({d['heartbeat_problem'] or 'heartbeat lagging'}). "
                      "This is NOT evidence of an Automation consent problem "
                      "— do not chase grants; check ~/.claude/dj/"
                      "daemon.err.log first.")
    elif not kc["readable"]:
        next_grant = ("System Settings > Privacy & Security > Full Disk "
                      "Access > enable for Terminal (or the daemon's python3)"
                      ". Unlocks tier 2: ~4 weeks of true per-play backfill "
                      "from knowledgeC, foreground corroboration.")
    return {
        "summary": (
            f"Tier {tier}. Daemon "
            + ("LIVE" if d["alive"] else
               "RUNNING but Music-blocked (probe errors in its heartbeat "
               "point at Automation consent)" if d["probes_blocked"] else
               "RUNNING but state unknown "
               f"({d['heartbeat_problem'] or 'heartbeat lagging'} — not a "
               "consent diagnosis)" if d["state_unknown"] else "NOT RUNNING")
            + f"; {counts['events']} events, {counts['sessions']} sessions, "
            f"{counts['tracks']} tracks, {counts['annotations']} user "
            f"annotations. Backfill {djlib._day(float(backfill_at)) if backfill_at else 'NEVER RUN'}. "
            + ("History order refresh is degraded; the last good snapshot "
               "remains available. " if history_refresh_error else "")
            + ("Counter refresh is degraded; the last good counters remain "
               "available. " if counter_refresh_error else "")
            + (f"Next grant: {next_grant}" if next_grant
               else "All tiers unlocked except the optional swift helper.")),
        "tier": tier, "daemon": d,
        "launchd_installed": plist.exists(),
        "knowledgec": kc if kc["readable"] else {
            "readable": False,
            "grant": "Full Disk Access for the daemon host",
            "unlocks": "~4 weeks of true per-play backfill"},
        "coverage": {
            "events_window": ("since %s" % d["accumulating_since"])
            if d["accumulating_since"] else "no daemon events yet",
            "before_that": "strata + lifetime counters only (reconstructed)",
            "gaps": gaps},
        "store_counts": counts,
        "backfill_at": djlib._day(float(backfill_at)) if backfill_at else None,
        "last_ingest_age_s": round(time.time() - float(last_ingest))
        if last_ingest else None,
        "history_order": {**djlib.history_age(conn),
                          "refresh_error": history_refresh_error or None,
                          "refresh_owner": "foreground MCP (not launchd)"},
        "counter_refresh": {
            "error": counter_refresh_error or None,
            "refresh_owner": "foreground MCP (not launchd)",
            "failure_retry_hours": 6},
        "catalog": {
            "search": "ready (Apple iTunes Search API, 10-minute cache)",
            "direct_play": ("ready" if play_shortcut_ready else "unavailable"),
            "native_queue": ("ready" if queue_shortcut_ready else "unavailable"),
            "play_shortcut": CATALOG_PLAY_SHORTCUT,
            "queue_shortcut": CATALOG_QUEUE_SHORTCUT,
            "queue_proof_boundary": "identity preflight + dispatch now; "
                                    "daemon confirmation when item starts"},
        "thresholds_in_force": th,
        "uninstall": "python3 ~/.claude/dj/daemon.py uninstall"}


# ------------------------------------------------------------ write tool

def remember_reaction(args, conn) -> dict:
    scope = args.get("scope", "track")
    if scope not in ("track", "artist", "pick_list", "session"):
        return {"error": f"scope must be track|artist|pick_list|session"}
    ref, warning = args.get("ref", ""), None
    if scope == "track":
        # track notes bind by exact 'Name — Artist' ref; canonicalise from
        # the library so lookups on short names never misattribute
        for sep in (" — ", " -- ", " - "):
            if sep in ref:
                nm, ar = ref.split(sep, 1)
                break
        else:
            nm, ar = ref, None
        row = djlib.find_track(conn, name=nm.strip(),
                               artist=ar.strip() if ar else None)
        if row:
            ref = f"{row['name']} — {row['artist']}"
        else:
            warning = ("ref does not resolve to a library track; stored "
                       "as-is and will only surface on an exact "
                       "'Name — Artist' match")
    conn.execute("INSERT INTO annotations VALUES(NULL,?,?,?,?,?,?)",
                 (time.time(), scope, ref,
                  args.get("verbatim", ""), args.get("valence", "unknown"),
                  "owner_verbatim"))
    conn.commit()
    out = {"summary": "Recorded verbatim. It will surface as evidence in "
                      "judge_track/adjacent_to/dormant_loves — never as an "
                      "automatic filter.",
           "stored": {"scope": scope, "ref": ref,
                      "verbatim": args.get("verbatim")}}
    if warning:
        out["warning"] = warning
    return out


# ---------------------------------------------------------- control tools

def _player_state():
    """None when the player cannot be reached — callers must fail CLOSED:
    an unknown state is never a licence to act."""
    try:
        return osa('tell application "Music" to player state as text',
                   timeout=10)
    except (RuntimeError, subprocess.TimeoutExpired):
        return None


def queue_set(args, conn) -> dict:
    state = _player_state()
    if state is None:
        return {"refused": True,
                "reason": "player state unreachable; acting blind could "
                          "steal a paused player. Refusing (fail closed)."}
    paused_note = None
    if state == "paused":
        # Pause is mostly dead air, not a wall (user directive 2026-08-04:
        # a stale pause is the DJ's cue to play). The one judgment call —
        # a fresh mid-track pause — belongs to the caller, so disclose
        # instead of refusing. Starting a pick never resumes their spot.
        paused_note = ("started over a paused player: pause is treated as "
                       "dead air. If the user had paused mid-track to come "
                       "back, their spot in that song is NOT preserved by "
                       "this playlist start.")
    live = live_player()
    if live.get("context_playlist") == djlib.DJ_PLAYLIST and \
            live.get("state") == "playing":
        return {"refused": True,
                "reason": "frozen-queue semantics: the DJ queue is playing "
                          "mid-flight; no mid-flight rebuilds. Wait for a "
                          "track boundary (see whats_happening_now position) "
                          "or let the queue finish."}
    picks = args.get("picks", [])
    if not picks:
        return {"error": "picks required: [{pid} or {name, artist}]"}
    pids = []
    for p in picks:
        row = djlib.find_track(conn, pid=p.get("pid"), name=p.get("name"),
                               artist=p.get("artist"))
        if row:
            pids.append((row["pid"], row["name"], row["artist"]))
    if not pids:
        return {"error": "no picks resolved to library tracks"}
    esc = lambda s: s.replace('\\', '\\\\').replace('"', '\\"')
    # delete + recreate the playlist object: 'delete every track' silently
    # no-ops on some Music builds, which would leave stale picks in the queue
    build = ['tell application "Music"',
             f'if (exists playlist "{djlib.DJ_PLAYLIST}") then delete '
             f'playlist "{djlib.DJ_PLAYLIST}"',
             f'make new playlist with properties '
             f'{{name:"{djlib.DJ_PLAYLIST}"}}']
    for pid, _, _ in pids:
        build.append(f'duplicate (some track of library playlist 1 whose '
                     f'persistent ID is "{esc(pid)}") to playlist '
                     f'"{djlib.DJ_PLAYLIST}"')
    build += ['set shuffle enabled to false',
              f'set o to ""',
              f'repeat with t in (every track of playlist "{djlib.DJ_PLAYLIST}")',
              'set o to o & (persistent ID of t) & ","',
              'end repeat', 'return o', 'end tell']
    journal("queue_set", {"pids": [p[0] for p in pids]})
    try:
        readback = [p for p in osa("\n".join(build), timeout=60).split(",") if p]
    except RuntimeError as exc:
        return {"error": f"playlist build failed: {exc}"}
    if readback != [p[0] for p in pids]:
        return {"error": "rebuild verification failed: playlist now holds "
                         f"{readback}, expected {[p[0] for p in pids]}"}
    started = False
    if args.get("start", True) and state in ("stopped", "paused"):
        time.sleep(1)  # settle after shuffle-off before playing the object
        journal("play", {"playlist": djlib.DJ_PLAYLIST})
        try:
            osa(f'tell application "Music" to play playlist '
                f'"{djlib.DJ_PLAYLIST}"', timeout=15)
            started = True
        except RuntimeError as exc:
            return {"error": f"queue built but play failed: {exc}"}
    out = {"summary": f"DJ queue rebuilt with {len(pids)} tracks (verified "
                      f"by readback), shuffle off"
                      f"{', playing' if started else ''}."
                      + ("" if started else
                         " Not started: a PLAYING session belongs to the "
                         "user; dead air and stale pause are fair game."),
           "queued": [{"name": n, "artist": a} for _, n, a in pids],
           "journaled": True}
    if started and paused_note:
        out["paused_player_note"] = paused_note
    return out


def catalog_play(args, conn) -> dict:
    """Immediate catalog play, only for an explicit user request.

    Normal DJ behavior extends the native queue instead. This tool exists so
    `play X` is not forced through the rare celebration/mood-veto budget.
    """
    try:
        ref = _catalog_ref(args)
    except (TypeError, ValueError) as exc:
        return {"error": str(exc)}
    state = _player_state()
    if state is None:
        return {"refused": True, "reason": "player unreachable; refusing to "
                                             "act blind (fail closed)."}
    if not (args.get("user_requested") or args.get("owner_requested")):
        return {"refused": True,
                "reason": "catalog_play interrupts immediately and is only "
                          "for an explicit user request. Pass "
                          "owner_requested=true only when the user actually asked; "
                          "otherwise use catalog_queue or play_moment."}
    reason = str(args.get("reason", "")).strip()
    if not reason:
        return {"error": "reason is required; quote or summarize the user's "
                         "immediate-play request"}
    payload = {"reason": reason, "owner_requested": True,
               "source": "catalog", "catalog_ref": ref}
    journal("catalog_play", payload)
    try:
        live = _play_catalog_ref(ref)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"error": f"catalog play was journaled but failed: {exc}",
                "journaled": True}
    return {
        "summary": f"User-requested catalog play landed: '{ref['name']}' — "
                   f"{ref['artist']} is playing, verified by live readback.",
        "playing": {"name": live["name"], "artist": live["artist"],
                    "track_id": ref["track_id"],
                    "collection_id": ref["collection_id"]},
        "reason": reason, "journaled": True,
        "verification": "catalog ID was preflighted; Music's native Play "
                        "Music action landed; live readback matched exact "
                        "name + artist at the beginning of the track",
    }


def catalog_queue(args, conn) -> dict:
    """Append exact Apple-catalog songs to Music's native Playing Next."""
    state = _player_state()
    if state is None:
        return {"refused": True,
                "reason": "player state unreachable; refusing to mutate the "
                          "native queue while blind."}
    # Appending to Playing Next makes no sound; a paused player is never a
    # reason to arrive with an empty clip (user directive 2026-08-04).
    if not _shortcut_ready():
        return {"known": False,
                "reason": f"the built-in Shortcut '{CATALOG_QUEUE_SHORTCUT}' "
                          "is missing; catalog identity works, but native "
                          "queue insertion is unavailable"}
    raw_picks = args.get("picks") or []
    if not raw_picks:
        return {"error": "picks required: [{catalog_ref: {...}}]"}
    if len(raw_picks) > 5:
        return {"error": "at most 5 catalog picks per call; keep the DJ queue "
                         "deliberate and bounded"}
    refs = []
    try:
        refs = [_catalog_ref(p) for p in raw_picks]
    except (TypeError, ValueError) as exc:
        return {"error": str(exc)}
    dispatched = []
    for index, ref in enumerate(refs):
        try:
            _dispatch_queue_ref(ref)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            # The picks already sent WILL play; failing to journal them makes
            # the daemon credit them to autoplay (Come Sail Away, 2026-07-29).
            if dispatched:
                journal("catalog_queue", {
                    "picks": dispatched, "native": True, "partial": True,
                    "user_requested": bool((args.get("user_requested") or args.get("owner_requested")))})
            return {"error": f"catalog queue stopped at pick {index + 1}: "
                             f"{exc}",
                    "dispatched_before_failure": dispatched,
                    "journaled": bool(dispatched)}
        dispatched.append({"name": ref["name"], "artist": ref["artist"],
                           "album": ref.get("album"),
                           "track_id": ref["track_id"]})
        # Shortcut URL execution is asynchronous. Serialize launches so Music
        # preserves the requested order instead of racing two searches.
        if index + 1 < len(refs):
            time.sleep(8)
    time.sleep(4)
    journal("catalog_queue", {
        "picks": [{"name": r["name"], "artist": r["artist"],
                   "album": r.get("album"), "track_id": r["track_id"],
                   "collection_id": r["collection_id"]} for r in refs],
        "native": True, "user_requested": bool((args.get("user_requested") or args.get("owner_requested")))})
    return {
        "summary": f"Dispatched {len(refs)} exact catalog song(s) to Music's "
                   "native Playing Next queue in the requested order.",
        "queued": dispatched, "journaled": True,
        "verification": {
            "catalog_identity": "each metadata query was preflighted against "
                                "Apple's first song result and exact track_id",
            "dispatch": "macOS accepted each built-in Shortcut URL",
            "live_queue_readback": {"known": False,
                "reason": "Music does not expose its native Playing Next "
                          "queue to AppleScript; the daemon verifies each "
                          "journaled item when playback actually reaches it"}},
    }


def play_moment(args, conn) -> dict:
    """Spend the rare musical-moment budget on one deliberate interruption.

    Normal DJ work respects track boundaries. A completion celebration or
    mood veto is the explicit, journaled exception. The caller judges the
    moment and owns the once-per-agent-session budget because MCP has no
    trustworthy agent-session identifier.
    """
    state = _player_state()
    if state is None:
        return {"refused": True, "reason": "player unreachable; refusing to "
                                             "act blind (fail closed)."}
    # A paused player does not block a moment (user directive 2026-08-04:
    # pause is mostly dead air, and a drop that lands is worth the interrupt).
    # The caller still owns the judgment for a fresh mid-track pause.
    kind = args.get("kind")
    if kind not in ("completion_celebration", "mood_veto"):
        return {"error": "kind must be completion_celebration|mood_veto"}
    reason = str(args.get("reason", "")).strip()
    if not reason:
        return {"error": "reason is required; name the verified moment that "
                         "justifies spending the rare override"}
    catalog = args.get("catalog_ref")
    if catalog:
        try:
            ref = _catalog_ref({"catalog_ref": catalog})
        except (TypeError, ValueError) as exc:
            return {"error": str(exc)}
        payload = {"kind": kind, "reason": reason, "source": "catalog",
                   "user_requested": bool((args.get("user_requested") or args.get("owner_requested"))),
                   "catalog_ref": ref}
        journal("play_moment", payload)
        try:
            live = _play_catalog_ref(ref)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return {"error": f"moment was journaled but catalog playback "
                             f"failed: {exc}", "journaled": True}
        return {
            "summary": f"Moment landed: '{ref['name']}' — {ref['artist']} "
                       f"is playing for {kind.replace('_', ' ')}. The catalog "
                       "ID and live title/artist were verified.",
            "playing": {"name": live["name"], "artist": live["artist"],
                        "track_id": ref["track_id"], "source": "catalog"},
            "reason": reason, "journaled": True,
            "budget_note": "caller spent the one musical-moment budget for "
                           "this agent session"}
    row = djlib.find_track(conn, pid=args.get("pid"), name=args.get("name"),
                           artist=args.get("artist"))
    if not row:
        return {"error": "pick resolves to neither a library track nor a "
                         "validated catalog_ref; call catalog_search first"}
    payload = {"kind": kind, "reason": reason, "pid": row["pid"],
               "name": row["name"], "artist": row["artist"]}
    journal("play_moment", payload)
    esc_pid = row["pid"].replace('"', '\\"')
    # scope the play inside the DJ queue when the pick lives there: playing it
    # out of the library instead leaves autoplay in catalog context, which
    # orphans the rest of the queue the moment the override ends.
    script = f'''tell application "Music"
        if (exists playlist "{djlib.DJ_PLAYLIST}") then
            set m to (every track of playlist "{djlib.DJ_PLAYLIST}" ¬
                whose persistent ID is "{esc_pid}")
            if m is not {{}} then
                play (item 1 of m)
                return "{djlib.DJ_PLAYLIST}"
            end if
        end if
        play (some track of library playlist 1 whose persistent ID is "{esc_pid}")
        return "library"
    end tell'''
    try:
        context = osa(script, timeout=15).strip()
        time.sleep(1)
        live = live_player()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"error": f"moment was journaled but playback failed: {exc}",
                "journaled": True}
    landed = live.get("pid") == row["pid"] or (
        live.get("name", "").casefold() == row["name"].casefold() and
        live.get("artist", "").casefold() == row["artist"].casefold())
    if not landed:
        return {"error": "Music accepted the play command but readback did "
                         "not match the requested track",
                "requested": payload, "readback": live, "journaled": True}
    on_queue = context == djlib.DJ_PLAYLIST
    return {"summary": f"Moment landed: '{row['name']}' — {row['artist']} "
                       f"is playing for {kind.replace('_', ' ')}. The command "
                       "is journaled so it will not be credited as a user pick. "
                       + ("Playing inside the DJ queue, so the picks behind it "
                          "still follow." if on_queue else
                          "Played out of the library — the pick was not in the "
                          "queue, so autoplay resumes in catalog context when "
                          "it ends; queue_set first if you want continuity."),
            "playing": {"pid": row["pid"], "name": row["name"],
                        "artist": row["artist"],
                        "context_playlist": context},
            "player_on_queue": on_queue,
            "reason": reason, "journaled": True,
            "budget_note": "caller spent the one musical-moment budget for "
                           "this agent session"}


def queue_status(args, conn) -> dict:
    live = live_player()
    try:
        raw = osa(f'''tell application "Music"
            set o to ""
            repeat with t in (every track of playlist "{djlib.DJ_PLAYLIST}")
              set o to o & (persistent ID of t) & (ASCII character 31)
            end repeat
            return o
        end tell''', timeout=20)
        actual = [p for p in raw.split("\x1f") if p]
    except RuntimeError as exc:
        return {"error": f"cannot read playlist: {exc}"}
    last = conn.execute("""SELECT arg FROM commands WHERE cmd='queue_set'
        ORDER BY at DESC LIMIT 1""").fetchone()
    expected = (json.loads(last["arg"]).get("pids", []) if last else [])
    drift = actual != expected
    on_queue = live.get("context_playlist") == djlib.DJ_PLAYLIST
    fallthrough = (live.get("state") == "playing" and not on_queue
                   and expected and live.get("pid") not in expected)
    last_catalog = conn.execute("""SELECT at, arg FROM commands
        WHERE cmd='catalog_queue' ORDER BY at DESC LIMIT 1""").fetchone()
    catalog_plan = None
    if last_catalog:
        parsed = json.loads(last_catalog["arg"] or "{}")
        catalog_plan = {
            "queued_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(last_catalog["at"])),
            "age_s": round(time.time() - last_catalog["at"]),
            "picks": parsed.get("picks", []),
            "native_queue_live_readback": {"known": False,
                "reason": "Music does not expose Playing Next to AppleScript; "
                          "this is the last journaled verified dispatch, and "
                          "the daemon confirms items as they start"}}
    return {"summary": (
        f"Queue has {len(actual)} tracks; "
        + ("matches the last journaled queue_set. " if not drift else
           "DIFFERS from the last journaled queue_set — someone else "
           "touched it. ")
        + ("Player is on the DJ queue. " if on_queue else
           "Player is NOT on the DJ queue. ")
        + ("Fall-through suspected: playing an unqueued track with no DJ "
           "context — the queue may have run dry." if fallthrough else "")),
        "queue_pids": actual, "expected_pids": expected,
        "drift": drift, "player_on_queue": on_queue,
        "catalog_native": catalog_plan}


def stop_gracefully(args, conn) -> dict:
    state = _player_state()
    if state is None:
        return {"refused": True, "reason": "player unreachable; refusing to "
                                           "act blind (fail closed)."}
    if state == "paused":
        return {"refused": True, "reason": "already paused by the user; nothing to stop."}
    journal("stop", None)
    try:
        osa('tell application "Music" to pause', timeout=10)
    except RuntimeError as exc:
        return {"error": str(exc)}
    return {"summary": "Paused (journaled as a DJ command, so the daemon "
                       "will not read it as the user's hand)."}


CATALOG_REF_SCHEMA = {
    "type": "object",
    "description": "Pass unchanged from catalog_search; never invent IDs",
    "properties": {
        "track_id": {"type": "integer"},
        "collection_id": {"type": "integer"},
        "name": {"type": "string"}, "artist": {"type": "string"},
        "album": {"type": "string"},
        "country": {"type": "string"},
        "track_view_url": {"type": "string"},
    },
}


TOOLS = {
    "whats_happening_now": (whats_happening_now, {},
        "The moment: any wake — track about to end, session start, or you "
        "need situational awareness. Returns the live player, how the "
        "current track started (manual vs autoplay vs DJ queue), the "
        "session trace, and urgent flags (queue ran dry). Call this first."),
    "current_cycle": (current_cycle,
        {"window_days": {"type": "integer",
                         "description": "trailing window, default 42"}},
        "The moment: choosing a direction or starting a DJ shift. What is "
        "the user in the middle of? Returns cycle state (active_cycle | "
        "forming_or_ambiguous | between_obsessions_or_shuffle | unknown) "
        "with the evidence: strata (gap months explicit), recent play order "
        "with its age, manual picks, era spread. Never a single-genre mood."),
    "dormant_loves": (dormant_loves,
        {"min_days": {"type": "integer",
                      "description": "dormant at least this long (default 45)"},
         "max_days": {"type": "integer",
                      "description": "dormant at MOST this long — with "
                                     "min_days this bands a specific cycle: "
                                     "'loved ~2 months ago' is 45-120"},
         "min_plays": {"type": "integer"}, "limit": {"type": "integer"}},
        "The moment: hunting a re-pick. 'The user played this months ago — "
        "would they want it back?' Dormant high-play tracks with stratum "
        "dates and contamination checks; discounted tracks are shown with "
        "their evidence, never silently dropped. Band with min_days+max_days "
        "to target a cycle; unbanded ordering favors the longest-dormant. "
        "A pool, not a verdict."),
    "judge_track": (judge_track,
        {"pid": {"type": "string"}, "name": {"type": "string"},
         "artist": {"type": "string"}},
        "The moment: you are considering ONE candidate. The user's full "
        "relationship with it: raw + intent-weighted plays, skip caveat, "
        "stratum, contamination, user verbatims, recent events. The "
        "summary states tensions; the verdict is yours."),
    "adjacent_to": (adjacent_to,
        {"pid": {"type": "string"}, "name": {"type": "string"},
         "artist": {"type": "string"},
         "anchor": {"type": "string",
                    "description": "'session' anchors on what is playing"}},
        "The moment: extending a vibe. Neighbours by real co-play when "
        "daemon data suffices, else era/genre/stratum fallback explicitly "
        "labeled as guessing. Includes the shoegaze warning: tags failed "
        "before."),
    "untouched_but_promising": (untouched_but_promising,
        {"limit": {"type": "integer"}},
        "The moment: the user might tolerate ONE new thing. Honestly-sourced "
        "candidates only: tracks the user added but never wore in, and autoplay "
        "fall-throughs conspicuously not skipped. Returns few or NONE "
        "— an empty list is a normal answer; do not pad from it."),
    "system_health": (system_health, {},
        "The moment: start of shift, or any tool answer smells stale. "
        "Daemon aliveness, event counts, coverage window and gaps, what TCC "
        "grant unlocks the next tier (verbatim instruction), thresholds in "
        "force, uninstall command."),
    "catalog_search": (catalog_search,
        {"query": {"type": "string",
                   "description": "song title or compact catalog query"},
         "artist": {"type": "string",
                    "description": "optional artist disambiguation"},
         "country": {"type": "string",
                     "description": "two-letter storefront, default US"},
         "limit": {"type": "integer",
                   "description": "1-10, default 5"}},
        "Resolve exact Apple Music catalog identity before any non-library "
        "play or queue action. Returns stable track/collection IDs, exact "
        "name/artist/album, and a catalog_ref to pass unchanged. This is "
        "identity lookup, not taste ranking; never silently choose a "
        "same-title recording."),
    "remember_reaction": (remember_reaction,
        {"scope": {"type": "string",
                   "description": "track | artist | pick_list | session"},
         "ref": {"type": "string",
                 "description": "what it refers to, e.g. 'Doin' Time — Sublime'"},
         "verbatim": {"type": "string",
                      "description": "the user's exact words, unedited"},
         "valence": {"type": "string",
                     "description": "positive | negative | mixed | unknown"}},
        "The moment: the user reacts in words the sensors cannot hear "
        "('too weird hippy'). The ONLY write into the intelligence store. "
        "Verbatims surface as evidence in other tools, never as filters."),
    "queue_set": (queue_set,
        {"picks": {"type": "array", "items": {"type": "object", "properties": {
            "pid": {"type": "string"}, "name": {"type": "string"},
            "artist": {"type": "string"}}}},
         "start": {"type": "boolean",
                   "description": "start playing if idle (default true); "
                                  "never steals from an active session"}},
        "Rebuild the DJ-owned queue playlist and play the PLAYLIST OBJECT "
        "(shuffle forced off, command journaled). Plays over stopped or "
        "(sacred) and refuses mid-flight rebuilds of a playing queue."),
    "queue_status": (queue_status, {},
        "Frozen queue vs reality: drift (someone edited it), whether the "
        "player is on the queue, and fall-through detection (queue ran "
        "dry into catalog autoplay)."),
    "catalog_play": (catalog_play,
        {"catalog_ref": CATALOG_REF_SCHEMA,
         "user_requested": {"type": "boolean",
             "description": "must be true only when the user explicitly "
                            "asked for this immediate play"},
         "reason": {"type": "string",
                    "description": "the user's immediate-play request"}},
        "Play one exact non-library catalog song immediately. Only for an "
        "explicit user request: requires owner_requested=true and a reason. "
        "Uses stable catalog IDs, journals the intervention, and verifies "
        "exact live name + artist. For ordinary DJ work, use catalog_queue."),
    "catalog_queue": (catalog_queue,
        {"picks": {"type": "array", "maxItems": 5,
                   "items": {"type": "object", "properties": {
                       "catalog_ref": CATALOG_REF_SCHEMA}}},
         "user_requested": {"type": "boolean",
             "description": "only needed to honor an explicit queue request "
                            "while the player is paused"}},
        "Append 1-5 exact catalog_search results to Music's native Playing "
        "Next queue. Each pick is preflighted so the built-in Shortcut's "
        "first song result has the requested track_id; ambiguous searches "
        "refuse instead of playing the wrong version. Journaled for daemon "
        "attribution. Native queue contents are not AppleScript-readable, so "
        "the response states the dispatch proof boundary honestly."),
    "play_moment": (play_moment,
        {"kind": {"type": "string",
                  "enum": ["completion_celebration", "mood_veto"],
                  "description": "why a boundary-breaking play is justified"},
         "reason": {"type": "string",
                    "description": "the verified session event that makes "
                                   "this rare interruption land"},
         "pid": {"type": "string"}, "name": {"type": "string"},
         "artist": {"type": "string"},
         "catalog_ref": CATALOG_REF_SCHEMA,
         "user_requested": {"type": "boolean",
             "description": "true only when the user themselves asked for this play"}},
        "The rare exception: spend the once-per-agent-session musical-moment "
        "budget. Use completion_celebration only when the real task is "
        "conclusively done and the user is likely delighted; use mood_veto "
        "only for an undeniable context-perfect fit. Never for routine "
        "greens, partial progress, unverified reports, or subagent wins. "
        "Fires over stopped or paused players — dead air is the enemy. "
        "Requires a reason, journals the action, and verifies the "
        "requested library or catalog track by live readback."),
    "stop_gracefully": (stop_gracefully, {},
        "Pause playback as the DJ, journaled so the daemon knows it was "
        "not the user's hand. Refuses if the user already paused."),
}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "music", "version": "2.2"},
                      "instructions": SERVER_INSTRUCTIONS}
        elif method == "tools/list":
            result = {"tools": [
                {"name": name, "description": desc,
                 "inputSchema": {"type": "object", "properties": props}}
                for name, (_fn, props, desc) in TOOLS.items()]}
        elif method == "tools/call":
            name = req["params"]["name"]
            args = req["params"].get("arguments") or {}
            try:
                conn = fresh_conn()
                payload = TOOLS[name][0](args, conn)
                conn.close()
                text = json.dumps(payload, indent=1, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001 - surface every failure
                text = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
            result = {"content": [{"type": "text", "text": text}]}
        elif method and method.startswith("notifications/"):
            continue
        else:
            result = {}
        if rid is not None:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
