#!/usr/bin/env python3
"""GROUND_TRUTH gate: the four labeled episodes must reproduce.

Episodes 1 and 4 replay synthetic observations through the daemon's real
classifier (no player touched); 2 and 3 run against the real store.
Run: python3 ~/.claude/dj/test_gates.py
"""
import importlib.util
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import djlib
import daemon as dj_daemon

MCP_PATH = Path(__file__).parent.parent / "mcp" / "music-mcp.py"
MCP_SPEC = importlib.util.spec_from_file_location("music_mcp", MCP_PATH)
music_mcp = importlib.util.module_from_spec(MCP_SPEC)
MCP_SPEC.loader.exec_module(music_mcp)

FAILURES = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


def make_daemon():
    d = dj_daemon.Daemon.__new__(dj_daemon.Daemon)
    d.current = None
    d.last_pos = 0.0
    d.last_ctx = ""
    d.library_pids = {"SUBWAY1", "PONY2", "BABE3", "CASUAL4",
                      "BLACK5", "NORAIN6", "GIRL7", "CALI8"}
    d.lib_loaded_at = time.time()
    d.cmd_offset = 0
    d.recent_cmds = []
    d.queue_pids = []
    d.catalog_queue = []
    d.state = {}
    d.th = dict(djlib.THRESHOLDS)
    return d


def obs(pid, name, artist, context=""):
    return {"pid": pid, "name": name, "artist": artist, "album": artist,
            "duration": 200.0, "position": 0.0, "context": context,
            "shuffle": False}


def episode_1():
    """One Chappell Roan vote, three autoplay artifacts."""
    d = make_daemon()
    labels = []
    # the user starts The Subway from idle
    labels.append(d.classify(obs("SUBWAY1", "The Subway", "Chappell Roan"),
                             None, None, from_idle=True))
    run = [("PONY2", "Pink Pony Club"), ("BABE3", "Good Luck, Babe!"),
           ("CASUAL4", "Casual")]
    prev = {"pid": "SUBWAY1", "name": "The Subway", "artist": "Chappell Roan"}
    for pid, name in run:
        # each previous track completed naturally (fraction 1.0), no context
        labels.append(d.classify(obs(pid, name, "Chappell Roan"),
                                 prev, 1.0, from_idle=False))
        prev = {"pid": pid, "name": name, "artist": "Chappell Roan"}
    kinds = [k for k, _ in labels]
    check("E1: The Subway is the one manual vote", kinds[0] == "manual",
          f"got {kinds[0]}: {labels[0][1]}")
    check("E1: run tail is machine-initiated, never manual",
          all(k in ("autoplay_run", "autoplay_falloff") for k in kinds[1:]),
          str(kinds[1:]))


def episode_4():
    """Manual picks spanning five genres in one sitting all read as manual."""
    d = make_daemon()
    seq = [("BLACK5", "Black", "Pearl Jam"),
           ("NORAIN6", "No Rain", "Blind Melon"),
           ("SUBWAY1", "The Subway", "Chappell Roan")]
    labels = [d.classify(obs(*seq[0]), None, None, from_idle=True)]
    prev = {"pid": seq[0][0], "name": seq[0][1], "artist": seq[0][2]}
    for pid, name, artist in seq[1:]:
        # the user cuts each track mid-flight to pick the next
        labels.append(d.classify(obs(pid, name, artist), prev, 0.5, False))
        prev = {"pid": pid, "name": name, "artist": artist}
    # Claude skips (journaled), then the user overrides with two manual picks
    d.recent_cmds = [{"at": time.time(), "cmd": "next_track", "arg": None}]
    dj_pick = d.classify(obs("GIRL7", "Just a Girl", "No Doubt"), prev, 0.4, False)
    d.recent_cmds = []
    manual2 = d.classify(obs("CALI8", "California Love", "2Pac"),
                         {"pid": "GIRL7", "name": "Just a Girl",
                          "artist": "No Doubt"}, 0.5, False)
    check("E4: user mid-flight changes are manual",
          all(k == "manual" for k, _ in labels), str([k for k, _ in labels]))
    check("E4: Claude's journaled skip is NOT credited as a user pick",
          dj_pick[0] == "dj_queue", f"got {dj_pick}")
    check("E4: user override after Claude's pick is manual",
          manual2[0] == "manual", f"got {manual2}")


