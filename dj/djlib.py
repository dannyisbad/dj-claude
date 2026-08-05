#!/usr/bin/env python3
"""DJ Claude shared library: store schema, library scan, parsers, detectors.

Owned store: ~/.claude/dj/ (dj.sqlite3 + jsonl feeds). The user's library is
READ-ONLY here; the only thing the wider system ever writes in Music.app is
the "DJ Claude" playlist, and never from this module.

Two epochs (DESIGN.md 1.1): reconstructed past (snapshot counters, History.dat
order, RecentSearches) vs accumulated future (daemon events). Every fact a
tool emits carries provenance; this module supplies the primitives.
"""
import base64
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import time
from pathlib import Path

# Tests and recovery tooling can point at an isolated copy without lying to
# the code about the user's home directory. Production uses the original
# ~/.claude/dj location when DJ_CLAUDE_DIR is unset.
DJ_DIR = Path(os.environ.get(
    "DJ_CLAUDE_DIR", str(Path.home() / ".claude" / "dj"))).expanduser()
DB_PATH = DJ_DIR / "dj.sqlite3"
SCROBBLES = DJ_DIR / "scrobbles.jsonl"
COMMANDS = DJ_DIR / "commands.jsonl"
SNAPSHOTS_FEED = DJ_DIR / "snapshots.jsonl"
HISTORY_FEED = DJ_DIR / "history_order.jsonl"
HEARTBEAT = DJ_DIR / "daemon.heartbeat"
MUSIC_LIB = Path.home() / "Music" / "Music" / "Music Library.musiclibrary"
HISTORY_DAT = MUSIC_LIB / "Preferences" / "History.dat"
RECENT_SEARCHES = MUSIC_LIB / "com.apple.MusicKit" / "RecentSearches.json"
KNOWLEDGEC = "/private/var/db/CoreDuet/Knowledge/knowledgeC.db"
DJ_PLAYLIST = "DJ Claude"
APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds

# n=1 defaults (DESIGN.md open question 5); stored in meta, echoed in
# responses that use them, editable there without touching code.
THRESHOLDS = {
    "complete_at": 0.85,        # >= this fraction heard: done-with-it
    "reject_below": 0.25,       # < this fraction heard: rejection
    "cycle_window_days": 42,
    "strata_min_tracks": 3,     # artist tracks stranded in one month = cluster
    "contam_ratio_x": 2.0,      # skip/play ratio outlier = x * library median
    "artist_neighbor_plays": 2, # <= this many plays on artist's other tracks
                                # still counts as "artist-isolated"
    "session_gap_min": 30,
    "dormant_min_days": 45,
    "dormant_min_plays": 15,
    "poll_s": 5,
    "cycle_active_plays": 10,   # tail plays by one artist = active cycle
    "cycle_active_share": 0.5,  # ...and that artist's share of the tail
    "cycle_forming_plays": 5,   # tail plays by one artist = forming
    "cycle_forming_share": 0.3,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks(
  pid TEXT PRIMARY KEY, name TEXT, artist TEXT, album TEXT,
  album_artist TEXT, sort_artist TEXT, genre TEXT, year INT,
  duration_s REAL, date_added REAL, kind TEXT, is_nonmusic INT,
  favorited INT, play_count INT, skip_count INT,
  last_played REAL, last_skipped REAL);
CREATE TABLE IF NOT EXISTS snapshots(
  taken_at REAL, pid TEXT, play_count INT, skip_count INT,
  last_played REAL, last_skipped REAL,
  UNIQUE(taken_at, pid));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, pid TEXT, name TEXT, artist TEXT, album TEXT,
  started_at REAL, ended_at_s REAL, duration_s REAL, fraction REAL,
  verdict TEXT, initiation TEXT, initiation_evidence TEXT,
  session_id INT, source TEXT, in_library INT, context_playlist TEXT,
  UNIQUE(started_at, pid, name));
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, started_at REAL, ended_at REAL,
  gap_before_s REAL, first_initiation TEXT, device_hint TEXT);
CREATE TABLE IF NOT EXISTS strata(
  pid TEXT PRIMARY KEY, stratum_month TEXT, computed_at REAL);
CREATE TABLE IF NOT EXISTS history_snapshots(
  taken_at REAL, rank INT, name TEXT, pid TEXT, store_id TEXT, kind TEXT,
  UNIQUE(taken_at, rank));
