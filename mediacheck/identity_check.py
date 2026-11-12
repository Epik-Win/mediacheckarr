#!/usr/bin/env python3
"""
Checks whether a media file's dialogue matches the exact episode or movie
its filename claims to be. Complements checkrr's corruption detection,
which only checks whether a file decodes cleanly.

Pipeline:
  1. Ask Sonarr/Radarr to parse the filename -> identifies title/season/
     episode.
  2. Extract up to 30 minutes of audio centered in the imported file
     (the whole file when shorter), then transcribe it with local Whisper.
  3. Compare the transcript with the corresponding timed section of an
     external subtitle for that exact movie or TV episode.

Subtitle references are fetched into memory only; no subtitle sidecars are
installed in the library. Subtitle providers are not able to guarantee that
a community subtitle is an official studio-published subtitle.

Usage: identity_check.py [--no-audio] <path-to-media-file>

  --no-audio  skip transcription and report UNVERIFIABLE (manual use only).

The last line printed is always "VERDICT: OK|MISMATCH|UNVERIFIABLE ..." and
the exit code matches it, so mediacheck can act on the result:
  0 = OK, 10 = MISMATCH, 20 = UNVERIFIABLE, 2 = error
"""
import fcntl
import html
from html.parser import HTMLParser
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
import io
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

import mclib
import yaml

# Resolve Radarr/Sonarr addresses and API keys from the shared Checkrr config,
# including each service's URL base.
_CONFIG = mclib.load_config()
_ARR = _CONFIG["arr"]
_IDENTITY = _CONFIG["identity"]
BAZARR_API = _IDENTITY["bazarr"]["url"].rstrip("/") + "/api"
SUBTITLECAT_HOST = "www.subtitlecat.com"
YIFY_HOST = "yifysubtitles.ch"
SUBTITLECAT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_SUBTITLE_PAGE_BYTES = 5 * 1024 * 1024
MAX_SUBTITLE_BYTES = 10 * 1024 * 1024

LOGFILE = mclib.IDENTITY_LOG


def log(msg):
    line = f"{msg}"
    print(line, flush=True)
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")


def http_json(url, data=None, headers=None, timeout=60):
    if data is not None:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers or {"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def identify(filename, is_tv):
    arr = _ARR["sonarr" if is_tv else "radarr"]
    q = urllib.parse.quote(filename)
    data = http_json(f"{arr['url']}/parse?title={q}&apikey={arr['key']}")

    if is_tv:
        series = data.get("series") or {}
        episodes = data.get("episodes") or []
        if not series or not episodes:
            return None
        ep = episodes[0]
        info = data["parsedEpisodeInfo"]
        # Sonarr carries the disambiguating suffix some series need ("(US)"),
        # but Wikipedia titles the articles without it.
        show = re.sub(r"\s*\((US|UK|\d{4})\)\s*$", "", series.get("title") or "")
        return {
            "label": f"{series.get('title')} - S{info['seasonNumber']:02d}"
                     f"E{info['episodeNumbers'][0]:02d} - {ep.get('title')}",
            "show": show,
            "episode_title": ep.get("title"),
            "series_id": series["id"],
            "episode_id": ep["id"],
            "season": info["seasonNumber"],
            "episode": info["episodeNumbers"][0],
            "episode_count": len(info["episodeNumbers"]),
            "genres": series.get("genres") or [],
            "original_language": (series.get("originalLanguage") or {}).get("name"),
            "is_tv": True,
        }
    else:
        movie = data.get("movie") or {}
        if not movie:
            return None
        movie_record = http_json(
            f"{arr['url']}/movie/{movie['id']}?apikey={arr['key']}"
        )
        movie_file = movie_record.get("movieFile") or {}
        return {
            "label": movie.get("title"),
            "show": movie.get("title"),
            "movie_id": movie["id"],
            "episode_title": None,
            "year": movie_record.get("year") or movie.get("year"),
            "runtime": movie_record.get("runtime") or movie.get("runtime"),
            "genres": movie.get("genres") or [],
            "scene_name": movie_file.get("sceneName") or os.path.splitext(filename)[0],
            "original_language": (movie_record.get("originalLanguage") or {}).get("name"),
            "is_tv": False,
        }


def bazarr_api(method, path, params=None, timeout=60, form=None):
    with open(_IDENTITY["bazarr"]["config_file"]) as f:
        api_key = ((yaml.safe_load(f) or {}).get("auth") or {}).get("apikey")
    query = urllib.parse.urlencode(params or [], doseq=True)
    url = f"{BAZARR_API}/{path.lstrip('/')}" + (f"?{query}" if query else "")
    data = urllib.parse.urlencode(form, doseq=True).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"X-API-KEY": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode()) if raw.strip() else None


# ISO 639-2 has two codes for some languages; files use either.
_ISO639_2_ALIASES = {
    "fre": "fra", "ger": "deu", "dut": "nld", "chi": "zho", "cze": "ces",
    "gre": "ell", "per": "fas", "rum": "ron", "slo": "slk", "alb": "sqi",
    "arm": "hye", "baq": "eus", "bur": "mya", "geo": "kat", "ice": "isl",
    "mac": "mkd", "mao": "mri", "may": "msa", "tib": "bod", "wel": "cym",
}


def language_by_name(name):
    """Bazarr's record for a language name from Radarr/Sonarr ("Turkish"):
    {"name", "code2", "code3"}, or None."""
    if not name:
        return None
    for lang in bazarr_api("GET", "system/languages") or []:
        if (lang.get("name") or "").lower() == name.lower():
            return {"name": lang["name"], "code2": lang["code2"], "code3": lang["code3"]}
    return None


