#!/bin/bash
# DJ Claude installer — copies the witness + MCP + skill into place.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== DJ Claude install =="

mkdir -p ~/.claude/dj ~/.claude/mcp ~/.claude/skills/dj-claude
cp "$HERE"/dj/*.py "$HERE"/dj/DESIGN.md ~/.claude/dj/
cp "$HERE"/dj/djnotify.swift ~/.claude/dj/
cp "$HERE"/mcp/music-mcp.py ~/.claude/mcp/
chmod +x ~/.claude/mcp/music-mcp.py ~/.claude/dj/daemon.py
cp "$HERE"/skills/dj-claude/SKILL.md ~/.claude/skills/dj-claude/

# notify helper: stream Music's playerInfo notifications without Apple events
if command -v swiftc >/dev/null 2>&1; then
  echo "-- compiling djnotify helper"
  swiftc -O -o ~/.claude/dj/djnotify ~/.claude/dj/djnotify.swift
else
  echo "-- swiftc not found: daemon will use poll mode (works, less precise)"
fi

echo "-- registering MCP server (user scope)"
if command -v claude >/dev/null 2>&1; then
  claude mcp remove music --scope user >/dev/null 2>&1 || true
  claude mcp add music --scope user -- python3 ~/.claude/mcp/music-mcp.py
else
  echo "   claude CLI not on PATH; add manually:"
  echo "   claude mcp add music --scope user -- python3 ~/.claude/mcp/music-mcp.py"
fi

echo "-- initial library backfill (read-only scan of Music.app)"
python3 ~/.claude/dj/backfill.py || echo "   backfill skipped (is Music.app set up?)"

echo "-- installing witness daemon (launchd)"
python3 ~/.claude/dj/daemon.py install

echo "-- running ground-truth gates"
python3 ~/.claude/dj/test_gates.py | tail -1

cat <<'EOF'

Done. Two manual steps remain (see README):
 1. Music.app: turn OFF Autoplay (the infinity icon in Playing Next).
 2. Shortcuts.app: create 'DJ Claude Queue Next' and 'DJ Claude Play
    Catalog' (text input -> Get Music -> Add to Up Next / Play Music).

Then in Claude Code: /dj-claude
EOF
