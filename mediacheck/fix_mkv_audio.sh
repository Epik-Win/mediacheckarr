#!/bin/bash
# Replaces remux_mkv_to_mp4.sh (now in retired/).
#
# Background: `Taken (2008)` (and others) wouldn't stream in Plex. HEVC
# 10-bit MKV with EAC3 5.1(side) audio - Plex's audio transcoder can't
# handle that layout and fails with `Cannot group in blocks of 6!`, which
# stalls the whole session, video included.
#
# The old script "fixed" it by remuxing to MP4 with ffmpeg's default stream
# selection, which keeps one video and one audio track and nothing else.
# PGS subtitles can't go in MP4 at all. Every file it touched lost all its
# subtitles (Euphoria S03E08: 41 tracks -> 0) and any extra audio (Toy
# Story 2: 3 audio + 53 subtitle tracks -> 1 + 0).
#
# This version stays MKV and keeps every track. It *adds* an AAC copy of the
# main audio track as the first, default track, so Plex has a safe track to
# direct-play or transcode, while the original EAC3/DTS/TrueHD tracks stay
# available for devices that can pass them through. Video, the original
# audio, subtitles and attachments are all stream-copied.
#
# The new file replaces the original only after quick stream and duration
# checks pass. The original backup is removed after the installed file also
# passes the lightweight post-install checks; checkrr handles deep decoding.
#
# Last line of output is always "RESULT: ok|fixed|failed - <why>".
# Exit: 0 = ok or fixed, 1 = failed (original left untouched)

FILE="$1"
MARK="AAC (Plex-compatible)"

result() { echo "RESULT: $1 - $2"; [ "$1" = failed ] && exit 1; exit 0; }

case "${FILE,,}" in
    *.mkv) ;;
    *) result ok "not an MKV" ;;
esac

probe() { ffprobe -v error "$@" -of default=noprint_wrappers=1:nokey=1 "$FILE"; }
VIDEO_CODEC=$(probe -select_streams V:0 -show_entries stream=codec_name)
PIX_FMT=$(probe -select_streams V:0 -show_entries stream=pix_fmt)
AUDIO_CODEC=$(probe -select_streams a:0 -show_entries stream=codec_name)
AUDIO_CHANNELS=$(probe -select_streams a:0 -show_entries stream=channels)
AUDIO_LAYOUT=$(probe -select_streams a:0 -show_entries stream=channel_layout)
AUDIO_CODECS=$(probe -select_streams a -show_entries stream=codec_name | tr '\n' ' ')

echo "[MKV Audio] $FILE"
echo "[MKV Audio] video: $VIDEO_CODEC ($PIX_FMT), main audio: $AUDIO_CODEC ($AUDIO_LAYOUT)"

case "$PIX_FMT" in
    *10le|*10be|*12le|*12be) DEEP=1 ;;
    *) DEEP=0 ;;
esac
[ "$VIDEO_CODEC" = hevc ] && [ $DEEP = 1 ] || result ok "not HEVC 10-bit+"
[ "$AUDIO_CODEC" = eac3 ] && [ "$AUDIO_LAYOUT" = '5.1(side)' ] \
    || result ok "not the known EAC3 5.1(side) playback issue"
case " $AUDIO_CODECS" in
    *" aac "*) result ok "already has an AAC track" ;;
esac

SIZE=$(stat -c%s "$FILE")
FREE=$(df --output=avail -B1 "$(dirname "$FILE")" | tail -1)
[ "$FREE" -gt $((SIZE + SIZE / 10)) ] || result failed "not enough free space ($FREE bytes free, need ~$SIZE)"
[ ! -e "$FILE.bak" ] || result failed "an existing backup must be resolved before fixing this file"

CH=${AUDIO_CHANNELS:-6}
[ "$CH" -gt 8 ] && CH=8   # ffmpeg's AAC encoder tops out at 7.1

TMP="$(dirname "$FILE")/.$(basename "$FILE").audiofix.tmp"
rm -f "$TMP"
# Output stream order: video, NEW aac (from a:0), then every original audio
# track, subtitles, attachments (fonts for styled subtitles).
if ! nice -n 10 ffmpeg -nostdin -hide_banner -loglevel error -y -i "$FILE" \
        -map 0:V -map 0:a:0 -map 0:a -map "0:s?" -map "0:t?" \
        -c copy -c:a:0 aac -b:a:0 640k -ac:a:0 "$CH" \
        -metadata:s:a:0 title="$MARK" \
        -disposition:a 0 -disposition:a:0 default \
        -map_metadata 0 -map_chapters 0 -f matroska "$TMP"; then
    rm -f "$TMP"
    result failed "ffmpeg remux failed"
fi

count() { ffprobe -v error -select_streams "$2" -show_entries stream=index -of csv=p=0 "$1" | wc -l; }
dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }
for s in V s t; do
    if [ "$(count "$FILE" $s)" != "$(count "$TMP" $s)" ]; then
        rm -f "$TMP"; result failed "stream count mismatch for '$s' tracks"
    fi
done
if [ "$(count "$TMP" a)" != $(( $(count "$FILE" a) + 1 )) ]; then
    rm -f "$TMP"; result failed "audio track count mismatch"
fi
if ! awk -v a="$(dur "$FILE")" -v b="$(dur "$TMP")" 'BEGIN{d=a-b; if(d<0)d=-d; exit !(a>0 && d<2)}'; then
    rm -f "$TMP"; result failed "duration mismatch"
fi

touch -r "$FILE" "$TMP"
if ! mv "$FILE" "$FILE.bak"; then
    rm -f "$TMP"
    result failed "could not preserve the original file"
fi
if ! mv "$TMP" "$FILE"; then
    mv "$FILE.bak" "$FILE" || result failed "install failed; original remains at $FILE.bak"
    result failed "could not install the verified replacement; original restored"
fi

NEW_AUDIO_CODEC=$(probe -select_streams a:0 -show_entries stream=codec_name)
NEW_AUDIO_DEFAULT=$(probe -select_streams a:0 -show_entries stream_disposition=default)
if ! ffprobe -v error "$FILE" >/dev/null 2>&1 ||
        [ "$(count "$FILE" V)" != "$(count "$FILE.bak" V)" ] ||
        [ "$(count "$FILE" s)" != "$(count "$FILE.bak" s)" ] ||
        [ "$(count "$FILE" t)" != "$(count "$FILE.bak" t)" ] ||
        [ "$(count "$FILE" a)" != $(( $(count "$FILE.bak" a) + 1 )) ] ||
        [ "$NEW_AUDIO_CODEC" != "aac" ] || [ "$NEW_AUDIO_DEFAULT" != "1" ] ||
        ! awk -v a="$(dur "$FILE.bak")" -v b="$(dur "$FILE")" \
            'BEGIN{d=a-b; if(d<0)d=-d; exit !(a>0 && d<2)}'; then
    rm -f "$FILE"
    mv "$FILE.bak" "$FILE" || result failed "post-install verification failed; original remains at $FILE.bak"
    result failed "post-install verification failed; original restored"
fi

if ! rm "$FILE.bak"; then
    result failed "replacement passed quick checks, but could not remove the original backup"
fi
result fixed "added $CH-channel AAC track ahead of EAC3 5.1(side) (all original tracks kept; quick checks passed)"
