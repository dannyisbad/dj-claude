# DJ Claude — System Design

Status: implemented; this document is the product contract. Supersedes taste.json (which violates the binding
constraints: hand-written blocklist, hand-written cycles, shallow aggregates).
Evaluation set: GROUND_TRUTH.md — a build that cannot reproduce those four
labeled episodes is not done. Quality bar: REQUIREMENTS.md.

Binding constraints restated: library read-only except the DJ-owned queue
playlist; the only network surface is Apple's public iTunes Search API for
exact catalog identity (never taste inference); stdlib python3 + osascript +
one built-in macOS Shortcut for native catalog queue insertion; TCC-gated
sources degrade, never fail; structural signals only — no cue words, no name
blocklists.

---

## 1. The signal model

Ground rule: every inference below names its evidence and its failure mode.
Where the evidence cannot carry the inference, the feature is cut and the
tool says "unknown" instead — the user spots fakes (Episode 3).

### 1.1 Two epochs of evidence

Everything divides on the day the daemon first runs:

- **Reconstructed past** — AppleScript snapshot: lifetime playedCount /
  skippedCount, ONE last-played and ONE last-skipped date per track,
  dateAdded, favorited. Cloud-synced, so it aggregates the user's whole multi-device
  history — coarsely. Plus History.dat's 200-deep ordered recents (no
  timestamps, strict order) and RecentSearches.json (timestamped manual
  intent).
- **Accumulated future** — the daemon's per-play ledger: start time, end
  position, session context, initiation class. Optionally seeded ~4 weeks
  back by knowledgeC.db if Full Disk Access is granted.

Every fact in every tool response carries which epoch it came from
(`provenance: reconstructed | accumulated | counter_delta | knowledgec |
history_order | owner_verbatim | inferred`). No number appears without its
basis.

### 1.2 The inferences, one by one

**Dormancy** ("the user played this two months ago — would the user want it back?")
- Evidence: last-played date (reconstructed, one point), playedCount,
  favorited, and the cycle the track stranded in (1.3). Post-daemon: exact
  per-track timelines.
- Honest form today: "83-play *All for Leyna*, favorited cohort, last played
  156 days ago, stranded in the 2026-02 Billy Joel stratum." That is real and
  useful.
- Failure modes: (a) last-played can be an autoplay touch — Lizzo's
  last-played keeps advancing while genuinely loved tracks sit dormant, so
  dormancy candidates must pass the contamination check first; (b) lifetime
  counts span years — a 47-play track may be 2023 taste. Mitigation: dormancy
  responses always pair count with the stratum date; the agent weighs, not us.

**Cycle membership** (past cycles)
- Evidence: the stranded-strata effect. Apple keeps only the LAST play date,
  so a finished obsession appears as an artist block frozen in its final
  month: 9 Beatles tracks at 2025-12, 7 Billy Joel at 2026-05, the 32-track
  Fleetwood Mac + Pink Floyd pileup at 2025-11. Detection: cluster tracks by
  final-play month, flag months where one artist/era holds ≥3 tracks or an
  outlier share.
- Failure modes: (a) a track played in the Beatles cycle AND again later
  vanishes from the Beatles stratum — strata systematically under-count
  cycles' overlap with evergreen tracks; (b) a cycle is only visible after it
  ENDS (nothing strands while still in rotation). Both are disclosed in
  responses (`method: "stranded_strata", bias: "undercounts tracks replayed
  since; blind to the active cycle"`). Forward events fix both.

**The active cycle / current mood**
- Evidence, post-daemon: intent-weighted plays over the trailing 2–6 weeks —
  manual picks and completed listens, clustered by artist/era. Evidence
  today: History.dat order + the most recent strata + live session.
- What is CUT: a single mood label. Episode 4 is binding — one sitting
  spanned Pearl Jam, Blind Melon, Chappell Roan, No Doubt, 2Pac. Any
  one-genre "current mood" is a fake. The tool reports the evidence (the
  actual recent manual picks, era spread, cycle age) and, when the picture is
  scattered — as July 2026 genuinely is (16 stranded tracks across 14
  artists) — it says `state: "between_obsessions_or_shuffle"` ratthe user's than
  inventing coherence.

