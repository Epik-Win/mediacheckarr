import contextlib
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mclib

_TEST_CONFIG_DIR = tempfile.TemporaryDirectory()
mclib.CONFIG_DIR = _TEST_CONFIG_DIR.name
mclib.DATA_DIR = _TEST_CONFIG_DIR.name
mclib.LOGFILE = str(Path(_TEST_CONFIG_DIR.name) / "mediacheck.log")
mclib.IDENTITY_LOG = str(Path(_TEST_CONFIG_DIR.name) / "identity_check.log")
Path(_TEST_CONFIG_DIR.name, "mediacheck.yaml").write_text(
    "identity:\n"
    "  audio_sample_minutes: 30\n"
    "  minimum_transcript_words: 50\n"
    "  minimum_reference_words: 50\n"
    "  match_threshold_pct: 15\n"
    "  movie_runtime_tolerance_pct: 25\n"
    "  bazarr:\n"
    "    url: http://localhost:6767\n"
    "    minimum_movie_candidate_score: 70\n"
    "    movie_reference_providers: [subtitlecat, yifysubtitles]\n",
)
Path(_TEST_CONFIG_DIR.name, "checkrr.yaml").write_text("arr: {}\n")

import identity_check


def srt_timestamp(seconds):
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},000"


def timed_subtitle(duration_seconds=3600):
    text = (
        "The quick brown fox speaks clearly while the brave hero "
        "crosses the river and finds the hidden road."
    )
    return "\n\n".join(
        f"{index // 5 + 1}\n{srt_timestamp(index)} --> "
        f"{srt_timestamp(min(index + 4, duration_seconds))}\n{text}"
        for index in range(0, duration_seconds, 5)
    )


class SharedAudioIdentityTests(unittest.TestCase):
    movie = {
        "label": "Example Film (2024)",
        "movie_id": 42,
        "runtime": 90,
        "is_tv": False,
    }
    episode = {
        "label": "Example Show - S01E02 - The Example",
        "episode_id": 84,
        "episode_count": 1,
        "is_tv": True,
    }
    candidate = {
        "provider": "subtitle-provider",
        "language": "en",
        "score": 90,
        "release_info": ["Independent reference"],
    }
    script = (
        "The quick brown fox speaks clearly while the brave hero crosses the river. "
    ) * 5

    def run_verification(self, identity, duration):
        audio_args = []
        window_calls = []
        original_window = identity_check.subtitle_middle_window

        def inspect_window(raw, seconds):
            window_calls.append(seconds)
            return original_window(raw, seconds)

        with (
            patch.object(identity_check, "get_duration_seconds", return_value=duration),
            patch.object(
                identity_check, "choose_audio_stream",
                return_value=(2, identity_check.ENGLISH),
            ),
            patch.object(
                identity_check, "extract_audio",
                side_effect=lambda path, start, minutes, stream: (
                    audio_args.append((start, minutes, stream)) or "/tmp/sample.mp3"
                ),
            ),
            patch.object(identity_check, "transcribe", return_value=self.script),
            patch.object(identity_check.os, "unlink"),
            patch.object(
                identity_check, "external_subtitle_references",
                return_value=(iter([(timed_subtitle(), self.candidate)]), "unverifiable"),
            ),
            patch.object(
                identity_check, "subtitle_middle_window", side_effect=inspect_window,
            ),
            patch.object(identity_check, "log"),
        ):
            with self.assertRaises(SystemExit) as result:
                identity_check.verify_media_audio("sample.mkv", identity)
        return result.exception.code, audio_args, window_calls

    def test_movie_uses_centered_30_minute_sample(self):
        code, calls, windows = self.run_verification(self.movie, duration=90 * 60)
        self.assertEqual(code, identity_check.EXIT_CODES["OK"])
        self.assertEqual(calls, [(30 * 60, 30, 2)])
        self.assertEqual(windows, [1, 30 * 60])

    def test_episode_uses_same_centered_30_minute_sample(self):
        code, calls, windows = self.run_verification(self.episode, duration=50 * 60)
        self.assertEqual(code, identity_check.EXIT_CODES["OK"])
        self.assertEqual(calls, [(10 * 60, 30, 2)])
        self.assertEqual(windows, [1, 30 * 60])

    def test_short_video_uses_entire_file(self):
        code, calls, windows = self.run_verification(self.episode, duration=12 * 60)
        self.assertEqual(code, identity_check.EXIT_CODES["OK"])
        self.assertEqual(calls, [(0, 12, 2)])
        self.assertEqual(windows, [1, 3599])

    def test_runtime_mismatch_does_not_skip_transcription_or_subtitle_comparison(self):
        mismatch_movie = {**self.movie, "runtime": 120}
        code, calls, _ = self.run_verification(mismatch_movie, duration=60 * 60)
        self.assertEqual(code, identity_check.EXIT_CODES["MISMATCH"])
        self.assertEqual(calls, [(15 * 60, 30, 2)])

    def test_missing_english_audio_is_unverifiable(self):
        with (
            patch.object(identity_check, "get_duration_seconds", return_value=900),
            patch.object(
                identity_check, "choose_audio_stream",
                return_value=(None, None),
            ),
            patch.object(identity_check, "extract_audio") as extract,
            patch.object(identity_check, "log"),
        ):
            with self.assertRaises(SystemExit) as result:
                identity_check.verify_media_audio(self.episode, self.episode)
        self.assertEqual(result.exception.code, identity_check.EXIT_CODES["UNVERIFIABLE"])
        extract.assert_not_called()

    def test_subtitle_middle_window_requires_timed_reference(self):
        text, duration = identity_check.subtitle_middle_window("No timestamps here", 1800)
        self.assertEqual((text, duration), ("", 0.0))