def language_tags(lang):
    code3 = lang["code3"].lower()
    tags = {lang["code2"].lower(), code3}
    for b, t in _ISO639_2_ALIASES.items():
        if code3 in (b, t):
            tags |= {b, t}
    return tags


def validation_profile_name(lang):
    """The Bazarr profile used to search references in this language,
    created (and the language enabled in Bazarr) the first time it's needed."""
    settings = _IDENTITY["bazarr"]
    if lang["code2"] == "en":
        return settings["validation_profile"]
    name = f"Identity Validation {lang['name']}"
    profiles = bazarr_api("GET", "system/languages/profiles") or []
    if any(p.get("name") == name for p in profiles):
        return name
    enabled = [l["code2"] for l in bazarr_api("GET", "system/languages") or [] if l.get("enabled")]
    if lang["code2"] not in enabled:
        bazarr_api("POST", "system/settings",
                   form=[("languages-enabled", c) for c in enabled + [lang["code2"]]])
    profiles.append({
        "profileId": max([p["profileId"] for p in profiles] + [0]) + 1,
        "name": name, "cutoff": None,
        "items": [{"id": 1, "language": lang["code2"], "forced": "False", "hi": "False",
                   "audio_exclude": "False", "audio_only_include": "False"}],
        "mustContain": [], "mustNotContain": [], "originalFormat": None, "tag": None,
    })
    bazarr_api("POST", "system/settings", form=[("languages-profiles", json.dumps(profiles))])
    log(f"Created Bazarr profile '{name}' for identity references")
    return name


@contextmanager
def bazarr_validation_profile(series_id, profile_name=None):
    """Temporarily apply the validation search profile, restoring the saved value."""
    settings = _IDENTITY["bazarr"]
    profile_name = profile_name or settings["validation_profile"]
    with open(settings["config_file"]) as f:
        bazarr_config = yaml.safe_load(f)
    api_key = (bazarr_config.get("auth") or {}).get("apikey")
    if not api_key:
        raise RuntimeError("Bazarr API key is missing from its local config")
    if (bazarr_config.get("general") or {}).get("serie_default_enabled") is not False:
        raise RuntimeError("refusing temporary profile changes while Bazarr automatic series subtitles are enabled")

    base_url = settings["url"].rstrip("/") + "/api"
    lock_path = os.path.join(tempfile.gettempdir(), "mediacheck-bazarr-profile.lock")

    def request(method, path, params=None, timeout=60):
        query = urllib.parse.urlencode(params or [], doseq=True)
        url = f"{base_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        req = urllib.request.Request(
            url, method=method, headers={"X-API-KEY": api_key})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw.decode()) if raw.strip() else None

    def series_profile():
        result = request("GET", "series", [("seriesid[]", series_id)])
        records = (result or {}).get("data", [])
        if len(records) != 1:
            raise RuntimeError(f"Bazarr returned {len(records)} series for Sonarr ID {series_id}")
        return records[0].get("profileId")

    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        profiles = request("GET", "system/languages/profiles")
        profile = next((p for p in profiles
                        if p.get("name") == profile_name), None)
        if not profile:
            raise RuntimeError(f"Bazarr profile '{profile_name}' was not found")

        original_profile = series_profile()
        validation_profile = int(profile["profileId"])
        changed = original_profile != validation_profile
        try:
            if changed:
                request("POST", "series", [
                    ("seriesid", series_id), ("profileid", validation_profile)])
            yield request
        finally:
            if changed:
                current_profile = series_profile()
                if current_profile not in (original_profile, validation_profile):
                    raise RuntimeError(
                        "Bazarr series profile changed during identity search; "
                        "leaving the newer setting untouched")
                if current_profile == validation_profile:
                    restore_profile = "null" if original_profile is None else str(original_profile)
                    request("POST", "series", [
                        ("seriesid", series_id), ("profileid", restore_profile)])
                    if series_profile() != original_profile:
                        raise RuntimeError("failed to restore the original Bazarr series profile")


RATE_LIMIT_HTTP_CODES = (429, 503)


class ReferenceSearch:
    """What happened while looking for a reference subtitle, so "no source
    has one" can be told apart from "a source couldn't be asked"."""
    rate_limited = []      # "<provider>: <why>"
    download_failures = 0  # candidates that existed but couldn't be read


def _note_download_error(provider, exc):
    if isinstance(exc, urllib.error.HTTPError) and exc.code in RATE_LIMIT_HTTP_CODES:
        ReferenceSearch.rate_limited.append(f"{provider}: HTTP {exc.code}")
    else:
        ReferenceSearch.download_failures += 1
    log(f"Reference from {provider} failed: {type(exc).__name__}: {exc}")


# Bazarr throttle reasons that mean "asked too often" - the source still
# exists and will answer later. Other reasons (HTTPError, ConnectionError,
# ParseResponseError...) mean the provider itself is broken; those are logged
# but can't hold every check forever.
RATE_LIMIT_REASONS = ("TooManyRequests", "DownloadLimitExceeded", "APIThrottled",
                      "ServiceUnavailable",
                      # need the admin, not time; mediacheck DMs once and holds
                      "AuthenticationError", "ConfigurationError", "PaymentRequired")
# Providers that only carry one kind of media; the rest carry both.
TV_ONLY_PROVIDERS = ("gestdown", "tvsubtitles", "addic7ed")
MOVIE_ONLY_PROVIDERS = ("yifysubtitles",)