**Autoplay contamination** (Episode 2, the CarPlay attack)
- Structural detector, snapshot tier (works today, no names anywhere):
  flag when ALL of — skip/play ratio is an outlier vs library median 0.47
  (About Damn Time: 92/46 = 2.0, worst in library); artist-isolated (only
  track by artist, no neighbouring artist/album plays — no way to have
  arrived there by taste adjacency); not favorited; last-played advances
  across snapshots while its count-cohort sits still. Bonus structural tell:
  after excluding blank-artist clips it is the alphabetically first song —
  recorded as evidence, not a rule.
- Event tier (daemon / knowledgeC): same track starting at session position
  0 across many sessions, no preceding manual action, frequently ended
  early. This is the sharp version — but note the hard limit below.
- HARD LIMIT, disclosed: CarPlay plays happen on the PHONE. The Mac daemon
  will never see those events; they arrive only as cloud-synced counter
  deltas. So the event-tier detector catches Mac-side autoplay, while
  phone-side contamination is caught by the snapshot tier: a counter delta
  on one track with no corresponding Mac session, repeatedly, with skip
  deltas riding along. The response for a contaminated track shows the
  evidence chain, never a verdict word like "blocked".
- Policy: DISCOUNT, never blocklist. Contaminated plays are excluded from
  intent-weighted counts but always present in raw counts, and every
  response that uses intent-weighting says so. The user's verbatim Lizzo
  quote lives in the annotations store as corroborating evidence the agent
  can read — it is never a filter input.

**Manual-pick weight** (the strongest signal in the system)
- Evidence (all verified on this box): the daemon knows the frozen queue it
  established, so any track change to an unpredicted track is user- or
  machine-initiated; context flip (`current playlist` name changes or
  vanishes) plus pid-present-in-library distinguishes a manual pick from
  autoplay fall-through (autoplay streams non-library pids with no
  location). A manual pick that starts a same-artist run marks track 1 as
  the vote; the run's tail is unlabeled (Episode 1 — one Chappell Roan vote,
  three artifacts).
- Failure modes: (a) a scripted `play` by any otthe user's agent is
  indistinguishable from the user's double-click — mitigated by the DJ contract
  (main agent only touches Music) and by the daemon journaling its OWN
  issued commands so it can subtract them; (b) pre-daemon history has no
  manual/auto distinction at all except RecentSearches (a timestamped search
  is the strongest reconstructed intent signal and is ingested as such).

**Skip semantics**
- Evidence, event tier only: end position vs duration at track change.
  ≥ ~85% of duration = done-with-it; < ~25% = rejection; between = partial
  (context decides). Thresholds are stored config, stated in responses, not
  buried.
- The reconstructed skippedCount is UNINTERPRETABLE per-event (Apple counts
  second-3 and second-200 skips identically) and co-occurs with love here
  (The Chain 51p/50s favorited). It is used for exactly one thing: the
  ratio-outlier term of the contamination detector. No tool ever presents a
  lifetime skip count as rejection.

**Adjacency** ("what fits next / what's near this")
- Evidence, accumulated: co-play — tracks/artists that share sessions with
  manual picks, weighted by completion. This is the only adjacency that
  survives Episode 3 (Sublime and Slowdive share an "Alternative" tag; one
  is loved, one is "too weird hippy" — tags cannot separate them).
- Evidence, reconstructed fallback: era (year is populated 296/329 and the user
  is 70s-first: 1,024 of 2,782 plays) + genre + shared-stratum membership.
  When the fallback is all we have, the response says `basis: "tag_and_era
  — co-play data insufficient (n sessions observed)"` and the agent treats
  it as a guess.
- CUT: BPM (0/329, dead), Genius.itdb (encrypted), any audio analysis
  (no dependencies). No fake "energy" axis; energy only ever enters as the
  agent's own judgment over named tracks.