CREATE TABLE IF NOT EXISTS searches(
  searched_at REAL, kind TEXT, raw_identifier TEXT, resolved_pid TEXT,
  UNIQUE(searched_at, kind));
CREATE TABLE IF NOT EXISTS annotations(
  id INTEGER PRIMARY KEY, at REAL, scope TEXT, ref TEXT,
  verbatim TEXT, valence TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS commands(
  at REAL, cmd TEXT, arg TEXT, UNIQUE(at, cmd));
CREATE TABLE IF NOT EXISTS coverage_gaps(
  started_at REAL, ended_at REAL, reason TEXT, UNIQUE(started_at));
CREATE TABLE IF NOT EXISTS generated_playlists(
  captured_at REAL, playlist TEXT, rank INT, name TEXT, artist TEXT,
  pid TEXT, in_library INT, UNIQUE(captured_at, playlist, rank));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ev_pid ON events(pid, started_at);
-- catalog plays carry pid=NULL, and NULLs are pairwise-distinct under the
-- table's UNIQUE(started_at,pid,name), so every re-ingest duplicated them
CREATE UNIQUE INDEX IF NOT EXISTS ev_dedup
  ON events(started_at, name, COALESCE(pid,''));
CREATE INDEX IF NOT EXISTS ev_time ON events(started_at);
CREATE INDEX IF NOT EXISTS snap_pid ON snapshots(pid, taken_at);
"""


def db() -> sqlite3.Connection:
    DJ_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for k, v in THRESHOLDS.items():
        conn.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (k, str(v)))
    conn.commit()
    return conn


def thresholds(conn) -> dict:
    got = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meta")}
    out = {}
    for k, dflt in THRESHOLDS.items():
        raw = got.get(k, dflt)
        out[k] = type(dflt)(float(raw)) if isinstance(dflt, (int, float)) else raw
    return out


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))
    conn.commit()


def osa(script: str, timeout: int = 30) -> str:
    out = subprocess.run(["osascript", "-e", script],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "osascript failed")
    return out.stdout.rstrip("\n")


# ---------------------------------------------------------------- library scan

# One Apple event per property list; dates come back as offsets from the
# script's own `current date` so no locale parsing is ever needed.
SCAN_SCRIPT = '''
set FS to ASCII character 31
set RS to ASCII character 30
tell application "Music"
  set L to library playlist 1
  set _pid to persistent ID of every track of L
  set _nm to name of every track of L
  set _ar to artist of every track of L
  set _al to album of every track of L
  set _aa to album artist of every track of L
  set _sa to sort artist of every track of L
  set _ge to genre of every track of L
  set _yr to year of every track of L
  set _du to duration of every track of L
  set _da to date added of every track of L
  set _pc to played count of every track of L
  set _sc to skipped count of every track of L
  set _pd to played date of every track of L
  set _sd to skipped date of every track of L
  set _fv to favorited of every track of L
  set _mk to media kind of every track of L
end tell
set nowd to current date
set out to {}
repeat with i from 1 to count of _pid
  set line_ to (item i of _pid) & FS & (item i of _nm) & FS & (item i of _ar) ¬
    & FS & (item i of _al) & FS & (item i of _aa) & FS & (item i of _sa) ¬
    & FS & (item i of _ge) & FS & (item i of _yr) & FS & (item i of _du) & FS
  set d to item i of _da
  if d is not missing value then set line_ to line_ & ((d - nowd) as text)
  set line_ to line_ & FS & (item i of _pc) & FS & (item i of _sc) & FS
  set d to item i of _pd
  if d is not missing value then set line_ to line_ & ((d - nowd) as text)
  set line_ to line_ & FS
  set d to item i of _sd
  if d is not missing value then set line_ to line_ & ((d - nowd) as text)
  set line_ to line_ & FS & (item i of _fv) & FS & ((item i of _mk) as text)
  copy line_ to end of out
end repeat
set AppleScript's text item delimiters to RS
return out as text
'''


def scan_library() -> list:
    """Full-library snapshot via AppleScript. Returns list of track dicts
    with unix-epoch dates. READ-ONLY against Music."""
    ref = time.time()
    raw = osa(SCAN_SCRIPT, timeout=120)
    rows = []
    for line in raw.split("\x1e"):
        parts = line.split("\x1f")
        if len(parts) != 16:
            continue
        (pid, name, artist, album, aartist, sartist, genre, year, dur,
         dadd, pc, sc, pdate, sdate, fav, mkind) = parts

        def ts(offset):
            return ref + float(offset) if offset.strip("-") else None
        rows.append({
            "pid": pid, "name": name, "artist": artist, "album": album,
            "album_artist": aartist, "sort_artist": sartist, "genre": genre,
            "year": int(year or 0), "duration_s": float(dur or 0),
            "date_added": ts(dadd), "play_count": int(pc or 0),
            "skip_count": int(sc or 0), "last_played": ts(pdate),
            "last_skipped": ts(sdate), "favorited": 1 if fav == "true" else 0,
            "kind": mkind,
            "is_nonmusic": 1 if (not artist and not album) else 0,
        })
    return rows


def store_scan(conn, rows, taken_at=None) -> None:
    taken_at = taken_at or time.time()
    for r in rows:
        conn.execute("""INSERT INTO tracks VALUES(
            :pid,:name,:artist,:album,:album_artist,:sort_artist,:genre,
            :year,:duration_s,:date_added,:kind,:is_nonmusic,:favorited,
            :play_count,:skip_count,:last_played,:last_skipped)
          ON CONFLICT(pid) DO UPDATE SET
            name=:name, artist=:artist, album=:album, genre=:genre,
            year=:year, duration_s=:duration_s, favorited=:favorited,
            play_count=:play_count, skip_count=:skip_count,
            last_played=:last_played, last_skipped=:last_skipped""", r)
        conn.execute("""INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?)""",
                     (taken_at, r["pid"], r["play_count"], r["skip_count"],
                      r["last_played"], r["last_skipped"]))
    conn.commit()


# ------------------------------------------------------------- History.dat

def parse_history_dat(path=HISTORY_DAT) -> list:
    """Stream the 230MB plist, skipping artwork. Returns ordered items
    (oldest first, verified: tail matches the most recent real plays):
    [{rank, name, pid, store_id, kind}]. pid is the AppleScript-style hex
    persistent ID when recoverable, else None."""
    items = []
    cur = {}
    last_key = None
    capture_modobj = False
    modobj_parts = []
    # plist skeleton is small; only <data> payloads are huge. Stream chunks,
    # route modobj data to a decoder and drop artwork data on the floor.
    tag_re = re.compile(
        rb"<key>([^<]*)</key>|<string>([^<]*)</string>|<data>|</data>|</dict>")
    in_data = False
    tail = b""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 22)
            if not chunk:
                break
            buf = tail + chunk
            pos = 0
            while True:
                if in_data:
                    end = buf.find(b"</data>", pos)
                    if end == -1:
                        if capture_modobj:
                            modobj_parts.append(buf[pos:])
                        tail = b""
                        pos = len(buf)
                        break
                    if capture_modobj:
                        modobj_parts.append(buf[pos:end])
                        cur["_modobj"] = b"".join(modobj_parts)
                        modobj_parts = []
                        capture_modobj = False
                    in_data = False
                    pos = end + 7
                    continue
                m = tag_re.search(buf, pos)
                if not m:
                    tail = buf[max(pos, len(buf) - 64):]
                    break
                pos = m.end()
                tok = m.group(0)
                if tok.startswith(b"<key>"):
                    last_key = m.group(1).decode("utf-8", "replace")
                elif tok.startswith(b"<string>"):
                    val = m.group(2).decode("utf-8", "replace")
                    if last_key == "name":
                        # a name closes the previous item's collection window
                        if "name" in cur:
                            items.append(cur)
                            cur = {}
                        cur["name"] = _unescape(val)
                    elif last_key == "libraryItemID":
                        cur["libraryItemID"] = val
                    elif last_key == "storeAdamID":
                        cur["storeAdamID"] = val
                    elif last_key == "kind":
                        cur["kind"] = val
                elif tok == b"<data>":
                    in_data = True
                    if last_key == "modobj":
                        capture_modobj = True
    if cur:
        items.append(cur)
    out = []
    for rank, it in enumerate(items):
        pid = None
        store_id = it.get("storeAdamID")
        if "libraryItemID" in it:
            m = re.search(r"PID:0x([0-9a-fA-F]+)", it["libraryItemID"])
            if m:
                pid = m.group(1).upper().zfill(16)
        elif "_modobj" in it:
            pid, sid = _modobj_ids(it["_modobj"])
            store_id = store_id or sid
        out.append({"rank": rank, "name": it.get("name", ""), "pid": pid,
                    "store_id": store_id, "kind": it.get("kind", "song")})
    return out


def _unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"'))


def _modobj_ids(b64: bytes):
    """Pull DeviceLibraryPersistentID / store adamID out of the NSKeyedArchiver
    blob without untangling the whole object graph."""
    try:
        pl = plistlib.loads(base64.b64decode(re.sub(rb"\s", b"", b64)))
    except Exception:
        return None, None
    pid = store = None
    for obj in pl.get("$objects", []):
        if isinstance(obj, dict):
            lib = obj.get("MPIdentifierSetDeviceLibraryPersistentID")
            if isinstance(lib, int) and lib:
                pid = format(lib & 0xFFFFFFFFFFFFFFFF, "016X")
            adam = obj.get("MPIdentifierSetStoreSubscriptionAdamID")
            if isinstance(adam, int) and adam and not store:
                store = str(adam)
    return pid, store


def store_history(conn, items, taken_at=None) -> None:
    taken_at = taken_at or time.time()
    for it in items:
        conn.execute("INSERT OR IGNORE INTO history_snapshots VALUES(?,?,?,?,?,?)",
                     (taken_at, it["rank"], it["name"], it["pid"],
                      it["store_id"], it["kind"]))
    conn.commit()


def latest_history(conn) -> list:
    at = conn.execute("SELECT MAX(taken_at) m FROM history_snapshots").fetchone()["m"]
    if at is None:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM history_snapshots WHERE taken_at=? ORDER BY rank", (at,))]


def history_age(conn) -> dict:
    """Both staleness layers of History.dat evidence: when we last read it,
    and that its items carry no timestamps at all."""
    at = conn.execute("SELECT MAX(taken_at) m FROM history_snapshots").fetchone()["m"]
    if at is None:
        return {"known": False, "reason": "no History.dat snapshot taken yet"}
    return {"snapshot_at": at,
            "snapshot_taken": time.strftime("%Y-%m-%d %H:%M", time.localtime(at)),
            "snapshot_age_h": round((time.time() - at) / 3600, 1),
            "item_timestamps": "none — History.dat is order-only; any item "
                               "may predate the snapshot by days or years"}


def month_add(ym: str, delta: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) - 1 + delta
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def month_bounds(ym: str) -> tuple:
    start = time.mktime(time.strptime(ym + "-01", "%Y-%m-%d"))
    end = time.mktime(time.strptime(month_add(ym, 1) + "-01", "%Y-%m-%d"))
    return start, end


# --------------------------------------------------------- RecentSearches

def parse_recent_searches(path=RECENT_SEARCHES) -> list:
    """Timestamped manual search intent. The term text is stored by Apple as
    catalog store-IDs only — unrecoverable offline; we keep timestamp + kind
    + the raw identifier blob hash so a future device-side join could resolve."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for section in data.values():
        for it in section:
            out.append({
                "searched_at": APPLE_EPOCH + float(it.get("dateAdded", 0)),
                "kind": it.get("kind", ""),
                "raw_identifier": it.get("identifiers", "")[:64]})
    return out