class MovieReferenceProviderTests(unittest.TestCase):
    def candidate(self, provider, score, url):
        return {"provider": provider, "language": "en", "score": score, "url": url,
                "release_info": [f"{provider}.release"], "forced": False, "hearing_impaired": False}

    def test_falls_back_to_yify_when_subtitlecat_link_is_dead(self):
        cat = self.candidate("subtitlecat", 90, "https://www.subtitlecat.com/subs/1/a.html")
        yify = self.candidate("yifysubtitles", 80, "https://yifysubtitles.ch/subtitles/film-english-yify-1")
        tried = []

        def dead(candidate):
            tried.append(candidate["provider"])
            raise RuntimeError("HTTP Error 404: Not Found")

        def works(candidate):
            tried.append(candidate["provider"])
            return timed_subtitle(90 * 60)

        identity = {
            "reference_language": {"name": "English", "code2": "en", "code3": "eng"},
            "scene_name": None,
        }
        with patch.object(identity_check, "bazarr_movie_candidates", return_value=[yify, cat]), \
                patch.dict(identity_check.MOVIE_REFERENCE_DOWNLOADERS,
                           {"subtitlecat": dead, "yifysubtitles": works}), \
                patch.dict(identity_check._IDENTITY["bazarr"],
                           {"movie_reference_providers": ["subtitlecat", "yifysubtitles"]}):
            references = list(identity_check.iter_movie_references(identity, 90))
        self.assertEqual(tried, ["subtitlecat", "yifysubtitles"])
        self.assertIs(references[0][1], yify)

    def test_unlisted_direct_provider_is_never_used(self):
        yify = self.candidate(
            "yifysubtitles", 99, "https://yifysubtitles.ch/subtitles/film-english-yify-1",
        )
        identity = {
            "reference_language": {"name": "English", "code2": "en", "code3": "eng"},
            "scene_name": None,
        }
        with patch.object(identity_check, "bazarr_movie_candidates", return_value=[yify]), \
                patch.dict(identity_check._IDENTITY["bazarr"],
                           {"movie_reference_providers": ["subtitlecat"]}):
            self.assertEqual(
                list(identity_check.iter_movie_references(identity, 90)),
                [],
            )

    def test_yify_rejects_other_hosts(self):
        with self.assertRaises(RuntimeError):
            identity_check.download_yify_reference({"url": "https://evil.example/subtitles/x"})


class SubtitleCatDownloadUrlTests(unittest.TestCase):
    def test_spaces_in_download_link_are_encoded(self):
        page = b'<a id="download_en" href="/subs/1044/Toy Story 2 (1999).eng-en.srt">x</a>'
        requested = []

        class Response:
            def __init__(self, url, body):
                self.url, self.body = url, body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def geturl(self): return self.url
            def read(self, n): return self.body

        def urlopen(req, timeout=None):
            requested.append(req.full_url)
            if req.full_url.endswith(".html"):
                return Response(req.full_url, page)
            return Response(req.full_url, timed_subtitle(60).encode())

        with patch.object(identity_check.urllib.request, "urlopen", urlopen):
            identity_check.download_subtitlecat_reference(
                {"url": "https://www.subtitlecat.com/subs/1044/toy.html", "language": "en"})
        self.assertEqual(requested[1],
                         "https://www.subtitlecat.com/subs/1044/Toy%20Story%202%20%281999%29.eng-en.srt")


class MissingBazarrEpisodeTests(unittest.TestCase):
    identity = {
        "series_id": 37,
        "episode_id": 4845,
        "reference_language": {"name": "English", "code2": "en", "code3": "eng"},
    }

    def test_episode_not_found_404_becomes_unverifiable_without_error(self):
        error = urllib.error.HTTPError(
            "http://bazarr/api/providers/episodes", 404, "NOT FOUND", {},
            io.BytesIO(b'"Episode not found"'),
        )

        def request(*args, **kwargs):
            raise error

        @contextlib.contextmanager
        def validation_profile(*args, **kwargs):
            yield request

        with (
            patch.object(identity_check, "bazarr_validation_profile", validation_profile),
            patch.object(identity_check, "validation_profile_name", return_value="test"),
            patch.object(identity_check, "log") as log,
            patch.dict(identity_check._IDENTITY["bazarr"], {"reference_provider": "gestdown"}),
        ):
            self.assertEqual(list(identity_check.iter_episode_references(self.identity)), [])
            self.assertIn("BAZARR_EPISODE_NOT_INDEXED", log.call_args.args[0])

        with (
            patch.object(identity_check, "bazarr_throttled_providers", return_value=[]),
            patch.object(identity_check.ReferenceSearch, "rate_limited", []),
            patch.object(identity_check.ReferenceSearch, "download_failures", 0),
        ):
            with self.assertRaises(SystemExit) as result:
                identity_check.no_reference_verdict("no usable subtitle", is_tv=True)
        self.assertEqual(result.exception.code, identity_check.EXIT_CODES["UNVERIFIABLE"])


if __name__ == "__main__":
    unittest.main()