def bazarr_throttled_providers(is_tv):
    """Enabled Bazarr providers relevant to this media type that are
    rate-limited right now, as [(name, reason, retry)]. Bazarr silently
    leaves throttled providers out of searches."""
    with open(_IDENTITY["bazarr"]["config_file"]) as f:
        api_key = ((yaml.safe_load(f) or {}).get("auth") or {}).get("apikey")
    req = urllib.request.Request(f"{BAZARR_API}/providers", headers={"X-API-KEY": api_key})
    with urllib.request.urlopen(req, timeout=30) as response:
        rows = json.loads(response.read().decode()).get("data", [])
    throttled = []
    for r in rows:
        name, reason = r["name"], r.get("status")
        if reason in (None, "Good"):
            continue
        if name in (MOVIE_ONLY_PROVIDERS if is_tv else TV_ONLY_PROVIDERS):
            continue
        if reason not in RATE_LIMIT_REASONS:
            log(f"Bazarr provider {name} is unavailable ({reason}); not treated as a rate limit")
            continue
        throttled.append((name, reason, r.get("retry")))
    return throttled


def bazarr_throttled_providers_or_defer(is_tv):
    try:
        return bazarr_throttled_providers(is_tv)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code not in RATE_LIMIT_HTTP_CODES:
            raise
        log(
            "WARNING: BAZARR_PROVIDER_STATUS_UNAVAILABLE: couldn't verify "
            f"subtitle-provider availability ({type(exc).__name__}: {exc}); "
            "deferring this import check"
        )
        log("VERDICT: REFERENCE_RATE_LIMITED - Bazarr provider status unavailable")
        sys.exit(REFERENCE_RATE_LIMITED_EXIT)


def _is_true(value):
    return str(value).lower() in ("1", "true", "yes")