def store_searches(conn, rows) -> None:
    for r in rows:
        conn.execute("INSERT OR IGNORE INTO searches VALUES(?,?,?,NULL)",
                     (r["searched_at"], r["kind"], r["raw_identifier"]))
    conn.commit()


# ------------------------------------------------------------- knowledgeC

def knowledgec_status() -> dict:
    """FDA-gated. Degrades to a status dict, never raises."""
    try:
        conn = sqlite3.connect(f"file:{KNOWLEDGEC}?mode=ro", uri=True, timeout=5)
        n = conn.execute(
            "SELECT COUNT(*) FROM ZOBJECT WHERE ZSTREAMNAME='/media/nowPlaying'"
        ).fetchone()[0]
        conn.close()
        return {"readable": True, "now_playing_rows": n}
    except sqlite3.OperationalError as exc:
        return {"readable": False, "error": str(exc)}


def import_knowledgec(conn) -> int:
    """Import /media/nowPlaying rows as events(source='knowledgec').
    Dedup rule (design open question 2): daemon wins inside its uptime
    windows; knowledgeC fills gaps. Re-run safe (unique key)."""
    status = knowledgec_status()
    if not status["readable"]:
        return 0
    kc = sqlite3.connect(f"file:{KNOWLEDGEC}?mode=ro", uri=True, timeout=10)
    kc.row_factory = sqlite3.Row
    rows = kc.execute("""
        SELECT ZOBJECT.ZSTARTDATE s, ZOBJECT.ZENDDATE e,
               ZSTRUCTUREDMETADATA.Z_DKNOWPLAYINGMETADATAKEY__TITLE t,
               ZSTRUCTUREDMETADATA.Z_DKNOWPLAYINGMETADATAKEY__ARTIST a,
               ZSTRUCTUREDMETADATA.Z_DKNOWPLAYINGMETADATAKEY__ALBUM al,
               ZSTRUCTUREDMETADATA.Z_DKNOWPLAYINGMETADATAKEY__DURATION d
        FROM ZOBJECT
        LEFT JOIN ZSTRUCTUREDMETADATA
          ON ZOBJECT.ZSTRUCTUREDMETADATA = ZSTRUCTUREDMETADATA.Z_PK
        WHERE ZSTREAMNAME='/media/nowPlaying'""").fetchall()
    kc.close()
    n = 0
    for r in rows:
        if not r["t"]:
            continue
        start = APPLE_EPOCH + r["s"]
        # daemon-wins: skip if a daemon event overlaps this window
        clash = conn.execute(
            "SELECT 1 FROM events WHERE source='daemon' AND "
            "started_at BETWEEN ?-30 AND ?+30", (start, start)).fetchone()
        if clash:
            continue
        dur = r["d"] or 0
        heard = (APPLE_EPOCH + r["e"]) - start if r["e"] else 0
        frac = min(heard / dur, 1.0) if dur else 0
        pid_row = conn.execute(
            "SELECT pid FROM tracks WHERE name=? AND artist=?",
            (r["t"], r["a"] or "")).fetchone()
        cur = conn.execute("""INSERT OR IGNORE INTO events
            (pid,name,artist,album,started_at,ended_at_s,duration_s,fraction,
             verdict,initiation,initiation_evidence,session_id,source,
             in_library,context_playlist)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,'knowledgec',?,NULL)""",
            (pid_row["pid"] if pid_row else None, r["t"], r["a"] or "",
             r["al"] or "", start, heard, dur, round(frac, 3),
             verdict_for(frac, thresholds(conn)), "unknown",
             "knowledgec has no initiation signal", 1 if pid_row else 0))
        n += cur.rowcount
    conn.commit()
    return n


