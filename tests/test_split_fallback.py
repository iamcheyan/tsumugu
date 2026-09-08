import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.audio_splitter import SplitResult
from app.download_manager import DownloadTask, download_manager


class SplitFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_automated_chapter_split_falls_back_to_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "mix.mp3"
            source.write_bytes(b"source")
            task = DownloadTask(
                id=1,
                url="https://youtube.com/watch?v=test",
                title="mix",
                save_path=tmp,
                split_mode="chapter_info",
                auto_split_fallback=True,
            )
            responses = [
                SplitResult(False, [], "No chapter information found"),
                SplitResult(True, [{"file_path": str(Path(tmp) / "Track 01.mp3")}]),
            ]
            with patch("app.download_manager.audio_splitter.split_audio", new=AsyncMock(side_effect=responses)) as split:
                with patch.object(download_manager, "_broadcast_progress", new=AsyncMock()):
                    with patch.object(download_manager, "_find_downloaded_file", return_value=str(source)):
                        await download_manager._split_audio(task)
            self.assertEqual([call.kwargs["split_mode"] for call in split.await_args_list], ["chapter_info", "silence_detection"])


if __name__ == "__main__":
    unittest.main()
