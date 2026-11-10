import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from importlib.machinery import SourceFileLoader
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_loader(
    "mediacheck_command",
    SourceFileLoader("mediacheck_command", str(HERE / "mediacheck")),
)
mediacheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mediacheck)


class MissingBazarrReferenceTests(unittest.TestCase):
    def test_run_identity_checks_subtitles_before_waiting_for_whisper(self):
        response = type("Result", (), {
            "returncode": 20,
            "stdout": "VERDICT: UNVERIFIABLE - no usable subtitle\n",
            "stderr": "",
        })()
        with (
            patch.object(mediacheck.subprocess, "run", return_value=response),
            patch.object(mediacheck, "wait_for_whisper") as wait,
        ):
            result, detail, _ = mediacheck.run_identity("/library/example.mkv", True)
        self.assertEqual(result, "UNVERIFIABLE")
        self.assertIn("no usable subtitle", detail)
        wait.assert_not_called()

    def test_hook_does_not_wait_for_whisper_before_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            queue = Path(root) / "queue"
            inprogress = Path(root) / "inprogress"
            deferred = Path(root) / "deferred"
            queue.mkdir()
            (queue / "test.job").write_text("app=sonarr\npath=/media/example.mkv\n")
            cfg = {
                "hook_queues": [str(queue)],
                "identity": {"on_import": True, "use_audio": True},
            }
            with (
                patch.object(mediacheck, "HOOK_INPROGRESS", str(inprogress)),
                patch.object(mediacheck, "HOOK_DEFERRED", str(deferred)),
                patch.object(mediacheck, "lock", return_value=object()),
                patch.object(mediacheck, "recover_interrupted_jobs"),
                patch.object(mediacheck, "load_state", return_value={"files": {}}),
                patch.object(mediacheck, "housekeeping"),
                patch.object(mediacheck, "due_deferred_jobs", return_value=([], False)),
                patch.object(mediacheck, "hook_job", return_value=("UNVERIFIABLE", None)) as hook,
                patch.object(mediacheck, "save_state"),
                patch.object(mediacheck, "wait_for_whisper") as wait,
            ):
                mediacheck.cmd_hook(None, cfg)
            hook.assert_called_once()
            wait.assert_not_called()

    def run_identity_outcome(self, detail):
        state = {"files": {}}
        with (
            patch.object(mediacheck, "run_identity", return_value=("ERROR", detail, "")),
            patch.object(mediacheck, "log"),
            patch.object(mediacheck, "flag_identity_review") as flag_review,
        ):
            result = mediacheck.identity_stage("/library/example.mkv", state, {}, True)
        return result, state["files"]["/library/example.mkv"], flag_review

    def test_no_subtitle_reference_is_silent_and_accepts_import(self):
        state = {"files": {}}
        with (
            patch.object(
                mediacheck,
                "run_identity",
                return_value=(
                    "UNVERIFIABLE",
                    "UNVERIFIABLE - no usable exact-episode English subtitle from any provider",
                    "",
                ),
            ),
            patch.object(mediacheck, "log"),
            patch.object(mediacheck, "flag_identity_review") as flag_review,
        ):
            result = mediacheck.identity_stage(
                "/library/example.mkv", state, {}, True,
            )
        self.assertEqual(result, "UNVERIFIABLE")
        self.assertEqual(
            state["files"]["/library/example.mkv"]["identity"]["result"],
            "UNVERIFIABLE",
        )
        self.assertNotIn("review", state["files"]["/library/example.mkv"])
        flag_review.assert_not_called()

    def test_missing_bazarr_episode_record_is_silent_and_unverifiable(self):
        result, entry, flag_review = self.run_identity_outcome(
            "ERROR - RuntimeError: Bazarr returned 0 series for Sonarr ID 37"
        )
        self.assertEqual(result, "UNVERIFIABLE")
        self.assertEqual(entry["identity"]["result"], "UNVERIFIABLE")
        self.assertNotIn("review", entry)
        self.assertNotIn("identity_attempts", entry)
        flag_review.assert_not_called()

    def test_missing_bazarr_episode_http_404_is_silent_and_withdraws_stale_review(self):
        state = {"files": {"/library/example.mkv": {
            "identity_review_id": "MC-12345678",
            "identity_review_notified": True,
            "identity_attempts": 1,
            "review": "old false error",
        }}}
        output = (
            "WARNING: BAZARR_EPISODE_NOT_INDEXED: Bazarr has no episode record\n"
            "VERDICT: UNVERIFIABLE - no usable exact-episode English subtitle\n"
        )
        with (
            patch.object(
                mediacheck, "run_identity",
                return_value=("UNVERIFIABLE", "no usable exact-episode English subtitle", output),
            ),
            patch.object(mediacheck, "log"),
            patch.object(mediacheck.mclib, "withdraw_identity_review") as withdraw,
            patch.object(mediacheck, "flag_identity_review") as flag_review,
        ):
            result = mediacheck.identity_stage(
                "/library/example.mkv", state, {}, True,
            )
        entry = state["files"]["/library/example.mkv"]
        self.assertEqual(result, "UNVERIFIABLE")
        self.assertNotIn("review", entry)
        self.assertNotIn("identity_review_id", entry)
        self.assertNotIn("identity_attempts", entry)
        withdraw.assert_called_once()
        flag_review.assert_not_called()

    def test_missing_bazarr_movie_record_is_silent_and_unverifiable(self):
        result, entry, flag_review = self.run_identity_outcome(
            "ERROR - RuntimeError: Bazarr has no record for Radarr movie 23"
        )
        self.assertEqual(result, "UNVERIFIABLE")
        self.assertNotIn("review", entry)
        flag_review.assert_not_called()

    def test_unrelated_identity_errors_still_request_review(self):
        result, entry, flag_review = self.run_identity_outcome(
            "ERROR - RuntimeError: Bazarr profile was not found"
        )
        self.assertEqual(result, "UNVERIFIABLE")
        self.assertEqual(entry["identity_attempts"], 1)
        flag_review.assert_called_once()


if __name__ == "__main__":
    unittest.main()