# Apple's own generated playlists. A user who streams rather than plays the library makes
# these the only view of real listening: "Replay All Time" is a true
# most-played ranking including streams, and Shazam is discovery the user initiated.
GENERATED_PLAYLISTS = ("Replay All Time", "My Shazam Tracks", "Favorite Songs")


def refresh_generated_playlists(conn, names=GENERATED_PLAYLISTS) -> dict:
    """Capture ranked contents of Apple's generated playlists.

    Must run from a foreground agent context; osascript is TCC-blocked under
    launchd, which is why the daemon cannot do this itself.
    """
    captured, out = time.time(), {}
    for pl in names:
        esc = pl.replace('"', '\\"')
        try:
            raw = osa(f'''tell application "Music"
                if not (exists playlist "{esc}") then return ""
                set o to ""
                repeat with t in tracks of playlist "{esc}"
                    set p to ""
                    try
                        set p to persistent ID of t
                    end try
                    set o to o & (name of t) & "\\t" & (artist of t) & "\\t" & p & linefeed
                end repeat
                return o
            end tell''', timeout=90)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            out[pl] = {"known": False, "reason": str(exc)}
            continue
        rows = [ln.split("\t") for ln in raw.splitlines() if ln.strip()]
        lib = {r["pid"] for r in conn.execute("SELECT pid FROM tracks")}
        for i, parts in enumerate(rows, start=1):
            nm, ar = parts[0], (parts[1] if len(parts) > 1 else "")
            pid = parts[2] if len(parts) > 2 and parts[2] else None
            conn.execute("""INSERT OR IGNORE INTO generated_playlists
                VALUES(?,?,?,?,?,?,?)""",
                (captured, pl, i, nm, ar, pid, 1 if pid in lib else 0))
        out[pl] = {"known": True, "tracks": len(rows)}
    conn.commit()
    meta_set(conn, "generated_at", str(captured))
    return out


