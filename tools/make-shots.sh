#!/usr/bin/env bash
# Turn a folder of raw PNG screenshots into the WebP pairs the gallery expects, and print
# ready-to-paste <figure> markup with the real pixel dimensions filled in.
#
#   tools/make-shots.sh ~/Pictures/emulatte-shots
#
# Input files should already be named semantically, because the name becomes the URL:
#   desktop-library.png  couch-wall.png  couch-crt-gamepage.png  themes-systems.png
#
# Two traps this exists to avoid (both bit the gallery):
#   · width/height must come from the real file, never a guess
#   · replacing an image without renaming it does not reach anyone. The Pages CDN and the
#     browser keep serving the cached bytes. Rename, or the swap will look broken and not be.
set -euo pipefail

SRC="${1:-}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/assets/screens"

if [[ -z "$SRC" || ! -d "$SRC" ]]; then
  echo "usage: $0 <folder-of-pngs>" >&2
  exit 1
fi
command -v magick >/dev/null || { echo "ImageMagick (magick) not found" >&2; exit 1; }

mkdir -p "$OUT"
shopt -s nullglob
files=("$SRC"/*.png "$SRC"/*.PNG "$SRC"/*.jpg "$SRC"/*.jpeg)
(( ${#files[@]} )) || { echo "no images found in $SRC" >&2; exit 1; }

echo "Converting ${#files[@]} image(s) → $OUT"
echo

for f in "${files[@]}"; do
  name="$(basename "${f%.*}")"
  # full size for the lightbox, and a thumbnail for the grid
  magick "$f" -resize 1600x -strip -quality 82 -define webp:method=6 "$OUT/$name.webp"
  magick "$f" -resize 640x  -strip -quality 78 -define webp:method=6 "$OUT/$name-thumb.webp"
  # The trailing \n matters: without it `read` returns non-zero and `set -e` kills the script.
  read -r w h < <(magick identify -format '%w %h\n' "$OUT/$name-thumb.webp")
  printf '              <figure class="shot" data-full="assets/screens/%s.webp">\n' "$name"
  printf '                <img src="assets/screens/%s-thumb.webp" loading="lazy" width="%s" height="%s" alt="TODO describe this shot">\n' "$name" "$w" "$h"
  printf '                <figcaption>TODO caption</figcaption>\n'
  printf '              </figure>\n'
done

echo
echo "Done. Paste the markup above into emulatte.html, inside a .shots-group > .grid."
echo "A group holding a single shot needs class=\"grid solo\" or it stretches the full width."
du -sh "$OUT" | sed 's/^/total: /'