def _download_gestdown(candidate):
    parsed = urllib.parse.urlparse(candidate.get("url") or "")
    if (parsed.scheme != "https" or parsed.hostname != "api.gestdown.info"
            or not parsed.path.startswith("/subtitles/download/")):
        return None
    req = urllib.request.Request(
        candidate["url"], headers={"User-Agent": "mediacheck/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        final_url = urllib.parse.urlparse(response.geturl())
        if final_url.scheme != "https" or final_url.hostname != "api.gestdown.info":
            return None
        raw = response.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise RuntimeError("Bazarr subtitle reference exceeded the 10 MB safety limit")
    return raw.decode("utf-8-sig", errors="replace")


SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub")


def _subtitle_files(media_path):
    folder = os.path.dirname(media_path)
    stem = os.path.splitext(os.path.basename(media_path))[0]
    found = {}
    for name in os.listdir(folder):
        if name.startswith(stem) and name.lower().endswith(SUBTITLE_EXTS):
            path = os.path.join(folder, name)
            st = os.stat(path)
            found[path] = (st.st_mtime_ns, st.st_size)
    return found


def _download_via_bazarr(request, identity, candidate):
    """Have Bazarr download a candidate from any of its providers, read it,
    and leave the library as it was: a new subtitle file is removed, and one
    Bazarr overwrote gets its original contents back."""
    media_path = identity["media_path"]
    before = _subtitle_files(media_path)
    originals = {}
    for path in before:
        with open(path, "rb") as f:
            originals[path] = f.read()
    # hi=False: Bazarr's manual download crashes on its HI-exclusion check
    # for series whose profile it can't resolve. Only the saved file name
    # differs, and the file is removed again below.
    ids = ([("seriesid", identity["series_id"]), ("episodeid", identity["episode_id"])]
           if identity["is_tv"] else [("radarrid", identity["movie_id"])])
    request("POST", "providers/episodes" if identity["is_tv"] else "providers/movies", ids + [
        ("hi", "False"), ("forced", "False"),
        ("original_format", str(candidate.get("original_format") or False)),
        ("provider", candidate["provider"]), ("subtitle", candidate["subtitle"]),
    ], timeout=120)
    new_path = None
    deadline = time.time() + BAZARR_DOWNLOAD_WAIT_SECONDS
    while time.time() < deadline and not new_path:
        time.sleep(3)
        after = _subtitle_files(media_path)
        new_path = next((p for p, sig in after.items() if before.get(p) != sig), None)
    if not new_path:
        return None
    try:
        with open(new_path, "rb") as f:
            raw = f.read(10 * 1024 * 1024 + 1)
    finally:
        if new_path in originals:
            with open(new_path, "wb") as f:
                f.write(originals[new_path])
        else:
            os.remove(new_path)
        if identity["is_tv"]:
            request("PATCH", "series", [("seriesid", identity["series_id"]),
                                        ("action", "scan-disk")])
        else:
            request("PATCH", "movies", [("radarrid", identity["movie_id"]),
                                        ("action", "scan-disk")])
    if len(raw) > 10 * 1024 * 1024:
        raise RuntimeError("Bazarr subtitle reference exceeded the 10 MB safety limit")
    return raw.decode("utf-8-sig", errors="replace")


BAZARR_DOWNLOAD_WAIT_SECONDS = 120


def iter_episode_references(identity):
    """Yield (raw timed subtitle, candidate) for the exact episode, one
    download at a time: Gestdown first (downloaded directly), then every
    other provider Bazarr has enabled (downloaded through Bazarr), best score
    first. The search runs under the validation profile; downloads don't
    need it (they're requested with hi=False)."""
    settings = _IDENTITY["bazarr"]
    preferred = settings["reference_provider"]
    max_tries = int(settings.get("max_episode_reference_tries", 6))
    lang = identity["reference_language"]
    try:
        with bazarr_validation_profile(identity["series_id"],
                                       validation_profile_name(lang)) as request:
            result = request(
                "GET", "providers/episodes", [("episodeid", identity["episode_id"])],
                timeout=180)
    except RuntimeError as exc:
        if str(exc) != f"Bazarr returned 0 series for Sonarr ID {identity['series_id']}":
            raise
        log(
            "WARNING: BAZARR_EPISODE_NOT_INDEXED: Bazarr has no series record "
            f"for Sonarr series {identity['series_id']}; skipping subtitle "
            "comparison and accepting the imported file"
        )
        return
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        response_body = exc.read().decode(errors="replace").strip().strip('"')
        if response_body != "Episode not found":
            raise
        log(
            "WARNING: BAZARR_EPISODE_NOT_INDEXED: Bazarr has no record "
            f"for Sonarr episode {identity['episode_id']}; skipping subtitle "
            "comparison and accepting the imported file"
        )
        return
    candidates = result.get("data", []) if result else []
    suitable = [
        c for c in candidates
        if (c.get("language") or "").lower() in language_tags(lang)
        and not _is_true(c.get("forced"))
        and int(c.get("score") or 0) >= settings["minimum_candidate_score"]
    ]
    suitable.sort(key=lambda c: (c.get("provider") != preferred,
                                 _is_true(c.get("hearing_impaired")),
                                 -int(c.get("score") or 0)))
    for candidate in suitable[:max_tries]:
        provider = candidate.get("provider")
        try:
            if provider == preferred:
                reference = _download_gestdown(candidate)
            else:
                reference = _download_via_bazarr(bazarr_api, identity, candidate)
        except Exception as exc:
            _note_download_error(provider, exc)
            continue
        if not (reference and _SRT_TIMESTAMP.search(reference)):
            ReferenceSearch.download_failures += 1
            continue
        log(f"Using {provider} {lang['name']} reference subtitle (score {candidate.get('score')}, "
            f"release {candidate.get('release_info')})")
        yield reference, candidate


def bazarr_movie_candidates(identity):
    settings = _IDENTITY["bazarr"]
    lang = identity["reference_language"]
    profile_name = (settings["movie_validation_profile"] if lang["code2"] == "en"
                    else validation_profile_name(lang))
    with open(settings["config_file"]) as f:
        bazarr_config = yaml.safe_load(f)
    api_key = (bazarr_config.get("auth") or {}).get("apikey")
    if not api_key:
        raise RuntimeError("Bazarr API key is missing from its local config")
    if (bazarr_config.get("general") or {}).get("movie_default_enabled") is not False:
        raise RuntimeError(
            "refusing temporary movie profile changes while Bazarr automatic movie subtitles are enabled"
        )

    def request(method, path, params=None, timeout=60):
        query = urllib.parse.urlencode(params or [], doseq=True)
        url = f"{BAZARR_API}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        req = urllib.request.Request(
            url, method=method, headers={"X-API-KEY": api_key},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw.decode()) if raw.strip() else None

    def get_movie_profile():
        result = request("GET", "movies", [("radarrid[]", identity["movie_id"])])
        rows = (result or {}).get("data", [])
        movie = next(
            (row for row in rows if int(row.get("radarrId", -1)) == identity["movie_id"]),
            None,
        )
        if movie is None:
            raise RuntimeError(f"Bazarr has no record for Radarr movie {identity['movie_id']}")
        return movie.get("profileId")

    lock_path = os.path.join(tempfile.gettempdir(), "mediacheck-bazarr-profile.lock")
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        profiles = request("GET", "system/languages/profiles")
        profile = next(
            (p for p in profiles if p.get("name") == profile_name),
            None,
        )
        if not profile:
            raise RuntimeError(
                f"Bazarr profile '{profile_name}' was not found"
            )
        if not any(item.get("language") == lang["code2"] for item in profile.get("items", [])):
            raise RuntimeError(f"Bazarr profile '{profile_name}' must include {lang['name']}")

        original_profile = get_movie_profile()
        validation_profile = int(profile["profileId"])
        changed = original_profile != validation_profile
        try:
            if changed:
                request("POST", "movies", [
                    ("radarrid", identity["movie_id"]),
                    ("profileid", validation_profile),
                ])
                if int(get_movie_profile()) != validation_profile:
                    raise RuntimeError("Bazarr did not apply the temporary movie subtitle profile")

            result = request(
                "GET", "providers/movies", [("radarrid", identity["movie_id"])],
                timeout=180,
            )
            return (result or {}).get("data", [])
        finally:
            if changed:
                current_profile = get_movie_profile()
                if current_profile not in (original_profile, validation_profile):
                    raise RuntimeError(
                        "Bazarr movie profile changed during identity search; "
                        "leaving the newer setting untouched"
                    )
                if current_profile == validation_profile:
                    restore_profile = "null" if original_profile is None else str(original_profile)
                    request("POST", "movies", [
                        ("radarrid", identity["movie_id"]),
                        ("profileid", restore_profile),
                    ])
                    if get_movie_profile() != original_profile:
                        raise RuntimeError("failed to restore the original Bazarr movie profile")


class SubtitleCatLinkParser(HTMLParser):
    def __init__(self, language):
        super().__init__()
        self.target_id = f"download_{language}"
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("id") == self.target_id and attrs.get("href"):
            self.links.append(attrs["href"])


def download_subtitlecat_reference(candidate):
    page_url = urllib.parse.urlparse(candidate.get("url") or "")
    if (page_url.scheme != "https" or page_url.hostname != SUBTITLECAT_HOST
            or not page_url.path.startswith("/subs/")):
        raise RuntimeError("Bazarr returned a non-approved SubtitleCat reference URL")

    headers = {
        "User-Agent": SUBTITLECAT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.5",
    }
    req = urllib.request.Request(candidate["url"], headers=headers)
    with urllib.request.urlopen(req, timeout=45) as response:
        final_url = urllib.parse.urlparse(response.geturl())
        if final_url.scheme != "https" or final_url.hostname != SUBTITLECAT_HOST:
            raise RuntimeError("SubtitleCat page redirected outside its approved host")
        page = response.read(MAX_SUBTITLE_PAGE_BYTES + 1)
    if len(page) > MAX_SUBTITLE_PAGE_BYTES:
        raise RuntimeError("SubtitleCat reference page exceeded the 5 MB safety limit")

    parser = SubtitleCatLinkParser(candidate.get("language", ""))
    parser.feed(page.decode("utf-8", errors="replace"))
    if not parser.links:
        raise RuntimeError("SubtitleCat page has no download link for the selected language")
    download_url = urllib.parse.urljoin(candidate["url"], parser.links[0])
    parsed_download = urllib.parse.urlparse(download_url)
    download_url = parsed_download._replace(
        path=urllib.parse.quote(urllib.parse.unquote(parsed_download.path), safe="/")
    ).geturl()
    if parsed_download.scheme != "https" or parsed_download.hostname != SUBTITLECAT_HOST:
        raise RuntimeError("SubtitleCat subtitle link points outside its approved host")

    req = urllib.request.Request(download_url, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as response:
        final_url = urllib.parse.urlparse(response.geturl())
        if final_url.scheme != "https" or final_url.hostname != SUBTITLECAT_HOST:
            raise RuntimeError("SubtitleCat download redirected outside its approved host")
        content = response.read(MAX_SUBTITLE_BYTES + 1)
    if len(content) > MAX_SUBTITLE_BYTES:
        raise RuntimeError("SubtitleCat subtitle exceeded the 10 MB safety limit")
    return content.decode("utf-8-sig", errors="replace")


def download_yify_reference(candidate):
    """The English .srt inside a yifysubtitles.ch zip. The download sits behind
    Cloudflare, which lets it through only with the subtitle page as Referer."""
    page_url = urllib.parse.urlparse(candidate.get("url") or "")
    if (page_url.scheme != "https" or page_url.hostname != YIFY_HOST
            or not page_url.path.startswith("/subtitles/")):
        raise RuntimeError("Bazarr returned a non-approved YIFY reference URL")
    slug = page_url.path.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise RuntimeError("YIFY subtitle URL has an unexpected format")
    req = urllib.request.Request(
        f"https://{YIFY_HOST}/subtitle/{slug}.zip",
        headers={"User-Agent": SUBTITLECAT_USER_AGENT, "Referer": candidate["url"]},
    )
    with urllib.request.urlopen(req, timeout=45) as response:
        final_url = urllib.parse.urlparse(response.geturl())
        if final_url.scheme != "https" or final_url.hostname != YIFY_HOST:
            raise RuntimeError("YIFY download redirected outside its approved host")
        archive = response.read(MAX_SUBTITLE_BYTES + 1)
    if len(archive) > MAX_SUBTITLE_BYTES:
        raise RuntimeError("YIFY subtitle archive exceeded the 10 MB safety limit")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            srts = [i for i in zf.infolist() if i.filename.lower().endswith(".srt")]
            if not srts:
                raise RuntimeError("YIFY archive contains no .srt file")
            if srts[0].file_size > MAX_SUBTITLE_BYTES:
                raise RuntimeError("YIFY subtitle exceeded the 10 MB safety limit")
            content = zf.read(srts[0])
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"YIFY download was not a zip archive: {exc}") from exc
    return content.decode("utf-8-sig", errors="replace")


# Reference downloaders by Bazarr provider name, tried in the configured order.
MOVIE_REFERENCE_DOWNLOADERS = {
    "subtitlecat": download_subtitlecat_reference,
    "yifysubtitles": download_yify_reference,
}


_SRT_TIMESTAMP = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
)