def replay_rank(conn, name: str, artist: str):
    """The user's true most-played rank, or None. Outranks play_count and skip ratio:
    tracks streamed constantly show few local plays and many skips."""
    row = conn.execute("""SELECT rank FROM generated_playlists
        WHERE playlist='Replay All Time' AND lower(name)=lower(?)
          AND lower(artist)=lower(?)
        ORDER BY captured_at DESC LIMIT 1""", (name, artist)).fetchone()
    return row["rank"] if row else None


def verdict_for(fraction: float, th: dict, duration_s: float = None) -> str:
    # a track shorter than the poll can resolve yields a fraction that measures
    # our sampling, not user interest: the 2s alarm tone always looked abandoned.
    # Say so instead of recording a rejection the user never made.
    if duration_s and duration_s < th["poll_s"] * 3:
        return "unobservable"
    if fraction >= th["complete_at"]:
        return "completed"
    if fraction < th["reject_below"]:
        return "abandoned"
    return "partial"


# ----------------------------------------------------------------- ingest

def ingest(conn) -> dict:
    """Fold jsonl feeds into SQLite. Idempotent (unique keys); offsets in
    meta only spare re-reading, correctness never depends on them."""
    counts = {"events": 0, "commands": 0, "snapshots": 0, "history": 0}
    for path, key, fold in ((SCROBBLES, "off_scrobbles", _fold_scrobble),
                            (COMMANDS, "off_commands", _fold_command),
                            (SNAPSHOTS_FEED, "off_snapshots", _fold_snapshot),
                            (HISTORY_FEED, "off_history", _fold_history)):
        if not path.exists():
            continue
        offset = int(meta_get(conn, key, "0"))
        size = path.stat().st_size
        if size < offset:
            offset = 0  # feed was rotated/truncated; re-fold (idempotent)
        with path.open("rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # torn tail write; re-read next ingest
                offset += len(raw)
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                counts[fold(conn, line)] += 1
        meta_set(conn, key, offset)
    _assign_sessions(conn)
    if not meta_get(conn, "daemon_first_run"):
        first = conn.execute("SELECT MIN(started_at) m FROM events "
                             "WHERE source='daemon'").fetchone()["m"]
        if first:
            meta_set(conn, "daemon_first_run", first)
    meta_set(conn, "last_ingest", time.time())
    conn.commit()
    return counts


def _fold_scrobble(conn, line) -> str:
    if line.get("type") == "daemon_start":
        prev = conn.execute(
            "SELECT MAX(started_at + ended_at_s) m FROM events WHERE source='daemon'"
        ).fetchone()["m"]
        if prev and line["at"] - prev > 600:
            conn.execute("INSERT OR IGNORE INTO coverage_gaps VALUES(?,?,?)",
                         (prev, line["at"], "daemon offline"))
        return "events"
    if line.get("type") != "play":
        return "events"
    conn.execute("""INSERT OR IGNORE INTO events
        (pid,name,artist,album,started_at,ended_at_s,duration_s,fraction,
         verdict,initiation,initiation_evidence,session_id,source,in_library,
         context_playlist)
        VALUES(:pid,:name,:artist,:album,:started_at,:ended_s,:duration_s,
               :fraction,:verdict,:initiation,:initiation_evidence,NULL,
               'daemon',:in_library,:context_playlist)""", line)
    return "events"


def _fold_command(conn, line) -> str:
    conn.execute("INSERT OR IGNORE INTO commands VALUES(?,?,?)",
                 (line["at"], line["cmd"], json.dumps(line.get("arg"))))
    return "commands"


def _fold_snapshot(conn, line) -> str:
    at = line["taken_at"]
    for pid, pc, sc, lp, ls in line["rows"]:
        conn.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?)",
                     (at, pid, pc, sc, lp, ls))
        conn.execute("""UPDATE tracks SET play_count=?, skip_count=?,
                        last_played=?, last_skipped=? WHERE pid=?""",
                     (pc, sc, lp, ls, pid))
    return "snapshots"