def episode_4_restart():
    """A daemon restarted mid-queue has no prev, so the frozen-queue check
    cannot fire. It must not bill the advance to the user's hand."""
    d = make_daemon()
    d.queue_pids = ["BLACK5", "NORAIN6", "GIRL7"]
    mid = d.classify(obs("NORAIN6", "No Rain", "Blind Melon"),
                     None, None, from_idle=False)
    check("E4-restart: queue advance after a restart is NOT credited to the user",
          mid[0] != "manual", f"got {mid}")
    check("E4-restart: it says unknown instead of guessing",
          mid[0] == "unknown", f"got {mid}")
    # the real from-idle pick must still read as manual: don't over-correct
    solo = d.classify(obs("CALI8", "California Love", "2Pac"),
                      None, None, from_idle=True)
    check("E4-restart: a from-idle pick outside the queue is still manual",
          solo[0] == "manual", f"got {solo}")


def episode_4_subpoll():
    """A queued track shorter than the poll leaves no observation. The pick
    AFTER it must still be credited to the DJ, and the unseen one recorded."""
    d = make_daemon()
    d.queue_pids = ["BLACK5", "NORAIN6", "GIRL7"]   # NORAIN6 stands in for the 2s tone
    prev = {"pid": "BLACK5", "name": "Black", "artist": "Pearl Jam"}
    after = d.classify(obs("GIRL7", "Just a Girl", "No Doubt",
                           context=djlib.DJ_PLAYLIST), prev, 1.0, False)
    check("E4-subpoll: pick after an unwitnessed short track is still dj_queue",
          after[0] == "dj_queue", f"got {after}")
    check("E4-subpoll: the evidence admits the queue skipped past a track",
          "too short" in after[1], f"got {after[1]}")
    check("E4-subpoll: the passed-over track is identified for the record",
          d.skipped_between("BLACK5", "GIRL7") == ["NORAIN6"],
          str(d.skipped_between("BLACK5", "GIRL7")))
    # adjacent advance must stay the plain prediction, not claim a skip
    adj = d.classify(obs("NORAIN6", "No Rain", "Blind Melon",
                         context=djlib.DJ_PLAYLIST), prev, 1.0, False)
    check("E4-subpoll: an adjacent advance does not invent a skipped track",
          adj[0] == "dj_queue" and "too short" not in adj[1], f"got {adj}")


def episode_4_catalog_append():
    """Two catalog_queue dispatches: Music appends, so picks still pending from
    the first must keep their DJ attribution after a second lands."""
    d = make_daemon()
    d.recent_cmds = []
    d.catalog_queue = [{"name": "Black Hole Sun", "artist": "Soundgarden"},
                       {"name": "Alive", "artist": "Pearl Jam"}]
    line = {"at": time.time(), "cmd": "catalog_queue",
            "arg": {"picks": [{"name": "Barracuda", "artist": "Heart"}]}}
    # replay the journal-folding branch the daemon uses
    if time.time() - float(line["at"]) < 12 * 3600:
        d.catalog_queue += [{"name": p["name"], "artist": p["artist"]}
                            for p in line["arg"]["picks"]]
        d.catalog_queue = d.catalog_queue[-25:]
    got = d.classify(obs("XCAT1", "Black Hole Sun", "Soundgarden"),
                     {"pid": "P0", "name": "Everlong", "artist": "Foo Fighters"},
                     1.0, False)
    check("E4-catalog: a second dispatch does not orphan the first's picks",
          got[0] == "dj_queue", f"got {got}")
    check("E4-catalog: later dispatch is still tracked",
          any(p["name"] == "Barracuda" for p in d.catalog_queue),
          str([p["name"] for p in d.catalog_queue]))


def episode_2(conn):
    """The CarPlay track flags structurally — no artist name in any rule."""
    row = djlib.find_track(conn, name="About Damn Time")
    if not row:
        print("SKIP  E2: store-dependent episode needs the original library "
              "(fresh install — synthetic episodes 1/4 still cover the "
              "classifier)")
        return
    c = djlib.contamination_check(conn, row["pid"])
    check("E2: About Damn Time flagged by structure alone", c["flagged"],
          "; ".join(f"{k}={v['fired']}" for k, v in c["legs"].items()))
    src = Path(__file__).parent
    code = "".join((src / f).read_text()
                   for f in ("djlib.py", "daemon.py", "backfill.py"))
    code_no_seeds = code.replace(
        code[code.index("SEED_ANNOTATIONS"):code.index("def main")], "")
    check("E2: zero name-matching in detector code",
          "Lizzo" not in code_no_seeds and "Damn Time" not in code_no_seeds)
    # the loved-but-skippy favorited track must NOT flag (The Chain class)
    chain = djlib.find_track(conn, name="The Chain")
    if chain:
        cc = djlib.contamination_check(conn, chain["pid"])
        check("E2: favorited high-skip track is NOT flagged",
              not cc["flagged"],
              f"{chain['play_count']}p/{chain['skip_count']}s favorited={chain['favorited']}")


