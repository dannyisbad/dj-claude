#!/usr/bin/env python3
"""DJ Claude daemon — the per-play witness Apple Music refuses to be.

Two event sources, best available wins:
- NOTIFY mode (primary): the compiled djnotify helper streams Music's
  com.apple.Music.playerInfo distributed notifications — no Apple events, no
  TCC, works under launchd (where Apple events to Music hang on this box).
  End position is heard-wallclock, which is what skip semantics need.
- POLL mode (fallback): osascript probe every few seconds, richer (true
  player position + playlist context) but only live where Apple events work.

Each closed play is one JSON line in scrobbles.jsonl with an initiation
class (manual | dj_queue | autoplay_run | autoplay_falloff | unknown)
derived from structure: frozen-queue prediction, the command journal,
mid-flight cuts, and library membership. Never blocks the player; every
persistence is append+flush; SIGKILL-safe by construction.

Install:   python3 ~/.claude/dj/daemon.py install
Uninstall: python3 ~/.claude/dj/daemon.py uninstall
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import djlib

STATE_FILE = djlib.DJ_DIR / "daemon_state.json"
ERR_LOG = djlib.DJ_DIR / "daemon.err.log"
HELPER = djlib.DJ_DIR / "djnotify"
PLIST = Path.home() / "Library" / "LaunchAgents" / "com.djclaude.daemon.plist"
LABEL = "com.djclaude.daemon"
SNAPSHOT_EVERY_S = 20 * 3600
JOURNAL_WINDOW_S = 12   # a journaled DJ command this recent explains a change
IDLE_GAP_S = 60         # silence this long means the next start is from idle

PROBE = '''
set FS to ASCII character 31
tell application "Music"
  if it is not running then return "off"
  set pstate to player state as text
  if pstate is "stopped" then return "stopped"
  set ctx to ""
  try
    set ctx to name of current playlist
  end try
  set shuf to shuffle enabled as text
  set ppos to "0"
  try
    set ppos to (player position) as text
  end try
  try
    set t to current track
    return pstate & FS & (persistent ID of t) & FS & (name of t) & FS & ¬
      (artist of t) & FS & (album of t) & FS & ((duration of t) as text) ¬
      & FS & ppos & FS & ctx & FS & shuf
  on error
    return pstate & FS & "" & FS & "" & FS & "" & FS & "" & FS & "0" & FS ¬
      & ppos & FS & ctx & FS & shuf
  end try
end tell
'''


def log_err(msg: str) -> None:
    with ERR_LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


_probe_ok_once = False


def probe():
    # until a probe has ever succeeded, allow 90s: an Automation consent
    # prompt may be awaiting an answer and an early kill would dismiss it
    global _probe_ok_once
    try:
        out = subprocess.run(["osascript", "-e", PROBE], capture_output=True,
                             text=True, timeout=15 if _probe_ok_once else 90)
    except subprocess.TimeoutExpired:
        return {"error": "osascript timeout — Apple events unreachable in "
                "this context (launchd), or Automation consent missing. "
                "The notify helper does not need them."}
    except OSError as exc:
        return {"error": str(exc)}
    if out.returncode != 0:
        return {"error": out.stderr.strip()[:200]}
    _probe_ok_once = True
    raw = out.stdout.rstrip("\n")
    if raw in ("off", "stopped"):
        return {"state": raw}
    p = raw.split("\x1f")
    if len(p) != 9:
        return {"error": f"bad probe shape ({len(p)} fields)"}
    try:
        return {"state": p[0], "pid": p[1], "name": p[2], "artist": p[3],
                "album": p[4], "duration": float(p[5] or 0),
                "position": float(p[6] or 0), "context": p[7],
                "shuffle": p[8] == "true"}
    except ValueError:
        return {"error": "unparseable numbers in probe"}


def append(path: Path, obj: dict) -> None:
    djlib.DJ_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def pid_hex(v) -> str:
    return format(int(v) & 0xFFFFFFFFFFFFFFFF, "016X") if v is not None else ""


class Daemon:
    def __init__(self):
        self.current = None      # open play (dict) or None
        self.last_pos = 0.0      # poll mode: furthest position heard
        self.last_ctx = ""
        self.last_activity = 0.0
        self.library_pids = set()
        self.lib_loaded_at = 0.0
        self.cmd_offset = 0
        self.recent_cmds = []
        self.queue_pids = []     # frozen DJ queue from the last queue_set
        self.catalog_queue = []  # ordered native Playing Next refs
        self.osa_usable = None   # learned; guards snapshot attempts
        self.state = self._load_state()
        self.th = dict(djlib.THRESHOLDS)

    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        STATE_FILE.write_text(json.dumps(self.state))

    def refresh_library(self) -> None:
        try:
            conn = djlib.db()
            self.library_pids = {r["pid"] for r in
                                 conn.execute("SELECT pid FROM tracks")}
            self.th = djlib.thresholds(conn)
            conn.close()
            self.lib_loaded_at = time.time()
        except Exception as exc:
            log_err(f"library refresh: {exc}")

    def in_library(self, pid: str) -> bool:
        # cache-only on purpose: a live osascript check would hang under
        # launchd; the cache refreshes hourly from the store
        return bool(pid) and pid in self.library_pids

    def read_commands(self) -> None:
        """Tail the MCP's command journal so DJ-issued transport commands
        are never mistaken for the user's hand."""
        try:
            if not djlib.COMMANDS.exists():
                return
            size = djlib.COMMANDS.stat().st_size
            if size < self.cmd_offset:
                self.cmd_offset = 0
            if size == self.cmd_offset:
                return
            with djlib.COMMANDS.open("rb") as f:
                f.seek(self.cmd_offset)
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break
                    self.cmd_offset += len(raw)
                    try:
                        line = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self.recent_cmds.append(line)
                    if line.get("cmd") == "queue_set":
                        self.queue_pids = (line.get("arg") or {}).get("pids", [])
                        self.catalog_queue = []
                    elif line.get("cmd") == "catalog_queue":
                        # A stale journal entry is not proof that Music still
                        # holds the native queue after restarts/manual edits.
                        if time.time() - float(line.get("at", 0)) < 12 * 3600:
                            # Native Playing Next APPENDS, so the tracker must
                            # too: assigning here dropped the still-pending
                            # picks of an earlier dispatch and credited them to
                            # autoplay when they played.
                            self.catalog_queue += [
                                {"name": str(p.get("name", "")),
                                 "artist": str(p.get("artist", ""))}
                                for p in (line.get("arg") or {}).get("picks", [])
                                if p.get("name") and p.get("artist")]
                            self.catalog_queue = self.catalog_queue[-25:]
                    elif line.get("cmd") == "stop":
                        self.catalog_queue = []
            self.recent_cmds = self.recent_cmds[-50:]
        except OSError as exc:
            log_err(f"command journal: {exc}")

    def journaled_cmd_near(self, at: float):
        for c in reversed(self.recent_cmds):
            if abs(at - c.get("at", 0)) <= JOURNAL_WINDOW_S and \
                    c.get("cmd") in ("queue_set", "play", "play_moment",
                                     "catalog_play", "catalog_queue",
                                     "next_track", "play_playlist", "stop"):
                return c
        return None

    def predicted_next(self, prev_pid: str):
        if prev_pid in self.queue_pids:
            i = self.queue_pids.index(prev_pid)
            if i + 1 < len(self.queue_pids):
                return self.queue_pids[i + 1]
        return None

    def skipped_between(self, prev_pid: str, now_pid: str):
        """Frozen-queue pids strictly between prev and now, or None when the
        pair is not a forward move inside the queue. A track shorter than the
        poll leaves no observation, so without this the queue predictor
        desyncs and bills the NEXT pick as unattributed."""
        try:
            i = self.queue_pids.index(prev_pid)
            j = self.queue_pids.index(now_pid)
        except ValueError:
            return None
        return self.queue_pids[i + 1:j] if j > i else None

    def record_inferred(self, pids, prev_name: str, now_name: str) -> None:
        """Write the passed-over tracks as labeled inference, never as
        observation: the queue advanced through them, so they occupied the
        timeline, but no poll ever saw them and the fraction is unknown."""
        if not pids:
            return
        try:
            conn = djlib.db()
            for pid in pids:
                r = conn.execute("""SELECT name,artist,album,duration_s
                                    FROM tracks WHERE pid=?""", (pid,)).fetchone()
                if not r:
                    continue
                append(djlib.SCROBBLES, {
                    "type": "play", "pid": pid, "name": r["name"],
                    "artist": r["artist"], "album": r["album"],
                    "started_at": round(time.time(), 1),
                    "ended_s": r["duration_s"] or 0,
                    "duration_s": r["duration_s"] or 0, "fraction": None,
                    "verdict": "unobservable", "initiation": "dj_queue",
                    "initiation_evidence": (
                        f"inferred from frozen queue position: ran between "
                        f"'{prev_name}' and '{now_name}', shorter than the "
                        f"{self.th['poll_s']}s poll so no sample exists"),
                    "in_library": 1,
                    "context_playlist": djlib.DJ_PLAYLIST})
            conn.close()
        except Exception as exc:
            log_err(f"inferred queue rows: {exc}")

    def classify(self, now: dict, prev, prev_frac, from_idle: bool):
        """Initiation of the play that just began. Structure only."""
        t = time.time()
        cmd = self.journaled_cmd_near(t)
        if cmd:
            return "dj_queue", (f"journaled DJ command '{cmd['cmd']}' "
                                f"{round(t - cmd['at'], 1)}s ago")
        now_name = str(now.get("name", "")).casefold()
        now_artist = str(now.get("artist", "")).casefold()
        for index, queued in enumerate(self.catalog_queue):
            if queued["name"].casefold() == now_name and \
                    queued["artist"].casefold() == now_artist:
                # If Music/user drifted past an earlier planned item, consume
                # through the item that actually started; never leave stale
                # refs to misattribute a later manual pick.
                self.catalog_queue = self.catalog_queue[index + 1:]
                return "dj_queue", ("matched journaled native catalog queue "
                                    f"at position {index + 1}")
        on_queue = (now.get("context") == djlib.DJ_PLAYLIST
                    or (now.get("context") is None and bool(self.queue_pids)))
        if prev and on_queue:
            passed = self.skipped_between(prev["pid"], now["pid"])
            if passed == []:
                return "dj_queue", "predicted by frozen queue (next in DJ playlist)"
            if passed:
                return "dj_queue", (f"frozen queue advanced past {len(passed)} "
                                    "track(s) too short for the poll to witness")
        in_lib = self.in_library(now["pid"])
        if from_idle or prev is None:
            # A restart leaves no prev, so the frozen-queue check above cannot
            # fire and a queue advance would be credited to the user's hand. Refuse to
            # guess: a false manual pick corrupts the one signal that matters.
            if prev is None and now["pid"] in self.queue_pids:
                return "unknown", ("no continuity (daemon started mid-queue) "
                                   "and the track is in the frozen DJ queue — "
                                   "a queue advance and a user pick are "
                                   "indistinguishable here")
            if in_lib:
                return "manual", "playback started from idle, no journaled command"
            return "unknown", "started from idle on a non-library track"
        if prev_frac is not None and prev_frac < self.th["complete_at"]:
            return "manual", (f"previous track cut mid-flight at "
                              f"{round(prev_frac * 100)}% — unpredicted change")
        if now.get("context") is not None and now["context"] != self.last_ctx:
            return "manual", (f"context flip '{self.last_ctx or 'none'}' -> "
                              f"'{now['context'] or 'none'}'")
        if not in_lib:
            return "autoplay_falloff", ("non-library pid after a completed "
                                        "track, no journaled command")
        if prev and now["artist"] == prev["artist"]:
            return "autoplay_run", ("same-artist continuation after a "
                                    "completed track, no command")
        return "unknown", "post-completion change matching no structural rule"

    def close_play(self, heard_s: float) -> None:
        c = self.current
        if not c:
            return
        dur = c["duration"] or 0
        frac = min(heard_s / dur, 1.0) if dur else 0.0
        append(djlib.SCROBBLES, {
            "type": "play", "pid": c["pid"] or None, "name": c["name"],
            "artist": c["artist"], "album": c["album"],
            "started_at": round(c["started_at"], 1),
            "ended_s": round(heard_s, 1), "duration_s": round(dur, 1),
            "fraction": round(frac, 3),
            "verdict": djlib.verdict_for(frac, self.th, dur),
            "initiation": c["initiation"],
            "initiation_evidence": c["evidence"],
            "in_library": 1 if c["in_lib"] else 0,
            "context_playlist": c.get("context") or None})
        self.last_activity = time.time()
        self.current = None

    def open_play(self, now: dict, prev, prev_frac, from_idle: bool) -> None:
        initiation, evidence = self.classify(now, prev, prev_frac, from_idle)
        if prev and initiation == "dj_queue":
            self.record_inferred(self.skipped_between(prev["pid"], now["pid"])
                                 or [], prev.get("name", "?"), now.get("name", "?"))
        self.current = dict(now, started_at=time.time(),
                            initiation=initiation, evidence=evidence,
                            in_lib=self.in_library(now["pid"]))

    def housekeeping(self) -> None:
        self.read_commands()
        if time.time() - self.lib_loaded_at > 3600:
            self.refresh_library()
        t = time.time()
        # History.dat is TCC-blocked from this launchd context on this box.
        # Notify mode therefore never tries it; the MCP refreshes the daily
        # snapshot from the foreground agent context. Poll mode may try only
        # after it has positively proved Apple-event access.
        if self.osa_usable and \
                t - self.state.get("last_history_snapshot", 0) > SNAPSHOT_EVERY_S:
            try:
                items = djlib.parse_history_dat()
                append(djlib.HISTORY_FEED, {
                    "type": "history_snapshot", "taken_at": t,
                    "items": [[i["rank"], i["name"], i["pid"], i["store_id"],
                               i["kind"]] for i in items]})
                self.state["last_history_snapshot"] = t
                self._save_state()
            except Exception as exc:
                log_err(f"history snapshot: {exc}")
        # counter snapshots need Apple events; only attempt where they work
        # (poll mode). In notify mode the MCP takes them from agent context.
        if self.osa_usable and \
                t - self.state.get("last_counter_snapshot", 0) > SNAPSHOT_EVERY_S:
            try:
                rows = djlib.scan_library()
                append(djlib.SNAPSHOTS_FEED, {
                    "type": "counter_snapshot", "taken_at": t,
                    "rows": [[r["pid"], r["play_count"], r["skip_count"],
                              r["last_played"], r["last_skipped"]]
                             for r in rows]})
                self.state["last_counter_snapshot"] = t
                self._save_state()
            except Exception as exc:
                log_err(f"counter snapshot: {exc}")

    def heartbeat(self, status: str) -> None:
        try:
            djlib.HEARTBEAT.write_text(json.dumps(
                {"at": time.time(), "status": status, "pid": os.getpid()}))
        except OSError:
            pass

    # ------------------------------------------------------- notify mode

    def run_notify(self) -> None:
        q = queue.Queue()

        def reader(proc):
            for line in proc.stdout:
                try:
                    q.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
            q.put(None)  # helper exited

        proc = subprocess.Popen([str(HELPER)], stdout=subprocess.PIPE,
                                text=True)
        threading.Thread(target=reader, args=(proc,), daemon=True).start()
        self.heartbeat("notify mode: waiting for player activity")
        heard = 0.0
        playing_since = None
        restarts = 0
        while True:
            try:
                note = q.get(timeout=self.th.get("poll_s", 5))
            except queue.Empty:
                self.housekeeping()
                if self.current:
                    live = heard + (time.time() - playing_since
                                    if playing_since else 0)
                    self.heartbeat(f"notify: {self.current['name']} "
                                   f"({round(live)}s heard)")
                else:
                    self.heartbeat("notify: idle")
                continue
            if note is None:
                restarts += 1
                if restarts > 5:
                    log_err("helper died 5x; falling back to poll mode")
                    return self.run_poll()
                time.sleep(2)
                proc = subprocess.Popen([str(HELPER)], stdout=subprocess.PIPE,
                                        text=True)
                threading.Thread(target=reader, args=(proc,),
                                 daemon=True).start()
                continue
            at = note.get("at", time.time())
            state = note.get("Player State", "")
            # "PersistentID" is the TRACK id (matches AppleScript persistent
            # ID); "Library PersistentID" is the whole library's — one value
            # for every track, verified against History.dat's DBID prefix
            pid = pid_hex(note.get("PersistentID"))
            now = {"pid": pid, "name": note.get("Name", ""),
                   "artist": note.get("Artist", ""),
                   "album": note.get("Album", ""),
                   "duration": (note.get("Total Time") or 0) / 1000.0,
                   "context": None}
            if state == "Stopped" or not now["name"]:
                if self.current:
                    if playing_since:
                        heard += at - playing_since
                    self.close_play(heard)
                heard, playing_since = 0.0, None
                continue
            key = (now["pid"], now["name"], now["artist"])
            cur = self.current
            if cur and key != (cur["pid"], cur["name"], cur["artist"]):
                if playing_since:
                    heard += at - playing_since
                dur = cur["duration"] or 0
                prev_frac = (heard / dur) if dur else None
                prev = {"pid": cur["pid"], "name": cur["name"],
                        "artist": cur["artist"]}
                self.close_play(heard)
                heard, playing_since = 0.0, None
                self.open_play(now, prev, prev_frac, False)
            elif cur is None:
                from_idle = (time.time() - self.last_activity) > IDLE_GAP_S
                self.read_commands()
                self.open_play(now, None, None, from_idle)
            if state == "Playing":
                if playing_since is None:
                    playing_since = at
            else:  # Paused
                if playing_since is not None:
                    heard += at - playing_since
                    playing_since = None
            self.last_activity = time.time()

    # --------------------------------------------------------- poll mode

    def run_poll(self) -> None:
        poll_s = self.th.get("poll_s", 5)
        was_idle = True
        fail_streak = 0
        while True:
            self.read_commands()
            now = probe()
            if "error" in now:
                self.heartbeat(f"probe error: {now['error']}")
                if self.current:
                    self.close_play(self.last_pos)
                was_idle = True
                # killed-mid-event osascripts wedge Music's event queue for
                # every client; back way off instead of hammering
                fail_streak += 1
                time.sleep(min(120 * fail_streak, 600)
                           if "timeout" in str(now["error"]) else poll_s)
                continue
            fail_streak = 0
            self.osa_usable = True
            if now["state"] in ("off", "stopped"):
                self.heartbeat(now["state"])
                if self.current:
                    self.close_play(self.last_pos)
                was_idle = True
                time.sleep(poll_s)
                continue
            self.heartbeat(f"{now['state']}: {now['name']}")
            key = (now["pid"], now["name"], now["artist"])
            cur = self.current
            if cur is None:
                self.open_play(now, None, None, was_idle)
                self.last_pos = now["position"]
            elif key != (cur["pid"], cur["name"], cur["artist"]):
                prev = {"pid": cur["pid"], "name": cur["name"],
                        "artist": cur["artist"]}
                dur = cur["duration"] or 0
                prev_frac = (self.last_pos / dur) if dur else None
                self.close_play(self.last_pos)
                self.open_play(now, prev, prev_frac, False)
                self.last_pos = now["position"]
            else:
                # same track: keep the furthest position heard, so a scrub
                # back or in-track restart still closes as one play
                self.last_pos = max(self.last_pos, now["position"])
                self.current["context"] = now["context"]
            self.last_ctx = now["context"]
            was_idle = False
            self.housekeeping()
            time.sleep(poll_s)

    def run(self) -> None:
        append(djlib.SCROBBLES, {"type": "daemon_start", "at": time.time()})
        self.refresh_library()
        if HELPER.exists():
            self.run_notify()
        else:
            log_err("djnotify helper missing; poll mode only")
            self.run_poll()


