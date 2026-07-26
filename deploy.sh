#!/usr/bin/env bash
#
# Deploy the remote script to every place Live might read it from, and say
# plainly whether Live needs restarting.
#
# Live reads a remote script exactly once, at startup. Copying the file over a
# running Live changes nothing until Live is restarted — which is how this repo
# repeatedly ended up "discovering" Live API limitations that were really just
# an old script answering the question.
#
# Live 12 loads user remote scripts from the User Library. The older
# ~/Library/Preferences/Ableton/<ver>/User Remote Scripts/ path is kept in sync
# too, because Live 9/10 used it and installs that have been upgraded in place
# sometimes still carry it.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO/AbletonMCP_Remote_Script/__init__.py"

[ -f "$SRC" ] || { echo "no remote script at $SRC" >&2; exit 1; }

BUILD_ID="$(sed -n 's/^BUILD_ID *= *["'"'"']\(.*\)["'"'"']/\1/p' "$SRC" | head -1)"
SRC_SUM="$(shasum "$SRC" | cut -d' ' -f1)"

echo "Deploying build ${BUILD_ID:-<unstamped>} ($(wc -c < "$SRC" | tr -d ' ') bytes)"
echo

targets=()
# Live 12's real location.
targets+=("$HOME/Music/Ableton/User Library/Remote Scripts/AbletonMCP")
# Legacy per-version locations, only where the folder already exists.
while IFS= read -r d; do
  [ -n "$d" ] && targets+=("$d/AbletonMCP")
done < <(find "$HOME/Library/Preferences/Ableton" -maxdepth 2 -type d \
           -name "User Remote Scripts" 2>/dev/null || true)

updated=0
for dest in "${targets[@]}"; do
  mkdir -p "$dest"
  if [ -f "$dest/__init__.py" ] &&
     [ "$(shasum "$dest/__init__.py" | cut -d' ' -f1)" = "$SRC_SUM" ]; then
    echo "  = $dest"
  else
    cp "$SRC" "$dest/__init__.py"
    echo "  > $dest"
    updated=$((updated + 1))
  fi
done

echo
if [ "$updated" -eq 0 ]; then
  echo "Everything already current. Nothing to restart."
else
  echo "$updated location(s) updated."
  echo
  echo "RESTART ABLETON LIVE — it will not pick this up otherwise."
  echo "Then run get_build_info to confirm all three copies agree."
fi
