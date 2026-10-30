#!/usr/bin/env bash
#
# build-css.sh — regenerate the vendored dashboard stylesheet.
#
# Compiles Tailwind (scanning templates/ for used classes) and concatenates
# the vendored @font-face declarations into assets/app.css, which is embedded
# into the smolofis-panel binary at compile time. Run after changing
# tailwind.config.js or adding/removing Tailwind classes in the template.
#
# Requires pnpm (or npx fallback). The woff2 files in assets/fonts/ are committed and only
# need refreshing if the font families change.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
DASHBOARD="$(dirname "${SCRIPT_DIR}")/src-dashboard"

log() { printf '\033[1;32m[build-css]\033[0m %s\n' "$*"; }

if command -v pnpm >/dev/null 2>&1; then
    RUNNER="pnpm dlx"
elif command -v npx >/dev/null 2>&1; then
    RUNNER="npx --yes"
else
    echo "pnpm (or npx) is required" >&2
    exit 1
fi

cd "${DASHBOARD}"

log "compiling tailwind (${RUNNER} templates/ scan, minified)"
${RUNNER} tailwindcss@3.4.17 \
    --config tailwind.config.js \
    --input assets/input.css \
    --output assets/tailwind.css \
    --minify

log "assembling assets/app.css (fonts + tailwind)"
cat assets/fonts.css assets/tailwind.css > assets/app.css

log "done: $(du -h assets/app.css | cut -f1) — rebuild the panel to embed it"
