#!/bin/bash
# Fixes an MP4/MOV whose tracks aren't interleaved (see mp4_layout_check.py
# for the Tuner (2026) story). A pure stream-copy remux - no re-encoding, no
# quality loss - because ffmpeg's muxer interleaves every track by time and
# +faststart moves the index to the front.
#
# The original stays next to the fixed file as <name>.mp4.bak until the
# installed replacement passes its layout, stream, and duration checks;
# checkrr handles deep decoding separately.
#
# Last line of output is always "RESULT: ok|fixed|failed - <why>".
# Exit: 0 = ok or fixed, 1 = failed (original left untouched)

FILE="$1"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

result() { echo "RESULT: $1 - $2"; [ "$1" = failed ] && exit 1; exit 0; }

echo "[Interleave Fix] Processing: $FILE"

"$DIR/mp4_layout_check.py" "$FILE"
case $? in
    0) result ok "tracks are interleaved" ;;
    1) ;;
    *) result failed "could not read the MP4 index" ;;
esac

SIZE=$(stat -c%s "$FILE")
FREE=$(df --output=avail -B1 "$(dirname "$FILE")" | tail -1)
if [ "$FREE" -lt $((SIZE + SIZE / 10)) ]; then
    result failed "not enough free space ($FREE bytes free, need ~$SIZE)"
fi
[ ! -e "$FILE.bak" ] || result failed "an existing backup must be resolved before fixing this file"

# Hidden + .tmp so Plex/Radarr/Sonarr never pick up a half-written file.
TMP="$(dirname "$FILE")/.$(basename "$FILE").interleave.tmp"
rm -f "$TMP"

# 0:V (capital) = real video only, not embedded cover art. Every audio and
# subtitle track is kept; chapters and metadata come across too.
if ! nice -n 10 ffmpeg -nostdin -hide_banner -loglevel error -y -i "$FILE" \
        -map 0:V -map "0:a?" -map "0:s?" -c copy \
        -map_metadata 0 -map_chapters 0 -movflags +faststart \
        -f mp4 "$TMP"; then
    rm -f "$TMP"
    result failed "ffmpeg remux failed"
fi

# Verify before swapping anything.
count() { ffprobe -v error -select_streams "$2" -show_entries stream=index -of csv=p=0 "$1" | wc -l; }
dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }
for s in V a s; do
    if [ "$(count "$FILE" $s)" != "$(count "$TMP" $s)" ]; then
        rm -f "$TMP"
        result failed "stream count mismatch for '$s' tracks"
    fi
done
if ! awk -v a="$(dur "$FILE")" -v b="$(dur "$TMP")" 'BEGIN{d=a-b; if(d<0)d=-d; exit !(a>0 && d<2)}'; then
    rm -f "$TMP"
    result failed "duration mismatch"
fi
if ! "$DIR/mp4_layout_check.py" "$TMP" >/dev/null; then
    rm -f "$TMP"
    result failed "remuxed file still fails the layout check"
fi

touch -r "$FILE" "$TMP"   # keep the date Radarr/Sonarr/Plex already know
if ! mv "$FILE" "$FILE.bak"; then
    rm -f "$TMP"
    result failed "could not preserve the original file"
fi
if ! mv "$TMP" "$FILE"; then
    mv "$FILE.bak" "$FILE" || result failed "install failed; original remains at $FILE.bak"
    result failed "could not install the verified replacement; original restored"
fi

if ! ffprobe -v error "$FILE" >/dev/null 2>&1 ||
        [ "$(count "$FILE" V)" != "$(count "$FILE.bak" V)" ] ||
        [ "$(count "$FILE" a)" != "$(count "$FILE.bak" a)" ] ||
        [ "$(count "$FILE" s)" != "$(count "$FILE.bak" s)" ] ||
        ! awk -v a="$(dur "$FILE.bak")" -v b="$(dur "$FILE")" \
            'BEGIN{d=a-b; if(d<0)d=-d; exit !(a>0 && d<2)}' ||
        ! "$DIR/mp4_layout_check.py" "$FILE" >/dev/null; then
    rm -f "$FILE"
    mv "$FILE.bak" "$FILE" || result failed "post-install verification failed; original remains at $FILE.bak"
    result failed "post-install verification failed; original restored"
fi

if ! rm "$FILE.bak"; then
    result failed "replacement passed quick checks, but could not remove the original backup"
fi
result fixed "remuxed so every track is interleaved with the video (quick checks passed)"
