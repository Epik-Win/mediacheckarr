import errno
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mclib

loader = importlib.machinery.SourceFileLoader(
    "mediacheck_cli", str(HERE / "mediacheck"),
)
spec = importlib.util.spec_from_loader(loader.name, loader)
mediacheck = importlib.util.module_from_spec(spec)
loader.exec_module(mediacheck)


class MediacheckContainerTests(unittest.TestCase):
    def test_move_job_falls_back_to_copy_across_mounts(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
            source = Path(source_dir) / "job.job"
            target = Path(target_dir) / "job.job"
            source.write_text("path=/media/movies/example.mkv\n")
            real_replace = os.replace
            first_call = True

            def replace_with_cross_device(source_path, target_path):
                nonlocal first_call
                if first_call:
                    first_call = False
                    raise OSError(errno.EXDEV, "cross-device move")
                return real_replace(source_path, target_path)

            with patch.object(mediacheck.os, "replace", side_effect=replace_with_cross_device):
                mediacheck.move_job(str(source), str(target))

            self.assertFalse(source.exists())
            self.assertEqual(target.read_text(), "path=/media/movies/example.mkv\n")

    def test_arr_api_urls_use_container_service_addresses_and_basepaths(self):
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, "mediacheck.yaml").write_text(
                "hook_queues: []\nlibrary_dirs: []\n",
            )
            Path(config_dir, "checkrr.yaml").write_text(
                "arr:\n"
                "  radarr:\n"
                "    address: radarr\n"
                "    port: 7878\n"
                "    baseurl: /radarr/\n"
                "    apikey: test-key\n"
                "    ssl: false\n",
            )

            with patch.object(mclib, "CONFIG_DIR", config_dir):
                config = mclib.load_config()

            self.assertEqual(
                config["arr"]["radarr"]["url"],
                "http://radarr:7878/radarr/api/v3",
            )
            self.assertEqual(config["arr"]["radarr"]["key"], "test-key")

    def test_disabled_identity_does_not_run_on_import(self):
        state = {"files": {}}
        config = {
            "host_media_root": "/media",
            "arr_media_root": "/media",
            "identity": {
                "enabled": False,
                "on_import": True,
                "use_audio": True,
            },
        }
        with (
            patch.object(mediacheck.os.path, "exists", return_value=True),
            patch.object(mediacheck, "fingerprint", return_value={"size": 1}),
            patch.object(mediacheck, "fix_stage", return_value=("ok", "unchanged")),
            patch.object(mediacheck, "identity_stage") as identity_stage,
            patch.object(mediacheck, "log"),
        ):
            mediacheck.hook_job(
                {"app": "radarr", "path": "/media/movie.mkv"},
                state,
                config,
            )

        identity_stage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
