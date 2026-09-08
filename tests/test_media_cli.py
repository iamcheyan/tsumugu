import io
import json
import unittest
from unittest.mock import patch

from app.cli import media


class MediaCliTests(unittest.TestCase):
    @patch("app.cli.media.request_json", return_value={"job_id": 7, "status": "queued"})
    def test_submit_emits_json(self, request_json):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            exit_code = media.main(
                [
                    "submit",
                    "https://www.youtube.com/watch?v=abc",
                    "--path",
                    "/Media/music",
                    "--split",
                    "auto",
                ]
            )
        self.assertEqual(exit_code, 0)
        request_json.assert_called_once()
        payload = request_json.call_args.args[2]
        self.assertEqual(payload["save_path"], "/Media/music")
        self.assertEqual(payload["split_policy"], "auto")
        json.loads(stdout.getvalue())

    @patch("app.cli.media.request_json", side_effect=RuntimeError("offline"))
    def test_errors_are_nonzero(self, request_json):
        with patch("sys.stderr"):
            self.assertEqual(media.main(["status", "12"]), 2)


if __name__ == "__main__":
    unittest.main()