**New-to-the user's picks**
- The user dislikes most new music. Taste candidates come from exactly
  two honest sources: (a) the library's unheard tail — 62 zero-play and 41
  one-play tracks the user ADDED (dateAdded is intent) but never wore in; (b)
  autoplay fall-through tracks (non-library pids) that the user conspicuously did
  NOT skip — a real, structural positive observed by the daemon.
- The tool returns few or none, each with its reason, and returning an
  empty list is a first-class outcome.

---

## 2. The store

Location: `~/.claude/dj/` — owned by the system, never touches the user's.

### 2.1 Why SQLite AND jsonl (each doing the one job it is good at)

- The daemon appends **jsonl** (`scrobbles.jsonl`, one line per closed play,
  plus a `commands.jsonl` journal of DJ-issued transport commands).
  Rationale: an append is crash-safe and dumb; a 10-second poller must never
  hold a database lock or corrupt state on SIGKILL. The jsonl is a raw feed,
  not a query surface.
- The MCP owns **SQLite** (`dj.sqlite3`, WAL). Every DJ question is an
  aggregation joined on track identity across time windows — dormancy is
  max(event time) per track × weighted counts; cycles are month × artist
  clusters; adjacency is session co-membership. Re-parsing jsonl per
  question is O(history) per call and grows forever; SQLite makes each
  question one indexed query. On each tool call (or a cheap mtime check) the
  MCP folds new jsonl lines into SQLite — ingestion is idempotent (unique on
  `(started_at, pid)`).

### 2.2 Schema (serves the queries, not the log)

```sql
tracks(pid TEXT PRIMARY KEY, name, artist, album, album_artist, sort_artist,
       genre, year, duration_s, date_added, kind,
       is_nonmusic INT,        -- structural: blank artist+album, local kind
       favorited INT, play_count INT, skip_count INT,   -- latest snapshot
       last_played, last_skipped)                        -- latest snapshot

snapshots(taken_at, pid, play_count, skip_count, last_played, last_skipped)
  -- full-library counter snapshot per daemon day; deltas between rows
  -- recover plays made on OTHER DEVICES (provenance: counter_delta)

events(id, pid, name, artist, album,          -- denormalized for non-library
       started_at, ended_at_s, duration_s, fraction,
       verdict TEXT,            -- completed | partial | abandoned
       initiation TEXT,         -- manual | dj_queue | autoplay_run
                                -- | autoplay_falloff | unknown
       session_id INT,
       source TEXT,             -- daemon | knowledgec | counter_delta
       in_library INT,          -- pid lookup result at event time
       context_playlist TEXT)   -- current-playlist name or NULL

sessions(id, started_at, ended_at, gap_before_s, first_initiation,
         device_hint TEXT)      -- 'mac' from daemon; 'other' for delta-only

strata(pid, stratum_month TEXT, computed_at)
  -- materialized stranded-strata assignment from the bootstrap snapshot;
  -- frozen (the bootstrap is a historical document, recomputed never)

history_snapshots(taken_at, rank INT, library_item_id, store_id, kind)
  -- History.dat order, artwork ignored; order-diffs => coarse events

searches(searched_at, raw_identifier, resolved_pid NULL)  -- RecentSearches

annotations(at, scope TEXT,     -- track | artist | pick_list | session
            ref TEXT, verbatim TEXT, valence TEXT, source TEXT)
  -- user reactions the agent witnessed, verbatim. Evidence, never filter.
  -- Binding: track-scope refs are canonical 'Name — Artist' and retrieval
  -- matches them exactly; wider scopes match the artist on word boundaries.
  -- Never substring-match track names: 'Time' must not inherit the Lizzo
  -- verbatim from 'About Damn Time'. remember_reaction canonicalises refs.

meta(key, value)  -- daemon_first_run, last_ingest, fda_status, thresholds
```