def parse_srt_timestamp(value):
    hours, minutes, seconds = re.split(r"[:,]", value.replace(",", "."))
    whole_seconds, milliseconds = seconds.split(".", 1)
    milliseconds = (milliseconds + "000")[:3]
    return (
        int(hours) * 3600 + int(minutes) * 60 + int(whole_seconds)
        + int(milliseconds) / 1000
    )


def subtitle_middle_window(raw, window_seconds):
    """Return central subtitle dialogue and its final cue time."""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", raw.strip()):
        match = _SRT_TIMESTAMP.search(block)
        if not match:
            continue
        start = parse_srt_timestamp(match.group("start"))
        end = parse_srt_timestamp(match.group("end"))
        text = subtitle_dialogue_text(block[match.end():])
        if text:
            cues.append((start, end, text))
    if not cues:
        return "", 0.0

    duration = max(end for _, end, _ in cues)
    window_start = max(0.0, (duration - window_seconds) / 2)
    window_end = min(duration, window_start + window_seconds)
    text = " ".join(
        cue for start, end, cue in cues
        if end >= window_start and start <= window_end
    )
    return text, duration


ENGLISH = {"name": "English", "code2": "en", "code3": "eng"}


def choose_audio_stream(media_path, identity):
    """(audio stream index, language) to transcribe: an English track if there
    is one, else a track in the title's original language (per Radarr/Sonarr),
    else the only track if it's untagged. None if no track fits."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index:stream_tags=language",
             "-of", "json", media_path],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not inspect audio stream languages: {exc}") from exc
    streams = [(s["index"], ((s.get("tags") or {}).get("language") or "").lower())
               for s in json.loads(out).get("streams", [])]

    for index, tag in streams:
        if tag in language_tags(ENGLISH):
            return index, ENGLISH
    original = language_by_name(identity.get("original_language"))
    if original:
        for index, tag in streams:
            if tag in language_tags(original):
                return index, original
        if len(streams) == 1 and streams[0][1] in ("", "und"):
            return streams[0][0], original
    return None, original


def runtime_matches(actual_seconds, expected_minutes, tolerance_pct):
    if not expected_minutes or expected_minutes <= 0:
        return False
    expected_seconds = float(expected_minutes) * 60
    return abs(actual_seconds - expected_seconds) / expected_seconds <= tolerance_pct / 100


def same_release(candidate, scene_name):
    if not scene_name:
        return False
    scene = normalize_title(scene_name)
    for release in candidate.get("release_info") or []:
        if not isinstance(release, str):
            continue
        candidate_release = re.sub(r"_track\d+.*$", "", release, flags=re.I)
        normalized = normalize_title(candidate_release)
        if (normalized == scene or normalized.startswith(scene + " ")
                or scene.startswith(normalized + " ")):
            return True
    return False


def iter_movie_references(identity, expected_runtime):
    """Directly downloadable providers (SubtitleCat, YIFY) in configured
    order, plus every other provider Bazarr has enabled (OpenSubtitles.com,
    ...), downloaded through Bazarr. For a non-English reference the Bazarr
    ones go first: SubtitleCat's non-English files are machine translations,
    which won't match what's actually said."""
    settings = _IDENTITY["bazarr"]
    lang = identity["reference_language"]
    candidates = bazarr_movie_candidates(identity)
    minimum_score = int(settings["minimum_movie_candidate_score"])
    direct = settings["movie_reference_providers"]
    acceptable = [
        candidate for candidate in candidates
        if (candidate.get("provider") not in MOVIE_REFERENCE_DOWNLOADERS
            or candidate.get("provider") in direct)
        and (candidate.get("language") or "").lower() in language_tags(lang)
        and isinstance(candidate.get("release_info"), list)
        and any(isinstance(release, str) and release.strip()
                for release in candidate["release_info"])
        and str(candidate.get("forced", "")).lower() not in ("1", "true", "yes")
        and int(candidate.get("score") or 0) >= minimum_score
        and not same_release(candidate, identity.get("scene_name"))
    ]

    def order(item):
        provider = item["provider"]
        via_bazarr = provider not in MOVIE_REFERENCE_DOWNLOADERS
        rank = (0 if via_bazarr else 1) if lang["code2"] != "en" else (
            1 if via_bazarr else 0)
        within = 0 if via_bazarr else direct.index(provider)
        # Hearing-impaired subtitles add sound descriptions, which only pad
        # the reference; still prefer plain ones.
        return (rank, within, _is_true(item.get("hearing_impaired")),
                -int(item.get("score") or 0))
    acceptable.sort(key=order)
    # Up to five tries per provider so one dead link can't sink the check.
    tries = {}
    for candidate in acceptable:
        provider = candidate["provider"]
        tries[provider] = tries.get(provider, 0) + 1
        if tries[provider] > 5:
            continue
        try:
            if provider in MOVIE_REFERENCE_DOWNLOADERS:
                raw = MOVIE_REFERENCE_DOWNLOADERS[provider](candidate)
            else:
                raw = _download_via_bazarr(bazarr_api, identity, candidate)
                if not raw:
                    ReferenceSearch.download_failures += 1
                    continue
            _, subtitle_duration = subtitle_middle_window(raw, 1)
        except (OSError, RuntimeError, urllib.error.URLError, ValueError) as exc:
            _note_download_error(provider, exc)
            continue
        if not runtime_matches(
            subtitle_duration, expected_runtime,
            float(_IDENTITY["movie_runtime_tolerance_pct"]),
        ):
            log(f"Skipping movie subtitle candidate with incompatible runtime "
                f"({provider}, score {candidate.get('score')}, "
                f"{subtitle_duration / 60:.1f} min)")
            continue
        log(f"Using {provider} {lang['name']} reference subtitle (score {candidate.get('score')}, "
            f"release {candidate.get('release_info')})")
        yield raw, candidate


def subtitle_dialogue_text(raw):
    """Remove subtitle timing and formatting, leaving only spoken text."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if (not line or line.isdigit() or "-->" in line
                or line.upper() == "WEBVTT"):
            continue
        if line.startswith("Dialogue:"):
            line = line.split(",", 9)[-1]
        line = re.sub(r"\{[^}]*\}|<[^>]*>", " ", line)
        lines.append(html.unescape(line))
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def episode_match_score(dialogue_samples, reference):
    """Coverage of the file's distinct four-word phrases in the reference."""
    def fourgrams(text):
        # Accents folded first so e.g. Turkish "güzel" stays one word.
        words = re.findall(r"\w+", fold_text(text).lower())
        return set(zip(words, words[1:], words[2:], words[3:]))

    dialogue_grams = set()
    for sample in dialogue_samples:
        dialogue_grams.update(fourgrams(sample))
    reference_grams = fourgrams(reference)
    matches = len(dialogue_grams & reference_grams)
    return (matches / len(dialogue_grams) * 100 if dialogue_grams else 0.0,
            matches, len(dialogue_grams))