PLIST_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>{Path(__file__).resolve()}</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardErrorPath</key><string>{ERR_LOG}</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""


def install() -> None:
    if not HELPER.exists():
        src = Path(__file__).parent / "djnotify.swift"
        try:
            out = subprocess.run(["swiftc", "-O", str(src), "-o", str(HELPER)],
                                 capture_output=True, text=True, timeout=300)
            if out.returncode != 0:
                print(f"helper compile failed (poll mode only): "
                      f"{out.stderr.strip()[:200]}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"no swiftc ({exc}); daemon will run poll mode only")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(PLIST_XML)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True)
    out = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}",
                          str(PLIST)], capture_output=True, text=True)
    if out.returncode != 0:
        print(f"launchctl bootstrap failed: {out.stderr.strip()}")
        sys.exit(1)
    print(f"installed + started: {PLIST}")
    print(f"event source: {'notify helper' if HELPER.exists() else 'poll'}")


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True)
    if PLIST.exists():
        PLIST.unlink()
    print(f"stopped and removed {PLIST}. Store and logs left in {djlib.DJ_DIR} "
          "(delete that directory to remove all accumulated data).")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "install":
        install()
    elif mode == "uninstall":
        uninstall()
    elif mode == "run":
        try:
            Daemon().run()
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            log_err(f"fatal: {exc!r}")
            raise
    else:
        print(__doc__)