Derived views (not stored, recomputed cheap at 329 tracks):
`intent_weighted_plays` (events where initiation != autoplay_* and verdict
!= abandoned, unioned with discounted counter_deltas), `contamination`
(the detector of 1.2 as a query), `coplay` (session co-membership matrix).

### 2.3 Bootstrap and ongoing ingestion

Bootstrap, once: full AppleScript scan → `tracks` + first `snapshots` row +
`strata`; History.dat parse (identifiers + order only — stream-parse, skip
artwork; the 242 MB is mostly PNG) → `history_snapshots`;
RecentSearches → `searches`. If FDA is granted at any time: import
knowledgeC `/media/nowPlaying` rows (~4 weeks of true events, start+end)
into `events` with `source='knowledgec'`, deduped against daemon events by
overlapping time windows; re-import every run because Apple prunes it.

Ongoing: the daemon (launchd, 5–10 s poll, ~118 ms/probe = negligible)
closes plays into jsonl; nightly (or on wake) it takes a counter snapshot
and a History.dat order snapshot. Daemon downtime is recorded as a coverage
gap in `meta`; on restart, counter deltas across the gap are folded in as
coarse `counter_delta` events (count and last-date only — positions
unrecoverable, and marked so).

### 2.4 Degrade ladder (constraint: degrade, not fail)

| Tier | Needs | You get |
|---|---|---|
| 0 | osascript Automation consent (already granted) | snapshot inference: strata, dormancy, snapshot-tier contamination |
| 1 | + daemon running | true per-play events, skip semantics, manual-pick detection, session context, co-play adjacency — Mac-side only |
| 2 | + Full Disk Access for the daemon's host app | knowledgeC backfill (~4 weeks head start), foreground corroboration, Biome as future secondary |
| 3 | + optional swift helper (CLT, no packages) | playerInfo notifications: near-instant track-change wake instead of 5–10 s poll granularity |

`system_health` (below) reports the current tier and the exact grant that
moves up one tier, with what it unlocks — verbatim, in the response.

---

## 3. The query surface (the MCP tools)

Design rule from REQUIREMENTS.md: a tool call returns a judgment-ready
situation. Every response leads with a one-paragraph `summary` (prose, so a
low-effort agent still gets the situation), then structured fields, every
number with provenance, uncertainty as a field. No tool's output exists to
feed anotthe user's of our tools.

Seven taste read tools, exact catalog search, one write tool, and six control
tools.

### 3.1 `whats_happening_now()`

The moment: any wake — track about to end, nag event, session start.
Args: none.

Returns:
```json
{
  "summary": "Playing 'Black' — Pearl Jam, 2:41/5:43, the user's manual pick 12
    min ago (context flipped off the DJ queue). Session is 38 min old, 5
    tracks: 3 manual picks spanning grunge/90s-alt/current-pop, 1 DJ pick
    completed, 1 DJ pick skipped at 0:19. The user is driving this session.",
  "player": {"state": "playing", "pid": "89EBAD578EBBBA33",
             "position_s": 161.2, "duration_s": 343.0,
             "shuffle": false, "context_playlist": null},
  "current_track_initiation": {"class": "manual",
    "evidence": "unpredicted vs frozen queue + context flip",
    "provenance": "accumulated"},
  "session": {"started_at": "…", "gap_before_min": 214,
    "trace": [{"name": "No Rain", "artist": "Blind Melon",
               "initiation": "manual", "verdict": "completed"}, "…"],
    "manual_share": 0.6},
  "pause_is_sacred": true,
  "coverage": {"daemon": "live", "tier": 1}
}
```
If the daemon has been off, `current_track_initiation.class: "unknown"` with
the reason. Autoplay fall-through (non-library pid, no context) is called
out in the summary as urgent — it means the DJ queue ran dry — but ONLY
while playing: on a paused player the same fact is a plain note ending
"pause is sacred; observe, do not act". Urgency language is an invitation
to act and must never fire where acting is forbidden. The response also
carries `time_of_day` (local clock as fact; the learned hour-of-day fit is
`known: false` until daemon data can support one).