def _fold_history(conn, line) -> str:
    for rank, name, pid, store_id, kind in line["items"]:
        conn.execute("INSERT OR IGNORE INTO history_snapshots VALUES(?,?,?,?,?,?)",
                     (line["taken_at"], rank, name, pid, store_id, kind))
    return "history"


def _assign_sessions(conn) -> None:
    th = thresholds(conn)
    gap = th["session_gap_min"] * 60
    rows = conn.execute("""SELECT id, started_at, ended_at_s, initiation
        FROM events WHERE session_id IS NULL AND source='daemon'
        ORDER BY started_at""").fetchall()
    if not rows:
        return
    last = conn.execute("""SELECT s.id sid, MAX(e.started_at + e.ended_at_s) t
        FROM sessions s JOIN events e ON e.session_id = s.id""").fetchone()
    sid, last_end = (last["sid"], last["t"]) if last and last["sid"] else (None, 0)
    for r in rows:
        if sid is None or r["started_at"] - (last_end or 0) > gap:
            cur = conn.execute(
                "INSERT INTO sessions VALUES(NULL,?,?,?,?,'mac')",
                (r["started_at"], r["started_at"] + (r["ended_at_s"] or 0),
                 r["started_at"] - (last_end or r["started_at"]),
                 r["initiation"]))
            sid = cur.lastrowid
        conn.execute("UPDATE events SET session_id=? WHERE id=?", (sid, r["id"]))
        last_end = r["started_at"] + (r["ended_at_s"] or 0)
        conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (last_end, sid))