def get_duration_seconds(media_path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", media_path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def extract_audio(media_path, start_seconds, minutes, audio_stream_index=None):
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(start_seconds),
        "-i", media_path,
        "-t", str(minutes * 60),
    ]
    if audio_stream_index is not None:
        cmd.extend(["-map", f"0:{audio_stream_index}"])
    cmd.extend(["-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", tmp.name])
    subprocess.run(cmd, check=True)
    return tmp.name


def transcribe(audio_path, language=None):
    with open(audio_path, "rb") as f:
        return mclib.transcribe(f.read(), language=language)


def fold_text(value):
    value = (value or "").translate(str.maketrans({
        "ç": "c", "Ç": "C", "ğ": "g", "Ğ": "G", "ı": "i", "İ": "I",
        "ö": "o", "Ö": "O", "ş": "s", "Ş": "S", "ü": "u", "Ü": "U",
    }))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return value


def normalize_title(value):
    value = fold_text(value)
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


EXIT_CODES = {"OK": 0, "MISMATCH": 10, "UNVERIFIABLE": 20}
# Transcription was unreachable; the caller waits for it and re-runs the check.
TRANSCRIPTION_UNAVAILABLE_EXIT = 30
# A subtitle source was rate-limited/throttled and nothing else had a
# reference: the caller holds the check until the source is usable again.
REFERENCE_RATE_LIMITED_EXIT = 31
# Sources do list subtitles for the title, but none could be downloaded.
REFERENCE_DOWNLOAD_FAILED_EXIT = 32