def episode_3(conn):
    """User verbatims must be retrievable as evidence for those artists."""
    if not conn.execute("SELECT 1 FROM annotations LIMIT 1").fetchone():
        print("SKIP  E3: no annotations in this store yet (fresh install)")
        return
    hippy = djlib.annotations_for(conn, artist="Slowdive")
    sublime = djlib.annotations_for(conn, "Doin' Time")
    check("E3: 'too weird hippy' surfaces for Slowdive",
          any("too weird hippy" in a["verbatim"] for a in hippy))
    check("E3: 'doin time is good tho' surfaces for the Sublime track",
          any("good tho" in a["verbatim"] for a in sublime))
    # short names must not inherit other tracks' testimony by substring
    for nm, ar in (("Time", "Pink Floyd"), ("Run", None), ("Go", None)):
        stray = djlib.annotations_for(conn, nm, ar)
        check(f"E3: no misattributed verbatims on short name '{nm}'",
              not stray, "; ".join(a["ref"] for a in stray) or "clean")


def episode_4_mood(conn):
    """A scattered month must never produce a single-genre mood."""
    sizes = djlib.stratum_sizes(conn)
    clusters = djlib.strata_clusters(conn)
    month = time.strftime("%Y-%m")
    cur = sizes.get(month, {"tracks": 0, "artists": 0})
    check("E4-mood: current month is genuinely scattered in real data",
          month not in clusters or cur["artists"] > 8,
          f"{cur['tracks']} tracks / {cur['artists']} artists this month")


def operational_contracts(conn):
    """Runtime promises the original four episode fixtures do not exercise."""
    d = make_daemon()
    d.osa_usable = None
    d.lib_loaded_at = time.time()
    d.state = {"last_history_snapshot": 0}
    d.read_commands = lambda: None
    d._save_state = lambda: None
    called = []
    old_parse = djlib.parse_history_dat
    djlib.parse_history_dat = lambda: called.append(True) or []
    try:
        d.housekeeping()
    finally:
        djlib.parse_history_dat = old_parse
    check("OPS: notify daemon never hammers TCC-blocked History.dat",
          not called)

    check("OPS: completion behavior ships in MCP instructions",
          "Breakthroughs count" in music_mcp.SERVER_INSTRUCTIONS and
          "routine greens" in music_mcp.SERVER_INSTRUCTIONS and
          "announce the gag" in music_mcp.SERVER_INSTRUCTIONS)
    check("OPS: instructions are user-neutral (releasable)",
          not __import__("re").search(
              r"\b[Ss]he\b|\b[Hh]er\b|\bhers\b|[Dd]ani|\b[Oo]wner\b",
              music_mcp.SERVER_INSTRUCTIONS))
    check("OPS: explicit preference ships in MCP instructions",
          "explicit" in music_mcp.SERVER_INSTRUCTIONS.lower())
    check("OPS: play_moment is discoverable", "play_moment" in music_mcp.TOOLS)
    check("OPS: catalog search/play/queue are discoverable",
          all(t in music_mcp.TOOLS for t in
              ("catalog_search", "catalog_play", "catalog_queue")))

    old_state = music_mcp._player_state
    old_osa = music_mcp.osa
    old_live = music_mcp.live_player
    old_journal = music_mcp.journal
    try:
        music_mcp._player_state = lambda: "paused"
        paused = music_mcp.play_moment(
            {"kind": "completion_celebration", "reason": "verified done"},
            conn)
        # user directive 2026-08-04: pause is mostly dead air, not a wall.
        # A moment may fire over a paused player; the judgment for a fresh
        # mid-track pause belongs to the caller, not a hard refusal here.
        check("OPS: play_moment is NOT blocked by a paused player",
              not (paused.get("refused")
                   and "pause" in str(paused.get("reason", ""))))

        row = conn.execute("SELECT * FROM tracks LIMIT 1").fetchone()
        if row is None:
            print("SKIP  OPS: play_moment journal/verify needs a scanned "
                  "library (fresh install — run backfill.py first)")
        else:
            calls = []
            music_mcp._player_state = lambda: "playing"
            music_mcp.osa = (lambda script, timeout=15:
                             calls.append(script) or "")
            music_mcp.live_player = lambda: {
                "state": "playing", "pid": row["pid"], "name": row["name"],
                "artist": row["artist"]}
            music_mcp.journal = lambda cmd, arg=None: calls.append((cmd, arg))
            landed = music_mcp.play_moment({
                "kind": "completion_celebration",
                "reason": "final suite green and deployed",
                "pid": row["pid"]}, conn)
            check("OPS: play_moment journals and verifies the exact track",
                  landed.get("playing", {}).get("pid") == row["pid"] and
                  any(isinstance(c, tuple) and c[0] == "play_moment"
                      for c in calls))
    finally:
        music_mcp._player_state = old_state
        music_mcp.osa = old_osa
        music_mcp.live_player = old_live
        music_mcp.journal = old_journal


