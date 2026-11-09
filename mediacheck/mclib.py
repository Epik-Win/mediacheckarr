"""Shared plumbing for mediacheck and identity_check.py: configuration,
logging, Debuggarr admin DMs, and the Radarr/Sonarr client used to re-acquire
bad files.

Configuration comes from two files in MEDIACHECK_CONFIG_DIR:
  checkrr.yaml    - Radarr/Sonarr ports, API keys and URL bases
  mediacheck.yaml - mediacheck's own policy and paths
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import yaml

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get("MEDIACHECK_CONFIG_DIR", DIR)
DATA_DIR = os.environ.get("MEDIACHECK_DATA_DIR", CONFIG_DIR)
LOGFILE = os.environ.get("MEDIACHECK_LOG", os.path.join(DATA_DIR, "mediacheck.log"))
IDENTITY_LOG = os.environ.get(
    "IDENTITY_LOG", os.path.join(DATA_DIR, "identity_check.log"),
)


def load_config():
    with open(os.path.join(CONFIG_DIR, "mediacheck.yaml")) as f:
        cfg = yaml.safe_load(f)
    with open(os.path.join(CONFIG_DIR, "checkrr.yaml")) as f:
        ck = yaml.safe_load(f)

    cfg["arr"] = {}
    for name, a in (ck.get("arr") or {}).items():
        address = (a.get("address") or "localhost").rstrip("/")
        scheme = "https" if a.get("ssl") else "http"
        if "://" in address:
            origin = address
        else:
            origin = f"{scheme}://{address}:{a['port']}"
        base = (a.get("baseurl") or "").strip("/")
        api_path = f"/{base}" if base else ""
        cfg["arr"][name] = {
            "url": f"{origin}{api_path}/api/v3",
            "key": a["apikey"],
        }
    # Test hooks: point everything at a scratch library.
    if os.environ.get("MEDIACHECK_ROOTS"):
        cfg["library_dirs"] = os.environ["MEDIACHECK_ROOTS"].split(":")
    if os.environ.get("MEDIACHECK_QUARANTINE"):
        cfg["quarantine_dir"] = os.environ["MEDIACHECK_QUARANTINE"]
    return cfg


def log(msg, echo=False):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with open(LOGFILE, "a") as f:
        f.write(line + "\n")
    if echo:
        print(msg, flush=True)


DEBUGGARR_ENV = os.environ.get(
    "DEBUGGARR_API_SECRET_FILE", "/run/secrets/debuggarr.env",
)


def _transcription_key(settings):
    with open(settings["key_file"]) as f:
        for line in f:
            if line.startswith(settings["key_name"] + "="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{settings['key_name']} not set in {settings['key_file']}")


def transcribe(audio_data, filename="audio.mp3", content_type="audio/mpeg",
               language=None, timeout=1800):
    """Speech-to-text through LiteLLM (identity.transcription in mediacheck.yaml).

    Raises urllib.error.URLError / OSError when transcription is unavailable.
    """
    settings = load_config()["identity"]["transcription"]
    boundary = "----mediacheckboundary"
    fields = {"model": settings["model"]}
    if language:
        fields["language"] = language
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    )
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + audio_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        settings["url"],
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {_transcription_key(settings)}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())["text"]


def _debuggarr_url(cfg):
    """Debuggarr has no published port, so find its IP on the Docker network
    (it changes whenever the container is recreated). mediacheck.yaml's
    debuggarr_url overrides this."""
    if cfg.get("debuggarr_url"):
        return cfg["debuggarr_url"]
    if os.environ.get("DEBUGGARR_URL"):
        return os.environ["DEBUGGARR_URL"]
    ip = subprocess.run(
        ["docker", "inspect", "debuggarr", "--format",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
        capture_output=True, text=True, timeout=10).stdout.split()
    if not ip:
        raise RuntimeError("debuggarr container not found")
    return f"http://{ip[0]}:8095/api/notify"


def _debuggarr_api_url(cfg):
    url = _debuggarr_url(cfg).rstrip("/")
    if url.endswith("/api/notify"):
        return url[:-len("/notify")]
    if not url.endswith("/api"):
        return url + "/api"
    return url


def _debuggarr_secret():
    with open(DEBUGGARR_ENV) as f:
        for line in f:
            if line.startswith("API_SECRET="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"API_SECRET not set in {DEBUGGARR_ENV}")


def _secret_value(name):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        with open(DEBUGGARR_ENV) as f:
            for line in f:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _telegram_alert(msg):
    token = _secret_value("MEDIACHECK_TELEGRAM_BOT_TOKEN")
    chat_id = _secret_value("MEDIACHECK_TELEGRAM_CHAT_ID")
    if not token and not chat_id:
        return
    if not token or not chat_id:
        raise RuntimeError(
            "Both MEDIACHECK_TELEGRAM_BOT_TOKEN and MEDIACHECK_TELEGRAM_CHAT_ID are required"
        )
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": msg[:4000],
        }).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Telegram API returned HTTP {error.code}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Telegram request failed: {error.reason}") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram did not confirm the alert: {result}")


def debuggarr_request(cfg, path, method="GET", payload=None):
    url = f"{_debuggarr_api_url(cfg)}/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "X-API-Key": _debuggarr_secret()},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode()) if raw.strip() else None


def request_identity_review(cfg, review):
    result = debuggarr_request(cfg, "reviews", "POST", review)
    if result.get("status") != "pending":
        raise RuntimeError(f"Debuggarr did not confirm the identity review DM: {result}")
    return result


def list_identity_review_decisions(cfg):
    result = debuggarr_request(cfg, "reviews?source=mediacheck&status=decided")
    reviews = result.get("reviews", [])
    legacy = debuggarr_request(cfg, "mediacheck/reviews").get("reviews", [])
    for review in legacy:
        review["details"] = {
            **(review.get("evidence") or {}),
            "media_path": review.get("media_path"),
        }
        review["_legacy"] = True
    return reviews + legacy


def complete_identity_review(cfg, review_id, result, legacy=False):
    path = (
        f"mediacheck/reviews/{urllib.parse.quote(review_id, safe='')}/complete"
        if legacy else
        f"reviews/{urllib.parse.quote(review_id, safe='')}/complete"
    )
    return debuggarr_request(
        cfg, path, "POST", {"result": result},
    )


def withdraw_identity_review(cfg, review_id, result):
    """Retract an unanswered review and delete its DM."""
    return debuggarr_request(
        cfg, f"reviews/{urllib.parse.quote(review_id, safe='')}/withdraw",
        "POST", {"result": result},
    )


def alert_admin(cfg, msg, notification_id=None):
    """Notify the admin privately through Debuggarr and/or Telegram."""
    if os.environ.get("MEDIACHECK_NOTIFY") == "0":
        log(f"admin alert (suppressed): {msg}")
        return
    try:
        req = urllib.request.Request(
            _debuggarr_url(cfg),
            data=json.dumps({
                "source": "mediacheck",
                "message": msg[:1800],
                **({"notification_id": notification_id} if notification_id else {}),
            }).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": _debuggarr_secret()})
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read())
        if result.get("status") != "sent":
            raise RuntimeError(f"Debuggarr did not confirm the DM was sent: {result}")
    except Exception as e:
        log(f"Debuggarr admin DM failed; no channel fallback: {type(e).__name__}: {e}")
    try:
        _telegram_alert(msg)
    except Exception as e:
        log(f"Telegram admin alert failed: {type(e).__name__}: {e}")


def resolve_admin_notification(cfg, notification_id, source="mediacheck"):
    path = (
        f"notify/{urllib.parse.quote(source, safe='')}/"
        f"{urllib.parse.quote(notification_id, safe='')}/resolve"
    )
    return debuggarr_request(cfg, path, "POST", {})


def short(path, cfg):
    return path.replace(cfg["host_media_root"], "", 1)


class Arr:
    """Minimal Radarr/Sonarr v3 API client."""

    def __init__(self, name, url, key):
        self.name, self.url, self.key = name, url, key

    def call(self, method, path, body=None, **params):
        url = f"{self.url}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Api-Key": self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        return json.loads(raw) if raw.strip() else None

    def command(self, name, wait=True, **body):
        cmd = self.call("POST", "command", dict(body, name=name))
        if wait:
            for _ in range(120):
                state = self.call("GET", f"command/{cmd['id']}")
                if state["status"] in ("completed", "failed", "aborted", "cancelled"):
                    return state
                time.sleep(2)
        return cmd


def arr_for(path, cfg):
    """(Arr client, path as the *arrs see it) for a library file."""
    name = "sonarr" if "/tv/" in path else "radarr"
    a = cfg["arr"][name]
    return (Arr(name, a["url"], a["key"]),
            path.replace(cfg["host_media_root"], cfg["arr_media_root"], 1))


def locate(path, cfg):
    """Find the Radarr movie / Sonarr episodes a file belongs to.

    Returns a dict with the client, ids for rescans and searches, and the
    history event that grabbed this exact release (None for manual imports).
    """
    arr, arr_path = arr_for(path, cfg)
    folder = os.path.dirname(arr_path)
    loc = {"arr": arr, "arr_path": arr_path, "grab": None}

    if arr.name == "radarr":
        movie = next((m for m in arr.call("GET", "movie") if m["path"].rstrip("/") == folder), None)
        if not movie:
            return None
        loc.update(title=f"{movie['title']} ({movie.get('year')})", movie_id=movie["id"])
        history = arr.call("GET", "history/movie", movieId=movie["id"])
    else:
        series = next((s for s in arr.call("GET", "series")
                       if arr_path.startswith(s["path"].rstrip("/") + "/")), None)
        if not series:
            return None
        efile = next((e for e in arr.call("GET", "episodefile", seriesId=series["id"])
                      if e["path"] == arr_path), None)
        episodes = [e for e in arr.call("GET", "episode", seriesId=series["id"])
                    if efile and e.get("episodeFileId") == efile["id"]]
        loc.update(title=series["title"], series_id=series["id"],
                   episode_ids=[e["id"] for e in episodes])
        history = arr.call("GET", "history/series", seriesId=series["id"])
        if isinstance(history, dict):
            history = history.get("records", [])

    # Compared without the extension: the old MKV->MP4 remux changed it.
    stem = os.path.splitext(arr_path)[0]
    imported = next((h for h in history if h["eventType"] == "downloadFolderImported"
                     and os.path.splitext((h.get("data") or {}).get("importedPath", ""))[0] == stem), None)
    if imported and imported.get("downloadId"):
        loc["grab"] = next((h for h in history if h["eventType"] == "grabbed"
                            and h.get("downloadId") == imported["downloadId"]), None)
    return loc


def rescan(loc):
    if loc["arr"].name == "radarr":
        loc["arr"].command("RescanMovie", movieId=loc["movie_id"])
    else:
        loc["arr"].command("RescanSeries", seriesId=loc["series_id"])


def quarantine(path, cfg):
    """Move a file out of the library, keeping its relative path."""
    dest = os.path.join(cfg["quarantine_dir"],
                        datetime.now().strftime("%Y%m%d-%H%M%S"), short(path, cfg).lstrip("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(path, dest)
    return dest


def reacquire(path, reason, cfg, dry_run=False):
    """Remove a bad file and have Radarr/Sonarr fetch a different release.

    1. quarantine the file (reversible for quarantine_days)
    2. rescan, so the *arr knows the file is gone
    3. mark the release that produced it as failed -> blocklisted, so the
       search can't pick the same one again
    4. search for a replacement
    Returns a human-readable summary.
    """
    loc = locate(path, cfg)
    if not loc:
        return f"not found in Radarr/Sonarr - left in place"
    grab = loc["grab"]
    plan = (f"{loc['arr'].name}: {loc['title']} - quarantine file, rescan, "
            + (f"blocklist release '{grab['sourceTitle']}', " if grab else
               "no grab history (manual import) so nothing to blocklist, ")
            + "search for a replacement")
    if dry_run:
        return "DRY RUN - would: " + plan

    dest = quarantine(path, cfg)
    log(f"quarantined {path} -> {dest} ({reason})")
    rescan(loc)
    if grab:
        try:
            loc["arr"].call("POST", f"history/failed/{grab['id']}")
        except urllib.error.HTTPError as e:
            log(f"mark-failed returned {e.code} for history {grab['id']} - searching anyway")
    if loc["arr"].name == "radarr":
        loc["arr"].command("MoviesSearch", wait=False, movieIds=[loc["movie_id"]])
    elif loc["episode_ids"]:
        loc["arr"].command("EpisodeSearch", wait=False, episodeIds=loc["episode_ids"])
    return plan + f" - done (quarantined at {dest})"