def verdict(kind, detail):
    log(f"VERDICT: {kind} - {detail}")
    sys.exit(EXIT_CODES[kind])


def no_reference_verdict(no_reference_detail, is_tv):
    """Only "every source answered and none has a subtitle" is a true skip."""
    throttled = bazarr_throttled_providers_or_defer(is_tv)
    limited = ReferenceSearch.rate_limited + [
        f"{name}: {reason} (retry {retry})" for name, reason, retry in throttled]
    if limited:
        log("EVIDENCE: " + json.dumps({"rate_limited_sources": limited}))
        log(f"VERDICT: REFERENCE_RATE_LIMITED - {'; '.join(limited)}")
        sys.exit(REFERENCE_RATE_LIMITED_EXIT)
    if ReferenceSearch.download_failures:
        log(f"VERDICT: REFERENCE_DOWNLOAD_FAILED - {ReferenceSearch.download_failures} "
            "subtitle(s) were listed but none could be downloaded")
        sys.exit(REFERENCE_DOWNLOAD_FAILED_EXIT)
    log(f"WARNING: NO_SUBTITLE_REFERENCE: {no_reference_detail}")
    verdict("UNVERIFIABLE", no_reference_detail)


def external_subtitle_references(identity):
    """(iterator of (raw subtitle, candidate), what to say if it's empty)."""
    name = identity["reference_language"]["name"]
    if identity["is_tv"]:
        return (iter_episode_references(identity),
                f"no usable exact-episode {name} subtitle from any provider")
    return (iter_movie_references(identity, identity.get("runtime")),
            f"no usable independent {name} movie subtitle from any provider")


def next_reference(references):
    """The next reference, or None. A Bazarr that can't be reached is a
    source that couldn't be asked: the check is held, not failed."""
    try:
        return next(references, None)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code not in RATE_LIMIT_HTTP_CODES:
            raise
        log(f"VERDICT: REFERENCE_RATE_LIMITED - Bazarr unavailable: {type(exc).__name__}: {exc}")
        sys.exit(REFERENCE_RATE_LIMITED_EXIT)


