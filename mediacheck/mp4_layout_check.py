#!/usr/bin/env python3
"""Detect MP4/MOV files whose tracks aren't interleaved - i.e. some track's
data (usually subtitles) is stored gigabytes away from the video it plays
alongside.

Background: `Tuner (2026) WEBDL-2160p.mp4` stuttered at Original quality on
the Bravia TV. It decoded perfectly (so checkrr never flagged it) and had
Plex-safe codecs (so the MKV remux never touched it). But all 6 subtitle
tracks had >99% of their data packed into the last ~3 MB of a 20 GB file.
With subtitles on, the TV had to jump between its play position and the end
of the file every ~0.5s - a fresh TLS connection, a disk seek across the
whole JBOD platter, and several MB downloaded to use a few bytes. That
starves the playback buffer. A plain `ffmpeg -c copy` remux interleaves it
properly and fixes it losslessly.

This only reads the file's index (the `moov` box, a few MB), never the media
data, so it takes well under a second even on a 50 GB remux.

Usage: mp4_layout_check.py <file>
Exit:  0 = OK, 1 = badly interleaved (needs remux), 2 = couldn't parse
"""
import bisect
import struct
import sys

# A track counts as "far" at some moment if its data sits further from the
# video being played at that same moment than this many seconds of the
# file's average bitrate (and never less than MIN_FAR_BYTES). Normal
# interleaving keeps tracks within ~1s of each other.
FAR_SECONDS = 30
MIN_FAR_BYTES = 32 * 1024 * 1024
# Flag a track once this share of its chunks is far away. A few stragglers
# (e.g. a final subtitle cue written after the last video chunk) are normal.
FAR_FRACTION = 0.05
# Tracks with this few chunks are read once up front (chapter titles) - a
# single seek, not a problem during playback.
MIN_CHUNKS = 3


def iter_boxes(buf, start=0, end=None):
    end = len(buf) if end is None else end
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[i:i + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr:
            return
        yield kind.decode("latin1"), buf[i + hdr:i + size]
        i += size


def child(buf, kind):
    for k, body in iter_boxes(buf):
        if k == kind:
            return body
    return None


def read_top_level(path):
    """Return (list of (kind, offset, size), moov body without its header, file size)."""
    tops, moov = [], None
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        pos = 0
        while pos + 8 <= file_size:
            f.seek(pos)
            hdr = f.read(16)
            size, kind = struct.unpack(">I4s", hdr[:8])
            kind = kind.decode("latin1")
            if size == 1:
                size = struct.unpack(">Q", hdr[8:16])[0]
            elif size == 0:
                size = file_size - pos
            if size < 8:
                break
            tops.append((kind, pos, size))
            if kind == "moov":
                f.seek(pos)
                raw = f.read(size)
                moov = raw[16:] if struct.unpack(">I", raw[:4])[0] == 1 else raw[8:]
            pos += size
    return tops, moov, file_size


def chunk_table(trak):
    """Return (handler, [(start_seconds, byte_offset), ...]) for a track."""
    mdia = child(trak, "mdia")
    hdlr = child(mdia, "hdlr")
    handler = hdlr[8:12].decode("latin1")
    mdhd = child(mdia, "mdhd")
    timescale = struct.unpack(">I", mdhd[20:24] if mdhd[0] == 1 else mdhd[12:16])[0] or 1
    stbl = child(child(mdia, "minf"), "stbl")

    co = child(stbl, "co64")
    if co is not None:
        n = struct.unpack(">I", co[4:8])[0]
        offsets = struct.unpack(">%dQ" % n, co[8:8 + 8 * n])
    else:
        co = child(stbl, "stco")
        n = struct.unpack(">I", co[4:8])[0]
        offsets = struct.unpack(">%dI" % n, co[8:8 + 4 * n])

    # samples per chunk (stsc): runs of (first_chunk, samples_per_chunk)
    stsc = child(stbl, "stsc")
    m = struct.unpack(">I", stsc[4:8])[0]
    runs = [struct.unpack(">III", stsc[8 + 12 * i:20 + 12 * i])[:2] for i in range(m)]
    # sample durations (stts): runs of (count, delta)
    stts = child(stbl, "stts")
    k = struct.unpack(">I", stts[4:8])[0]
    deltas = [struct.unpack(">II", stts[8 + 8 * i:16 + 8 * i]) for i in range(k)]

    # Single pass over chunks: a pointer into the stsc runs gives each chunk's
    # sample count, and a pointer into the stts runs advances the clock by
    # whole runs at a time rather than sample by sample.
    times = []
    t = 0
    r = 0
    d_idx, d_left = 0, deltas[0][0] if deltas else 0
    for c in range(n):
        times.append(t / timescale)
        while r + 1 < len(runs) and runs[r + 1][0] - 1 <= c:
            r += 1
        need = runs[r][1] if runs else 1
        while need and d_idx < len(deltas):
            if d_left == 0:
                d_idx += 1
                if d_idx >= len(deltas):
                    break
                d_left = deltas[d_idx][0]
                continue
            step = min(need, d_left)
            t += step * deltas[d_idx][1]
            d_left -= step
            need -= step
    return handler, list(zip(times, offsets))


def main(path):
    tops, moov, file_size = read_top_level(path)
    if moov is None:
        print(f"ERROR: no moov box found in {path}")
        return 2

    tracks = []
    for kind, body in iter_boxes(moov):
        if kind == "trak":
            try:
                tracks.append(chunk_table(body))
            except Exception as e:  # malformed/unusual track: skip it, don't fail the file
                print(f"  (skipping unreadable track: {e})")

    videos = [t for t in tracks if t[0] == "vide" and t[1]]
    if not videos:
        print("ERROR: no video track")
        return 2
    ref = max(videos, key=lambda t: len(t[1]))[1]
    ref_times = [c[0] for c in ref]
    duration = ref_times[-1] or 1
    far_bytes = max(MIN_FAR_BYTES, int(file_size / duration * FAR_SECONDS))

    moov_pos = next(p for k, p, _ in tops if k == "moov")
    mdat_pos = next((p for k, p, _ in tops if k == "mdat"), 0)
    moov_at_end = moov_pos > mdat_pos
    print(f"moov at {'end' if moov_at_end else 'start'} of file")

    bad = []
    for handler, chunks in tracks:
        if chunks is ref or len(chunks) < MIN_CHUNKS:
            continue
        far = 0
        worst = 0
        for t, off in chunks:
            i = min(bisect.bisect_right(ref_times, t) - 1, len(ref) - 1)
            gap = abs(off - ref[max(i, 0)][1])
            worst = max(worst, gap)
            if gap > far_bytes:
                far += 1
        frac = far / len(chunks)
        status = "BAD" if frac > FAR_FRACTION else "ok"
        print(f"  {handler} track: {len(chunks)} chunks, {frac:.0%} stored far from the video "
              f"(worst {worst / 1e9:.2f} GB away) - {status}")
        if status == "BAD":
            bad.append(handler)

    if bad:
        print(f"BAD INTERLEAVE: {', '.join(bad)} track data is stored away from the video - "
              f"players must seek back and forth across the file during playback")
        return 1
    print("OK - tracks are interleaved")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: mp4_layout_check.py <file>")
        sys.exit(2)
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as e:
        print(f"ERROR: could not parse {sys.argv[1]}: {e}")
        sys.exit(2)