### 3.2 `current_cycle()`

The moment: choosing a direction, start of a DJ shift.
Args: `window_days` (default 42).

Returns (worked, real, as of 2026-07-29 — tier 0/1 early):
```json
{
  "summary": "No dominant obsession right now. July's stranded tracks
    scatter across 14 artists (Chingy, Aerosmith, Alice In Chains, Aqua…),
    unlike May's clear Billy Joel block (7 tracks). This morning's manual
    picks span Pearl Jam, Blind Melon, Chappell Roan, No Doubt, 2Pac —
    wide era spread, 90s-heavy. Read: between obsessions or shuffle-heavy;
    do not pin a single mood on this.",
  "state": "between_obsessions_or_shuffle",
  "recent_manual_picks": [{"artist": "Pearl Jam", "name": "Black",
      "at": "2026-07-29T08:40", "provenance": "accumulated"}, "…"],
  "trailing_strata": [
    {"month": "2026-05", "cluster": "Billy Joel", "tracks": 7,
     "provenance": "reconstructed",
     "bias": "strata only show ENDED cycles; blind to an active one"},
    {"month": "2026-07", "cluster": null, "tracks": 16, "artists": 14}],
  "era_distribution_of_intent_plays": {"1990s": 0.4, "1970s": 0.2, "…": 0},
  "known": true,
  "caveat": "co-play/energy structure needs more daemon days (3 observed)"
}
```
When the picture IS coherent (a 2025-11-style Fleetwood-Mac/Floyd block),
`state: "active_cycle"` with the cluster, its age, and whetthe user's intent-plays
in it are rising or fading week-over-week. `state` has exactly four values:
`active_cycle | forming_or_ambiguous | between_obsessions_or_shuffle |
unknown`. The gate is dominance SHARE of the recent tail plus an absolute
play floor (thresholds in meta, echoed in the caveat) — never an
artist-count cap, which lets one stray fifth artist erase a forming
obsession. `forming_or_ambiguous` exists because obsessions have a middle:
real concentration that has not settled yet must be reported as a lean
with its counter-evidence, not flattened into "between obsessions".

Two honesty rules bound this tool's evidence:
- Trailing strata cover CONTIGUOUS months; a month with no stranded tracks
  appears as an explicit `known: false` entry distinguishing "per-play
  events observed, stratum evaporated by replays" from "no coverage —
  cannot distinguish silence from replays". Never a silent gap.
- History.dat order is undated. Every response using it carries
  `recent_play_order_age`: when the snapshot was taken, that items bear no
  timestamps, and a freshness bound (the tail can be no fresthe user's than the
  newest last-played among its tracks, capped by the snapshot time).
  Undisclosed staleness is fake freshness.

### 3.3 `dormant_loves(min_days=45, max_days=None, limit=10)`

THE "the user played this two months ago" tool. `min_days` alone favors the
longest-dormant epics; `max_days` closes a dormancy BAND so the headline
question is directly askable ("loved ~2 months ago" = 45–120), surfacing
that cycle's tracks instead of burying them under year-old ones. The
unbanded response says which shape it is in and how to band.

