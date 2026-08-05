# DJ Claude

Give Claude a real DJ residency on your Mac. A per-play witness daemon, a
listening-intelligence MCP server for Music.app, and a skill that turns any
Claude Code session into the DJ.

The design premise: Apple Music refuses to be a witness — play counts are
lifetime-cloud-synced lies, skips don't encode position, and the native
Playing Next queue can't even be read back. DJ Claude builds the missing
observability layer and then puts *judgment* (the model), not an algorithm,
in charge of the aux.

## What you get

- **`dj/daemon.py`** — a launchd daemon that witnesses every play: who
  started it (you, the DJ, or autoplay), how much of it actually played,
  and a verdict (completed / partial / abandoned / unobservable). Never
  blocks the player; SIGKILL-safe appends.
- **`mcp/music-mcp.py`** — 15 MCP tools over that store: live state, cycle
  detection, dormant re-picks, honest catalog search/queue with exact-ID
  preflight (explicit versions ranked first), a journaled celebration/mood
  override, and verbatim reaction logging. Tools answer `known: false`
  with a reason instead of guessing.
- **`skills/dj-claude/SKILL.md`** — the DJ contract. `/dj-claude` in any
  session and Claude runs the room: keeps the queue ~3 deep, reads the
  session, drops a context-perfect song when a moment earns it.
- **`dj/test_gates.py`** — 38 ground-truth gates covering the attribution
  classifier's hard cases (restarts, sub-poll tracks, append-vs-replace
  queues, false-manual protection). Store-dependent gates skip on a fresh
  install.

## Install

```bash
./install.sh          # copies files, installs the daemon, registers the MCP
```

Then, in Music.app's world, two things the installer cannot do for you:

1. **Turn OFF Autoplay** (the ∞ icon in Playing Next). With it on, Music's
   own radio outranks the DJ's playlist queue and the picks never advance.
   This is a UI-only toggle; there is no API for it.
2. **Create two Shortcuts** (Shortcuts.app), named exactly:
   - `DJ Claude Queue Next` — accepts text input → *Get Music* (search by
     the input) → *Add to Up Next* (play next).
   - `DJ Claude Play Catalog` — accepts text input → *Get Music* → *Play
     Music*.
   The MCP preflights every dispatch against Apple's search API so the
   Shortcut's first result is verified to be the exact requested track_id
   before anything is sent — ambiguous queries are refused, never guessed.

Optional but recommended: System Settings → Privacy & Security → Full Disk
Access for the daemon's python3. Unlocks ~4 weeks of true per-play backfill
from knowledgeC, including plays from other apps.

## Honest limits (by design, stated rather than hidden)

- Native Playing Next is append-only and write-only: the DJ can add but
  never read or remove. Vetoes of already-dispatched catalog picks require
  interception at play time.
- Tracks shorter than the daemon's poll interval are recorded as
  `unobservable` by inference from queue position — the daemon never claims
  to have watched something it couldn't sample.
- Lifetime counters are treated as a partial sample of one platform. If an
  Apple "Replay" playlist exists, it is ingested as ground truth and
  outranks them.

## Uninstall

```bash
python3 ~/.claude/dj/daemon.py uninstall
claude mcp remove music
rm -rf ~/.claude/dj ~/.claude/mcp/music-mcp.py ~/.claude/skills/dj-claude
```

## What's in the store

Everything lives in `~/.claude/dj/` — an SQLite store plus append-only
jsonl feeds (scrobbles, DJ command journal, counter snapshots). Every fact
a tool emits carries provenance: `reconstructed` (inferred from today's
library) vs `accumulated` (witnessed by the daemon) vs `inferred` (derived,
labeled as such). Your reactions are stored verbatim and surface as
evidence for the DJ to weigh — never as automatic filters.