# -------------------------------------------------------------- detectors

def compute_strata(conn) -> dict:
    """Stranded-strata: freeze each played track into its final-play month.
    Frozen at bootstrap (a historical document); bias disclosed at query
    time: strata only show ENDED cycles and undercount replayed tracks."""
    now = time.time()
    conn.execute("DELETE FROM strata")
    for r in conn.execute("""SELECT pid, last_played FROM tracks
                             WHERE last_played IS NOT NULL AND is_nonmusic=0"""):
        month = time.strftime("%Y-%m", time.localtime(r["last_played"]))
        conn.execute("INSERT OR REPLACE INTO strata VALUES(?,?,?)",
                     (r["pid"], month, now))
    conn.commit()
    return strata_clusters(conn)


def strata_clusters(conn) -> dict:
    """month -> [{artist, tracks}] for artists holding >= strata_min_tracks."""
    th = thresholds(conn)
    out = {}
    for r in conn.execute("""
        SELECT s.stratum_month m, t.artist a, COUNT(*) n
        FROM strata s JOIN tracks t ON t.pid = s.pid
        WHERE t.is_nonmusic = 0
        GROUP BY m, a HAVING n >= ? ORDER BY m, n DESC""",
            (th["strata_min_tracks"],)):
        out.setdefault(r["m"], []).append({"artist": r["a"], "tracks": r["n"]})
    return out


def stratum_sizes(conn) -> dict:
    return {r["m"]: {"tracks": r["n"], "artists": r["na"]}
            for r in conn.execute("""
        SELECT s.stratum_month m, COUNT(*) n, COUNT(DISTINCT t.artist) na
        FROM strata s JOIN tracks t ON t.pid=s.pid WHERE t.is_nonmusic=0
        GROUP BY m""")}