Returns:
```json
{
  "summary": "59 tracks with 15+ plays untouched 60+ days. Top of the pool:
    'All for Leyna' (Billy Joel) 83 plays — the library's #1 — silent 156
    days, from the Feb-2026 Billy Joel stratum; 'Karma Police' 56 plays,
    100 days; 'Landslide' 40 plays AND favorited, 194 days. All three pass
    the contamination check. Counts are lifetime/cloud-synced — pair each
    with its stratum date before trusting intensity.",
  "pool_size": 59,
  "candidates": [
    {"pid": "…", "name": "All for Leyna", "artist": "Billy Joel",
     "plays": {"raw": 83, "intent_weighted": null,
       "note": "pre-daemon; raw lifetime count, cloud-synced"},
     "favorited": false, "dormant_days": 156,
     "stratum": "2026-02", "stratum_cluster": "Billy Joel",
     "contamination": {"flagged": false,
       "checks": "skip_ratio 0.3 vs median 0.47; artist has 12 tracks"},
     "provenance": "reconstructed"},
    {"…": "Karma Police 56p/100d"}, {"…": "Landslide 40p fav/194d"}],
  "excluded": [{"name": "About Damn Time", "artist": "Lizzo",
     "reason": "contamination flagged: skip/play 2.0 (library worst),
       artist-isolated, not favorited, last-played advances while cohort
       dormant", "owner_annotation": "IGNORE LIZZO THAT IS EVIL THAT IS MY
       CAR …", "shown_because": "you should see what was discounted"}]
}
```
Note the shape: the discounted track is SHOWN with its evidence and the
user's verbatim, not silently dropped — requirement 4 (machine plays
marked, never silently counted) and requirement 3 (negative evidence).

### 3.4 `judge_track(query)` — args: `pid` or `artist` + `name`

The moment: the agent is considering a specific candidate.

