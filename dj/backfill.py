#!/usr/bin/env python3
"""DJ Claude backfill — reconstruct what today's library can still tell us.

Reads (never writes) the user's library: AppleScript full scan, History.dat
order, RecentSearches, knowledgeC if FDA allows. Writes only ~/.claude/dj/.
Run: python3 ~/.claude/dj/backfill.py   (safe to re-run; idempotent)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import djlib

# User verbatims witnessed by agents (GROUND_TRUTH.md + taste.json), stored
# as evidence the DJ can quote — never compiled into filters. taste.json's
# hand blocklist/cycles are NOT migrated: the detectors replace them, and
# deleting the user's words needs user sign-off (DESIGN.md open question 3).
SEED_ANNOTATIONS_FILE = Path(__file__).parent / "seed_annotations.json"


def _load_seeds():
    """User verbatims are personal data: they live in a local JSON beside the
    store, never in shipped code. Missing file = no seeds, which is normal."""
    try:
        rows = json.loads(SEED_ANNOTATIONS_FILE.read_text())
        return [(r["scope"], r["ref"], r["verbatim"], r["valence"], r["source"])
                for r in rows]
    except (OSError, ValueError, KeyError):
        return []


SEED_ANNOTATIONS = _load_seeds()


def main() -> None:
    t0 = time.time()
    conn = djlib.db()
    report = {}

    print("[1/5] AppleScript full-library scan (read-only)...")
    rows = djlib.scan_library()
    djlib.store_scan(conn, rows)
    music = [r for r in rows if not r["is_nonmusic"]]
    played = [r for r in music if r["last_played"]]
    report["tracks"] = len(rows)
    report["music_tracks"] = len(music)
    report["nonmusic"] = len(rows) - len(music)
    report["with_last_played"] = len(played)
    report["zero_play"] = sum(1 for r in music if r["play_count"] == 0)
    report["one_play"] = sum(1 for r in music if r["play_count"] == 1)
    report["favorited"] = sum(1 for r in music if r["favorited"])
    report["total_plays"] = sum(r["play_count"] for r in music)
    yrs = [r["year"] for r in music if r["year"]]
    report["year_populated"] = f"{len(yrs)}/{len(music)}"

    print("[2/5] Stranded-strata computation...")
    clusters = djlib.compute_strata(conn)
    report["strata_months"] = len(djlib.stratum_sizes(conn))
    report["strata_clusters"] = {
        m: [f"{c['artist']} ({c['tracks']} tracks)" for c in cs]
        for m, cs in sorted(clusters.items())}

    print("[3/5] History.dat order snapshot (streamed, artwork skipped)...")
    if djlib.HISTORY_DAT.exists():
        items = djlib.parse_history_dat()
        djlib.store_history(conn, items)
        resolved = sum(1 for i in items if i["pid"])
        report["history_items"] = len(items)
        report["history_pid_resolved"] = resolved
        report["history_tail_5_most_recent"] = [
            i["name"] for i in items[-5:]]
    else:
        report["history_items"] = "History.dat not found"

    print("[4/5] RecentSearches + knowledgeC (degrades without FDA)...")
    searches = djlib.parse_recent_searches()
    djlib.store_searches(conn, searches)
    report["searches"] = [
        {"at": djlib._day(s["searched_at"]), "kind": s["kind"],
         "note": "term text is store-IDs only; unrecoverable offline"}
        for s in searches]
    kc = djlib.knowledgec_status()
    if kc["readable"]:
        n = djlib.import_knowledgec(conn)
        report["knowledgec"] = f"imported {n} events ({kc['now_playing_rows']} rows)"
    else:
        report["knowledgec"] = ("not readable (no Full Disk Access). Grant: "
                                "System Settings > Privacy & Security > Full "
                                "Disk Access for the daemon's host (Terminal)."
                                " Unlocks ~4 weeks of true per-play backfill.")

    print("[5/5] Seeding user-verbatim annotations + contamination check...")
    for scope, ref, verbatim, valence, source in SEED_ANNOTATIONS:
        dup = conn.execute(
            "SELECT 1 FROM annotations WHERE ref=? AND verbatim=?",
            (ref, verbatim)).fetchone()
        if not dup:
            conn.execute("INSERT INTO annotations VALUES(NULL,?,?,?,?,?,?)",
                         (time.time(), scope, ref, verbatim, valence, source))
    conn.commit()

    flagged = []
    for r in conn.execute("""SELECT pid, name, artist FROM tracks
                             WHERE play_count >= 5 AND is_nonmusic=0"""):
        c = djlib.contamination_check(conn, r["pid"])
        if c["flagged"]:
            flagged.append(f"{r['name']} — {r['artist']}: " + "; ".join(
                l["evidence"] for l in c["legs"].values() if l["fired"]))
    report["contamination_flagged"] = flagged or "none"
    report["skip_ratio_median"] = round(djlib.library_skip_ratio_median(conn), 3)

    djlib.meta_set(conn, "backfill_at", time.time())
    report["elapsed_s"] = round(time.time() - t0, 1)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