def library_skip_ratio_median(conn) -> float:
    ratios = [r["r"] for r in conn.execute("""
        SELECT CAST(skip_count AS REAL)/play_count r FROM tracks
        WHERE play_count >= 5 AND is_nonmusic = 0 ORDER BY r""")]
    if not ratios:
        return 0.0
    return ratios[len(ratios) // 2]


def contamination_check(conn, pid) -> dict:
    """Snapshot-tier autoplay detector (DESIGN.md 1.2). Structural only —
    AND of four legs; legs that need >=2 snapshots report 'unavailable'.
    Flag requires every AVAILABLE leg true and at least 3 available."""
    t = conn.execute("SELECT * FROM tracks WHERE pid=?", (pid,)).fetchone()
    if not t:
        return {"flagged": False, "error": "not in tracks"}
    th = thresholds(conn)
    median = library_skip_ratio_median(conn)
    ratio = (t["skip_count"] / t["play_count"]) if t["play_count"] else 0.0
    legs = {}
    # a track too short to skip cannot accrue skips, so its flawless ratio
    # measures its length, not user taste (the 2s alarm outranked the library)
    dur = t["duration_s"] or 0
    unskippable = bool(dur and dur < th["poll_s"] * 3)
    legs["ratio_outlier"] = {
        "fired": bool(t["play_count"] >= 5 and median
                      and ratio >= th["contam_ratio_x"] * median),
        "evidence": f"skip/play {ratio:.2f} vs library median {median:.2f} "
                    f"(threshold {th['contam_ratio_x']}x)"
                    + (f"; CAVEAT {dur:.0f}s track is too short to skip or to "
                       "witness — this ratio is an artifact of its length, not "
                       "evidence of taste" if unskippable else "")}
    others = conn.execute("""SELECT COUNT(*) n, COALESCE(SUM(play_count),0) p
        FROM tracks WHERE artist = ? AND pid != ? AND is_nonmusic=0""",
        (t["artist"], t["pid"])).fetchone()
    legs["artist_isolated"] = {
        "fired": bool(others["n"] == 0
                      or others["p"] <= th["artist_neighbor_plays"]),
        "evidence": f"artist has {others['n']} other library tracks with "
                    f"{others['p']} total plays"}
    legs["not_favorited"] = {"fired": not t["favorited"],
                             "evidence": f"favorited={bool(t['favorited'])}"}
    # advancing last-played with no matching Mac event = other-device machine
    # plays (the CarPlay signature seen from this box)
    snaps = conn.execute("""SELECT taken_at, play_count, last_played
        FROM snapshots WHERE pid=? ORDER BY taken_at""", (pid,)).fetchall()
    if len(snaps) < 2:
        legs["advancing_unwitnessed"] = {
            "fired": None, "evidence":
            f"needs >=2 counter snapshots ({len(snaps)} taken)"}
    else:
        fired = False
        detail = "no unwitnessed advance"
        for a, b in zip(snaps, snaps[1:]):
            if (b["last_played"] or 0) > (a["last_played"] or 0):
                seen = conn.execute("""SELECT 1 FROM events WHERE pid=?
                    AND source='daemon' AND started_at BETWEEN ? AND ?""",
                    (pid, a["taken_at"], b["taken_at"])).fetchone()
                if not seen:
                    fired = True
                    detail = ("last_played advanced %s -> %s with no Mac-side "
                              "event" % (_day(a["last_played"]),
                                         _day(b["last_played"])))
                    break
        legs["advancing_unwitnessed"] = {"fired": fired, "evidence": detail}
    available = [l for l in legs.values() if l["fired"] is not None]
    flagged = (len(available) >= 3 and all(l["fired"] for l in available))
    return {"flagged": flagged, "legs": legs,
            "legs_available": len(available),
            "confidence": ("snapshot_tier; all %d available legs fired"
                           % len(available)) if flagged else None,
            "policy": "discount, never blocklist; a manual play outweighs this",
            "provenance": "inferred"}


def intent_weighted_plays(conn, pid) -> dict:
    """Daemon-era plays with machine-initiated and abandoned ones excluded.
    Null before the daemon epoch — pretending is worse (DESIGN.md 5)."""
    row = conn.execute("""SELECT COUNT(*) n FROM events WHERE pid=?
        AND source='daemon' AND initiation NOT LIKE 'autoplay%'
        AND verdict NOT IN ('abandoned','unobservable')""", (pid,)).fetchone()
    epoch = meta_get(conn, "daemon_first_run")
    if not epoch:
        return {"value": None, "note": "pre-daemon; no intent data exists yet"}
    return {"value": row["n"],
            "note": "daemon-era only (since %s); excludes autoplay_*, "
                    "abandoned, and too-short-to-observe" % _day(float(epoch))}


def annotations_for(conn, name=None, artist=None) -> list:
    """User verbatims for one track/artist (evidence, never a filter).
    Track-scope notes bind by their exact 'Name — Artist' ref, never by
    substring: short names ('Time', 'Run', 'Go') must not inherit other
    tracks' testimony. Wider scopes match the artist on word boundaries."""
    out = []
    for r in conn.execute("SELECT * FROM annotations ORDER BY at"):
        if r["scope"] == "track":
            rn, _, ra = (r["ref"] or "").partition(" — ")
            hit = (name and rn.strip().lower() == name.strip().lower()
                   and (not artist or not ra.strip()
                        or ra.strip().lower() == artist.strip().lower()))
        else:
            hay = (r["ref"] or "") + " " + (r["verbatim"] or "")
            hit = artist and re.search(
                r"(?<!\w)%s(?!\w)" % re.escape(artist.strip()), hay, re.I)
        if hit:
            out.append({"at": _day(r["at"]), "scope": r["scope"],
                        "ref": r["ref"], "verbatim": r["verbatim"],
                        "valence": r["valence"], "source": r["source"]})
    return out


def _day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "never"


def days_since(ts) -> int:
    return int((time.time() - ts) / 86400) if ts else None


def find_track(conn, pid=None, name=None, artist=None):
    if pid:
        return conn.execute("SELECT * FROM tracks WHERE pid=?", (pid,)).fetchone()
    if name and artist:
        return conn.execute(
            "SELECT * FROM tracks WHERE name=? COLLATE NOCASE AND "
            "artist=? COLLATE NOCASE", (name, artist)).fetchone()
    if name:
        return conn.execute(
            "SELECT * FROM tracks WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return None
