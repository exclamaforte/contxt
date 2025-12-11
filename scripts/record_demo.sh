#!/usr/bin/env bash
set -euo pipefail

# Record an asciinema session of contxt and optionally convert it to a GIF.
# Usage: scripts/record_demo.sh [cast_path] [gif_path]
# Defaults: records/contxt-demo.cast and assets/contxt-demo.gif

CAST_PATH=${1:-records/contxt-demo.cast}
GIF_PATH=${2:-assets/contxt-demo.gif}
RECORD_CMD=${CONTXT_RECORD_COMMAND:-contxt}

mkdir -p "$(dirname "$CAST_PATH")"
mkdir -p "$(dirname "$GIF_PATH")"

if ! command -v asciinema >/dev/null 2>&1; then
    echo "ERROR: asciinema is required. Install with 'pip install asciinema' or visit https://asciinema.org/ for packages."
    exit 1
fi

echo "Starting asciinema recording. Output will be saved to $CAST_PATH."
echo "Your contxt session will launch with command: $RECORD_CMD"
echo "Press Ctrl-D when you're done recording."
asciinema rec -c "$RECORD_CMD" "$CAST_PATH"

if command -v agg >/dev/null 2>&1; then
    FONT_SIZE=${AGG_FONT_SIZE:-18}
    THEME=${AGG_THEME:-solarized-dark}
    SPEED=${AGG_SPEED:-1}
    echo "Converting $CAST_PATH to GIF via agg..."
    agg --font-size "$FONT_SIZE" --theme "$THEME" --speed "$SPEED" "$CAST_PATH" "$GIF_PATH"
    echo "GIF saved at $GIF_PATH"
else
    cat <<EOF
Skipping GIF conversion because 'agg' was not found.
Install it (https://github.com/asciinema/agg) and run:
  agg --font-size 18 --theme solarized-dark --speed 1 $CAST_PATH $GIF_PATH
EOF
fi
