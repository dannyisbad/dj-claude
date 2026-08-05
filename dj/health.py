#!/usr/bin/env python3
"""DJ Claude health — one command, the whole truth.

Run: python3 ~/.claude/dj/health.py        (add --json for the raw dict)
Says: daemon state, plays logged, what TCC would unlock, backfill currency.
Same code path as the MCP's system_health tool, so they can never disagree.
"""
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import djlib

spec = importlib.util.spec_from_file_location(
    "music_mcp", Path.home() / ".claude" / "mcp" / "music-mcp.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


def main() -> None:
    conn = djlib.db()
    try:
        djlib.ingest(conn)
    except Exception as exc:
        print(f"WARNING: ingest failed: {exc}")
    h = mcp.system_health({}, conn)
    if "--json" in sys.argv:
        print(json.dumps(h, indent=1, ensure_ascii=False))
        return
    d = h["daemon"]
    print(h["summary"])
    print()
    print(f"  tier                : {h['tier']}")
    print(f"  daemon process      : "
          f"{'running' if d['process_running'] else 'NOT RUNNING'}"
          + (" (probes blocked)" if d["probes_blocked"] else ""))
    print(f"  daemon last status  : {d['last_status']}")
    print(f"  plays logged        : {d['events_logged']} daemon events "
          f"(total events {h['store_counts']['events']})")
    print(f"  accumulating since  : {d['accumulating_since'] or 'never'}")
    print(f"  coverage            : {h['coverage']['events_window']}; "
          f"{len(h['coverage']['gaps'])} known gap(s)")
    backfill = h["backfill_at"] or "NEVER RUN — run python3 ~/.claude/dj/backfill.py"
    print(f"  backfill            : {backfill}")
    print(f"  launchd installed   : {h['launchd_installed']}")
    print(f"  knowledgeC (FDA)    : "
          + ("readable" if h["knowledgec"].get("readable")
             else "locked — " + h["knowledgec"].get("grant", "")))
    print(f"  thresholds          : {h['thresholds_in_force']}")
    print(f"  uninstall           : {h['uninstall']}")


if __name__ == "__main__":
    main()
