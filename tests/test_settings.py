import unittest
from unittest.mock import patch

from app.nas_mount import _get_mounted_username
from app.settings import get_settings


class SettingsTests(unittest.TestCase):
    def test_shared_config_contains_media_nas_defaults(self):
        settings = get_settings()
        self.assertEqual(settings.nas.username, "media")
        self.assertEqual(settings.nas.share, "NAS")
        self.assertEqual(settings.media.default_path, "/Media/music")
        self.assertEqual(settings.media.default_format, "mp3")
        self.assertEqual(settings.media.split_policy, "auto")

    @patch(
        "app.nas_mount.subprocess.run",
        return_value=type("Result", (), {"returncode": 0, "stdout": "rw,username=media,vers=3.0"})(),
    )
    def test_mounted_username_is_read_without_password(self, run):
        self.assertEqual(_get_mounted_username("/tmp/nas_mnt/NAS"), "media")
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