def catalog_contracts(conn):
    """Catalog identity, exact dispatch, pause semantics, attribution."""
    code = MCP_PATH.read_text()
    check("CAT: immediate catalog play uses native workflow + exact readback",
          "CATALOG_PLAY_SHORTCUT" in code and
          "set player position to 0" in code and
          "_same_track(readback, ref)" in code)
    sample = {
        "wrapperType": "track", "kind": "song",
        "trackId": 1444107530, "collectionId": 1444107292,
        "trackName": "Celebration (Single Version)",
        "artistName": "Kool & The Gang", "collectionName": "Celebrate!",
        "trackViewUrl": ("https://music.apple.com/us/album/"
                         "celebration-single-version/1444107292"
                         "?i=1444107530&uo=4"),
        "trackTimeMillis": 299000, "primaryGenreName": "R&B/Soul",
        "trackExplicitness": "notExplicit",
    }
    ref = music_mcp._catalog_ref_from_row(sample)
    old_search = music_mcp._itunes_search
    old_run = music_mcp.subprocess.run
    old_state = music_mcp._player_state
    old_play = music_mcp._play_catalog_ref
    old_journal = music_mcp.journal
    old_sleep = music_mcp.time.sleep
    try:
        music_mcp._itunes_search = lambda q, country="US", limit=5: [sample]
        found = music_mcp.catalog_search(
            {"query": "Celebration (Single Version)",
             "artist": "Kool & The Gang"}, conn)
        got = found["candidates"][0]["catalog_ref"]
        check("CAT: search returns stable IDs and provenance",
              got["track_id"] == 1444107530 and
              got["collection_id"] == 1444107292 and
              got["provenance"] == "itunes_search_api")

        calls = []
        class Done:
            returncode = 0
            stdout = ""
            stderr = ""
        music_mcp.subprocess.run = lambda argv, **kw: calls.append(argv) or Done()
        music_mcp._dispatch_queue_ref(ref)
        url = calls[-1][-1]
        check("CAT: queue preflights exact ID and percent-escapes Shortcut URL",
              url.startswith("shortcuts://run-shortcut?") and
              "Celebration%20%28Single%20Version%29" in url and
              "+" not in url)

        wrong = dict(sample, trackId=999)
        music_mcp._itunes_search = lambda q, country="US", limit=5: [wrong]
        refused_ambiguous = False
        try:
            music_mcp._dispatch_queue_ref(ref)
        except RuntimeError as exc:
            refused_ambiguous = "ambiguous" in str(exc)
        check("CAT: queue refuses a same-query wrong catalog ID",
              refused_ambiguous)

        music_mcp._player_state = lambda: "paused"
        paused = music_mcp.catalog_play(
            {"catalog_ref": ref, "reason": "play it"}, conn)
        check("CAT: immediate catalog play requires explicit user request",
              paused.get("refused") is True)
        music_mcp._play_catalog_ref = lambda r: {
            "state": "playing", "name": r["name"], "artist": r["artist"]}
        journaled = []
        music_mcp.journal = lambda cmd, arg=None: journaled.append((cmd, arg))
        landed = music_mcp.catalog_play({
            "catalog_ref": ref, "reason": "user said play Celebration",
            "user_requested": True}, conn)
        check("CAT: explicit user request can play from paused + verifies",
              landed.get("playing", {}).get("track_id") == 1444107530 and
              journaled[-1][0] == "catalog_play")
    finally:
        music_mcp._itunes_search = old_search
        music_mcp.subprocess.run = old_run
        music_mcp._player_state = old_state
        music_mcp._play_catalog_ref = old_play
        music_mcp.journal = old_journal
        music_mcp.time.sleep = old_sleep

    d = make_daemon()
    d.catalog_queue = [{"name": "Celebration (Single Version)",
                        "artist": "Kool & The Gang"}]
    label = d.classify(obs("CATALOG", "Celebration (Single Version)",
                           "Kool & The Gang"),
                       {"pid": "OLD", "name": "Old", "artist": "Other"},
                       1.0, False)
    check("CAT: delayed native queue play is attributed to DJ, not user",
          label[0] == "dj_queue" and not d.catalog_queue, str(label))


if __name__ == "__main__":
    conn = djlib.db()
    episode_1()
    episode_2(conn)
    episode_3(conn)
    episode_4()
    episode_4_restart()
    episode_4_subpoll()
    episode_4_catalog_append()
    episode_4_mood(conn)
    operational_contracts(conn)
    catalog_contracts(conn)
    print("\n%d failures" % len(FAILURES))
    sys.exit(1 if FAILURES else 0)