Returns the user's full relationship with the track: raw + intent-weighted plays,
favorited, skip picture WITH the caveat ("lifetime skips do not encode
position; The-Chain-class tracks are loved at 51p/50s"), stratum/cycle
membership, dormancy, contamination check, any annotation verbatim, recent
event trail if daemon-era, and `in_library`. For a non-library track
(autoplay observation) it returns the fall-through evidence instead. No
verdict field — the last line of `summary` states the tensions, e.g.
"Strawberry Fields: 24p/13s and a user note 'complicated relationship' —
plays alone overstate the welcome."

### 3.5 `adjacent_to(anchor)` — args: `pid`/`artist`+`name`, or `"session"`

Returns co-play neighbours (sessions shared with manual picks, completion-
weighted) when tier-1 data suffices, else era+genre+stratum fallback with
`basis` stated:
```json
{"basis": "tag_and_era — only 3 daemon days of co-play observed",
 "neighbours": [{"artist": "Sublime", "why": "shared 2026-05 stratum with
   anchor; era-adjacent (90s); user verbatim 'doin time is good tho'",
   "provenance": "reconstructed+owner_verbatim"}],
 "warning": "tag adjacency failed before: shoegaze picks tagged
   'alternative' were rejected ('too weird hippy'). Weigh groove/energy
   yourself — the store has no energy axis and will not fake one."}
```
A session anchor that resolves to an out-of-library track is its own
answer, not "nothing playing": the response names the track and player
state, states the true reason (`current track not in library`), and
surfaces what the daemon HAS observed of it (play count, mean fraction,
initiations) — the same evidence judge_track computes for that pid.

### 3.6 `untouched_but_promising(limit=5)`

New-to-the-user, honestly sourced: the 62 zero-play / 41 one-play library tracks
(the user added them — dateAdded is intent) filtered to the active cycle's
era/genre neighbourhood, plus any autoplay fall-through tracks the user let play
≥85% (structural positive). Each with its reason; empty list is a normal
answer and the description says so.

### 3.7 `system_health()`

Tier, daemon uptime/gaps, event count, coverage window ("events since
2026-07-29; before that: strata only"). Diagnoses must not outrun their
evidence: "Music-blocked (Automation consent)" requires a live heartbeat
whose status IS a probe error; a missing, stale, or unreadable heartbeat
is `state_unknown` with its own instruction (check daemon.err.log) —
never a consent claim that sends the operator to re-grant something
already granted. FDA status with the exact
instruction ("System Settings → Privacy & Security → Full Disk Access →
enable for Terminal; unlocks ~4 weeks of true per-play backfill, real skip
semantics on that window, foreground corroboration"), ingest staleness,
threshold values in force.

### 3.8 `remember_reaction(scope, ref, verbatim, valence)` — the one write

The agent witnesses reactions the sensors cannot ("too weird hippy", "doin
time is good tho"). This appends to `annotations`, verbatim, with source
`owner_verbatim`. It is the ONLY write tool into the intelligence store,
and annotations are surfaced as evidence in otthe user's responses — never used
as automatic filters.

### 3.9 Control tools (bound to verified semantics)

`queue_set(pids[])` — rebuilds the DJ-owned playlist ("DJ Claude") and
`play`s the PLAYLIST OBJECT (never a track of it), after forcing shuffle
off with a settle delay; journals the command. `queue_status()` — frozen
queue vs reality, flags fall-through. `stop_gracefully()`. Mid-flight
appends are rejected with the reason (frozen-queue semantics: re-play
happens only at a track boundary, handled by the daemon). Pause is sacred:
control tools refuse to act while state is `paused` and say why.

`catalog_search(query, artist?)` resolves stable Apple catalog IDs and returns
an opaque `catalog_ref`; it is identity lookup, not taste ranking.
`catalog_play(catalog_ref, owner_requested, reason)` handles an explicit
immediate user request through the native `DJ Claude Play Catalog` Shortcut,
then requires exact live name+artist readback. `catalog_queue(picks[])`
preflights each exact metadata
query so Apple's first result has the requested `track_id`, then invokes the
built-in `DJ Claude Queue Next` Shortcut to append to native Playing Next.
That queue is not AppleScript-readable; accepted dispatch is reported as such,
and the daemon confirms the journaled name+artist when playback reaches it.

`play_moment(kind, reason, pid|name+artist|catalog_ref)` is the explicit exception for
the already-promised once-per-session mood veto and a verified major-task
completion celebration. The caller owns the agent-session boundary (MCP has
no trustworthy session identifier); the store surfaces the last spend. The
tool refuses paused/unreachable players unless the user explicitly
pre-authorized that exact play, requires a reason, resolves a real library
track or catalog ref, journals before playback, and verifies live readback.
Routine test greens, partial progress, unverified reports, and subagent
completions do not qualify.

---

## 4. The agent's role

The system is a witness with a good memory. The agent is the DJ.

The system must NOT:
- **Rank or score candidates.** No `score`, no `match_pct`, no sorted
  "recommendations". `dormant_loves` orders by dormancy×plays for
  presentation and says so; it is a pool, not a verdict.
- **Auto-pick or auto-queue.** Nothing plays without an agent decision. The
  daemon only re-plays the playlist the agent already set, at boundaries.
- **Hard-block anything.** Contamination discounts and discloses. If the user
  one day manually picks About Damn Time, that manual event outweighs the
  flag — and the data model already handles it (initiation=manual).
- **Fake confidence.** `known: false` + reason is a complete answer.
- **Override taste with statistics.** User verbatims are quoted to the
  agent, not compiled into rules.

Where judgment enters: reading Episode-4-style spread as a vibe; deciding
whetthe user's a dormant favorite fits THIS moment; the groove/energy axis the
store honestly lacks; spending the once-per-session mood veto; the risk
call on any new-music pick; and interpreting `partial` verdicts, which are
context, not signal.

---

## 5. Adversarial self-review

**Overfit to one big number.** 83 plays of All for Leyna is a lifetime,
multi-device, multi-year aggregate — it proves 2023-era love as easily as
current love. Mitigation: counts never travel without last-played and
stratum; `intent_weighted` is null pre-daemon ratthe user's than pretending.
Residual risk (disclosed): the reconstructed epoch simply cannot rank
"how hard the user loved it in its cycle" — only that the user did and when it ended.

**The obviously wrong pick.** Failure path: contamination misses a
phone-side autoplay track and `dormant_loves` resurfaces it. The detector's
AND-of-four is deliberately conservative, and the CarPlay case is caught by
three independent legs (ratio outlier, artist isolation, advancing
last-played). But the snapshot tier WILL miss an autoplay track by an
artist the user also loves (not artist-isolated). Disclosed in the detector's
own output; only the event tier fixes it, and only on the Mac. Phone-side
per-event truth is structurally unreachable on this box — stated in
`system_health`, not papered over.

**Chappell trap inversion.** Discounting run-tails (Episode 1) risks
under-crediting a real binge the user chose to let run. Mitigation: run-tails
are `unlabeled`, not negative; a track the user manually RETURNS to later earns
its own vote. The first-track-is-the-vote rule can still misfire when the user
manually picks track 3 of a run — the queue-prediction check catches that
(any unpredicted change is manual), so only same-artist AUTOPLAY tails are
discounted.

**Cold library / fresh box.** Strata need last-played dates; with none,
every tool returns `known: false` with "bootstrap has nothing to
reconstruct; daemon accumulating since <date>". No fabricated cycles.

**A day the user plays nothing.** `whats_happening_now` reports idle + hours
since last session; `current_cycle` does not decay into "unknown" from one
quiet day (window is 42 days). Nothing infers mood from silence.

**Daemon off for a week.** Gap recorded; counter deltas recover that week
as coarse `counter_delta` events (which track, how many, last date — no
positions, no order). Responses that lean on the gap window carry
`coverage: "counter_delta only for 07-30..08-06"`. The failure NOT fixable:
manual-vs-auto is unknowable for the gap; those plays get half-weight
nothing — they stay raw-only.

**CarPlay logs 40 plays overnight.** They arrive as a counter delta with no
Mac session, same track, skip deltas alongside — exactly the snapshot-tier
signature; flagged before they can enter any intent-weighted number. If it
is a NEW track (no history), one night creates an instant ratio outlier
with artist isolation — caught, but disclosed as "flagged on 1 day of
evidence, confidence low".

**Concurrent scripting.** Otthe user's agents drove Music during archaeology and
will again. Any non-journaled scripted `play` reads as a manual pick —
poisoning the strongest signal. Mitigations: DJ contract (main agent only),
the commands journal, and `whats_happening_now` surfacing anomalies
("3 'manual' picks in 90 s at 2 a.m." reads wrong to any competent agent).
Not fully fixable; disclosed.

**History.dat cost.** 242 MB per parse is real; parse is streamed
(SAX-style, artwork skipped), snapshot cadence daily, initiated by the
foreground MCP only when stale. It never runs from launchd, where the file is
TCC-blocked, and therefore cannot retry on every daemon poll.

**Threshold arbitrariness.** 85%/25% verdict cuts, 42-day windows, ≥3-track
strata clusters — all config in `meta`, all echoed in responses that use
them, so the agent (and the user) can see and challenge the knobs rather
than discover them.

**The evaluation gate.** Before this design is called built: Episode 1 must
yield one manual vote + three unlabeled; Episode 2's track must be flagged
by structure with zero name-matching anywhere in the code; Episode 3's
verbatims must surface in `adjacent_to`/`judge_track` for those artists;
Episode 4's session must produce `between_obsessions_or_shuffle` or an
explicit multi-cluster answer — never a single-genre mood.

---

## Open questions (for the user / next phase)

1. **Swift notification helper** — build the tiny CLT-compiled listener
   (tier 3) now, or ship poll-only first? Poll at 5–10 s loses up to one
   poll interval of position precision at track boundaries.
2. **knowledgeC dedup** — when FDA lands mid-life, knowledgeC and daemon
   events overlap; proposed rule is "daemon wins inside its uptime windows,
   knowledgeC fills gaps". Accept?
3. **taste.json retirement** — migrate its user-authored facts (verbatims,
   loved_new) into `annotations` and delete the blocklist/cycles sections
   (now structural). Needs user sign-off since it is the user's words.
4. **Phone-side blindness** — per-event truth for CarPlay/phone plays is
   unreachable locally (no iOS backups on box, no network). Counter-delta
   inference is the ceiling. Is that acceptable, or is an iPhone backup
   worth creating periodically to mine (would change the constraint set)?
5. **Threshold tuning** — 85% done / 25% rejected / 42-day cycle window are
   defensible defaults with n=1 user; revisit after 2 weeks of daemon data
   against the user's actual behavior.