def verify_media_audio(media_path, identity):
    if identity["is_tv"] and identity.get("episode_count") != 1:
        verdict("UNVERIFIABLE", "combined multi-episode files are not yet supported")

    duration = get_duration_seconds(media_path)
    if duration <= 0:
        verdict("UNVERIFIABLE", "media file has no valid duration")
    sample_seconds = min(
        float(_IDENTITY["audio_sample_minutes"]) * 60,
        duration,
    )
    sample_start = (duration - sample_seconds) / 2

    audio_stream, lang = choose_audio_stream(media_path, identity)
    if audio_stream is None:
        original = lang["name"] if lang else (identity.get("original_language") or "unknown")
        verdict("UNVERIFIABLE", f"no audio track in English or the original language "
                                f"({original}) to compare with a subtitle")
    identity["reference_language"] = lang
    log(f"Audio: stream {audio_stream} ({lang['name']}); reference subtitles in {lang['name']}")

    identity["media_path"] = media_path
    references, no_reference_detail = external_subtitle_references(identity)
    current = next_reference(references)
    if not current:
        no_reference_verdict(no_reference_detail, identity["is_tv"])

    audio_path = extract_audio(
        media_path, sample_start, sample_seconds / 60, audio_stream,
    )
    try:
        transcript = transcribe(audio_path, language=lang["code2"])
    except (urllib.error.URLError, OSError) as e:
        if isinstance(e, urllib.error.HTTPError) and e.code < 500:
            raise
        log(f"VERDICT: TRANSCRIPTION_UNAVAILABLE - {type(e).__name__}: {e}")
        sys.exit(TRANSCRIPTION_UNAVAILABLE_EXIT)
    finally:
        os.unlink(audio_path)
    transcript_words = len(transcript.split())
    if transcript_words < int(_IDENTITY["minimum_transcript_words"]):
        verdict("UNVERIFIABLE", f"audio sample produced only {transcript_words} "
                               "transcript words")

    threshold = float(_IDENTITY["match_threshold_pct"])
    expected_runtime = identity.get("runtime")
    runtime_difference_pct = None
    if expected_runtime:
        runtime_difference_pct = round(
            abs(duration / 60 - float(expected_runtime)) / float(expected_runtime) * 100,
            2,
        )

    # A reference can itself be wrong (another episode uploaded under this
    # number, a different cut), so a low score moves on to the next one. Only
    # when no available reference matches is it a mismatch.
    tried = []
    best = None
    while current:
        reference_raw, candidate = current
        _, subtitle_duration = subtitle_middle_window(reference_raw, 1)
        reference_window_seconds = (
            subtitle_duration if duration <= sample_seconds else sample_seconds
        )
        reference, _ = subtitle_middle_window(reference_raw, reference_window_seconds)
        reference_words = len(reference.split())
        if reference_words < int(_IDENTITY["minimum_reference_words"]):
            log(f"Reference from {candidate.get('provider')} has only {reference_words} "
                "dialogue words in the matching window; trying the next one")
            current = next_reference(references)
            continue
        pct, matches, total = episode_match_score([transcript], reference)
        log(f"Transcribed {sample_seconds / 60:.1f} minutes from the center of the file "
            f"({transcript_words} words); compared against the matching timed subtitle "
            f"window from {candidate.get('provider')} ({reference_words} words).")
        log(f"Matching four-word phrases: {matches}/{total} = {pct:.1f}% "
            f"(threshold {threshold:.1f}%)")
        evidence = {
            "method": "centered_audio_to_exact_title_subtitle",
            "claimed_title": identity["label"],
            "media_type": "episode" if identity["is_tv"] else "movie",
            "sonarr_episode_id" if identity["is_tv"] else "radarr_movie_id":
                identity["episode_id"] if identity["is_tv"] else identity["movie_id"],
            "reference_provider": candidate.get("provider"),
            "reference_language": candidate.get("language"),
            "reference_score": candidate.get("score"),
            "reference_release": candidate.get("release_info", []),
            "audio_sample_start_seconds": round(sample_start),
            "audio_sample_minutes": round(sample_seconds / 60, 2),
            "media_duration_minutes": round(duration / 60, 2),
            "subtitle_duration_minutes": round(subtitle_duration / 60, 2),
            "transcript_words": transcript_words,
            "reference_words": reference_words,
            "matching_four_word_phrases": matches,
            "sampled_four_word_phrases": total,
            "match_percent": round(pct, 2),
            "threshold_percent": threshold,
            "audio_language": lang["code2"],
            "references_tried": len(tried) + 1,
        }
        if expected_runtime:
            evidence["arr_expected_runtime_minutes"] = expected_runtime
            evidence["arr_runtime_difference_percent"] = runtime_difference_pct
            evidence["runtime_tolerance_percent"] = float(
                _IDENTITY["movie_runtime_tolerance_pct"]
            ) if not identity["is_tv"] else None
        log("EVIDENCE: " + json.dumps(evidence, ensure_ascii=False))
        if pct >= threshold:
            if (not identity["is_tv"] and runtime_difference_pct is not None
                    and runtime_difference_pct
                    > float(_IDENTITY["movie_runtime_tolerance_pct"])):
                verdict("MISMATCH", f"dialogue matched {pct:.1f}% of the subtitle, but the file "
                                    f"runtime differs from Radarr by {runtime_difference_pct:.1f}%")
            verdict("OK", f"center audio matches the exact-title subtitle "
                          f"({matches}/{total} four-word phrases, {pct:.1f}%"
                          + (f"; {len(tried)} other reference(s) didn't match" if tried else "")
                          + ")")
        tried.append(f"{candidate.get('provider')} {pct:.1f}%")
        if best is None or pct > best[0]:
            best = (pct, matches, total)
        current = next_reference(references)

    if best is None:
        no_reference_verdict(no_reference_detail, identity["is_tv"])
    # References that couldn't be fetched because of a rate limit might have
    # matched: hold rather than report.
    if ReferenceSearch.rate_limited or bazarr_throttled_providers_or_defer(identity["is_tv"]):
        no_reference_verdict(no_reference_detail, identity["is_tv"])
    pct, matches, total = best
    verdict("MISMATCH", f"center audio does not sufficiently match any exact-title "
                        f"subtitle (best {matches}/{total} four-word phrases, {pct:.1f}%; "
                        f"tried {', '.join(tried)})")


def main():
    args = [a for a in sys.argv[1:] if a != "--no-audio"]
    allow_audio = "--no-audio" not in sys.argv[1:]
    if len(args) != 1:
        print("Usage: identity_check.py [--no-audio] <path-to-media-file>",
              file=sys.stderr)
        sys.exit(2)

    media_path = args[0]
    is_tv = "/tv/" in media_path

    log(f"==== {media_path} ====")

    identity = identify(os.path.basename(media_path), is_tv)
    if not identity:
        verdict("UNVERIFIABLE", "Sonarr/Radarr couldn't identify the title from the filename")
    log(f"Claimed: {identity['label']}")
    log("EVIDENCE: " + json.dumps({
        "claimed_movie" if not identity["is_tv"] else "claimed_episode": identity["label"],
        "radarr_movie_id" if not identity["is_tv"] else "sonarr_episode_id":
            identity["movie_id"] if not identity["is_tv"] else identity["episode_id"],
    }, ensure_ascii=False))

    if not allow_audio:
        verdict("UNVERIFIABLE", "audio transcription was explicitly disabled")
    verify_media_audio(media_path, identity)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"VERDICT: ERROR - {type(e).__name__}: {e}")
        sys.exit(2)
