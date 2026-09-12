#!/usr/bin/env bash
#
# Copy the remote-script package and distinguish installed from loaded code.
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
SOURCE_DIR="$REPO/AbletonMCP_Remote_Script"

[ -f "$SRC" ] || { echo "no remote script at $SRC" >&2; exit 1; }

BUILD_ID="$(sed -n 's/^BUILD_ID *= *["'"'"']\(.*\)["'"'"']/\1/p' "$SRC" | head -1)"

echo "Deploying build ${BUILD_ID:-<unstamped>} ($(wc -c < "$SRC" | tr -d ' ') bytes)"
echo

targets=()
if [ -n "${ABLETON_MCP_REMOTE_SCRIPT_DIR:-}" ]; then
  # Custom User Library locations, and isolated installation verification.
  targets+=("$ABLETON_MCP_REMOTE_SCRIPT_DIR")
else
  targets+=("$HOME/Music/Ableton/User Library/Remote Scripts/AbletonMCP")
  # Legacy per-version locations, only where the folder already exists.
  while IFS= read -r d; do
    [ -n "$d" ] && targets+=("$d/AbletonMCP")
  done < <(find "$HOME/Library/Preferences/Ableton" -maxdepth 2 -type d \
             -name "User Remote Scripts" 2>/dev/null || true)
fi

updated=0
for dest in "${targets[@]}"; do
  mkdir -p "$dest"
  location_changed=0
  for source_file in "$SOURCE_DIR"/*.py; do
    installed_file="$dest/$(basename "$source_file")"
    if ! [ -f "$installed_file" ] || ! cmp -s "$source_file" "$installed_file"; then
      cp "$source_file" "$installed_file"
      location_changed=1
    fi
    cmp -s "$source_file" "$installed_file" || {
      echo "Installed file verification failed: $installed_file" >&2
      exit 1
    }
  done
  if [ "$location_changed" -eq 1 ]; then
    echo "  > Installed and verified: $dest"
    updated=$((updated + 1))
  else
    echo "  = Installed files already match: $dest"
  fi
done

echo
echo "$updated remote-script location(s) updated; all package .py files verified."
echo "Loaded Ableton and MCP server versions were not checked by this copy operation."
echo "Restart Ableton Live and the MCP server in your host to load changed code."
echo
echo "Then run get_build_info to compare the running build, protocol, and file hashes."
