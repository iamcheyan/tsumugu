import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.audio_splitter import AudioSplitter
from app.media.naming import sanitize_component
from app.media.policy import choose_split_policy


class MediaCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_chapter_split_uses_number_and_title_without_ffmpeg(self):
        splitter = AudioSplitter()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "long mix.mp3"
            source.write_bytes(b"source")
            with patch.object(splitter, "_get_audio_info", new=AsyncMock(return_value={"format": {"duration": "40"}})), patch.object(
                splitter, "_extract_segment", new=AsyncMock(return_value=True)
            ):
                result = await splitter.split_audio(
                    str(source),
                    "chapter_info",
                    keep_original=True,
                    chapters=[
                        {"title": "01: First / Song", "start_time": 0, "end_time": 20},
                        {"title": "Second", "start_time": 20, "end_time": 40},
                    ],
                )

            self.assertTrue(result.success)
            self.assertEqual(len(result.files), 2)
            self.assertEqual(
                Path(result.files[0]["file_path"]).name,
                "long mix - Track 01 - 01 First Song.mp3",
            )
            self.assertEqual(result.files[0]["track_number"], 1)

    async def test_split_rejects_unknown_output_format(self):
        splitter = AudioSplitter()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "song.mp3"
            source.write_bytes(b"source")
            result = await splitter.split_audio(str(source), "chapter_info", output_format="ogg")
        self.assertFalse(result.success)
        self.assertIn("Unsupported output format", result.error or "")

    def test_automation_policy(self):
        self.assertEqual(choose_split_policy(title="My Full Album Mix", duration=60), "chapter_info")
        self.assertIsNone(choose_split_policy(title="One Song", duration=180))
        self.assertEqual(choose_split_policy(title="One Song", duration=180, requested="auto"), None)

    def test_names_are_single_safe_components(self):
        self.assertEqual(sanitize_component("../A/B: song"), "AB song")
        self.assertEqual(sanitize_component("..."), "untitled")


if __name__ == "__main__":
    unittest.main()
