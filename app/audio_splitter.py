"""
Audio Splitter - Handles splitting audio files using chapter info or silence detection
"""
import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ChapterInfo:
    """Represents a chapter/track in an audio file."""

    title: str
    start_time: float
    end_time: float
    track_number: int


@dataclass
class SplitResult:
    """Result of splitting an audio file."""

    success: bool
    files: List[Dict[str, Any]]
    error: Optional[str] = None


class AudioSplitter:
    """Handles splitting audio files using chapter metadata or silence."""

    _CODECS = {
        "mp3": ("libmp3lame", ["-q:a", "2"]),
        "m4a": ("aac", ["-b:a", "192k"]),
        "flac": ("flac", []),
    }

    def __init__(self):
        self.silence_threshold = "-30dB"
        self.silence_duration = 0.5
        self.min_track_duration = 10.0

    async def split_audio(
        self,
        audio_file_path: str,
        split_mode: str,
        keep_original: bool = True,
        output_dir: Optional[str] = None,
        chapters: Optional[List[Dict]] = None,
        output_format: str = "mp3",
    ) -> SplitResult:
        """Split an audio file into individual tracks."""
        if not os.path.exists(audio_file_path):
            return SplitResult(False, [], f"Audio file not found: {audio_file_path}")
        if output_format not in self._CODECS:
            return SplitResult(False, [], f"Unsupported output format: {output_format}")

        output_dir = output_dir or os.path.dirname(audio_file_path)
        os.makedirs(output_dir, exist_ok=True)
        if not await self._get_audio_info(audio_file_path):
            return SplitResult(False, [], "Failed to get audio file info")

        if split_mode == "chapter_info":
            return await self._split_by_chapters(
                audio_file_path, output_dir, chapters, keep_original, output_format
            )
        if split_mode == "silence_detection":
            return await self._split_by_silence(
                audio_file_path, output_dir, keep_original, output_format
            )
        return SplitResult(False, [], f"Unknown split mode: {split_mode}")

    async def _get_audio_info(self, audio_file_path: str) -> Optional[Dict]:
        """Get audio file information using ffprobe."""
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                audio_file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            if process.returncode != 0:
                return None
            return json.loads(stdout.decode())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error getting audio info: {exc}")
            return None

    async def _split_by_chapters(
        self,
        audio_file_path: str,
        output_dir: str,
        chapters: Optional[List[Dict]],
        keep_original: bool,
        output_format: str,
    ) -> SplitResult:
        """Split audio file using chapter metadata."""
        if not chapters:
            chapters = await self._extract_chapters_from_file(audio_file_path)
        if not chapters:
            return SplitResult(False, [], "No chapter information found in the audio file")

        chapter_infos: List[ChapterInfo] = []
        for index, chapter in enumerate(chapters):
            start_time = float(chapter.get("start_time", 0))
            end_time = float(chapter.get("end_time", 0))
            title = self._sanitize_filename(chapter.get("title", f"Track {index + 1}"))
            if end_time - start_time >= self.min_track_duration:
                chapter_infos.append(ChapterInfo(title, start_time, end_time, index + 1))

        if not chapter_infos:
            return SplitResult(False, [], "No valid chapters found (all chapters too short)")

        split_files: List[Dict[str, Any]] = []
        base_name = os.path.splitext(os.path.basename(audio_file_path))[0]
        for chapter_info in chapter_infos:
            output_filename = (
                f"{base_name} - Track {chapter_info.track_number:02d} - "
                f"{chapter_info.title}.{output_format}"
            )
            output_path = os.path.join(output_dir, output_filename)
            if await self._extract_segment(
                audio_file_path,
                output_path,
                chapter_info.start_time,
                chapter_info.end_time,
                output_format,
            ):
                split_files.append(
                    {
                        "file_path": output_path,
                        "title": chapter_info.title,
                        "duration": int(chapter_info.end_time - chapter_info.start_time),
                        "track_number": chapter_info.track_number,
                    }
                )

        self._remove_original_if_requested(audio_file_path, keep_original, split_files)
        return SplitResult(
            bool(split_files),
            split_files,
            None if split_files else "Failed to split any tracks",
        )

    async def _split_by_silence(
        self,
        audio_file_path: str,
        output_dir: str,
        keep_original: bool,
        output_format: str,
    ) -> SplitResult:
        """Split audio file using silence detection."""
        silence_points = await self._detect_silence(audio_file_path)
        if not silence_points:
            return SplitResult(False, [], "No silence points detected for splitting")

        audio_info = await self._get_audio_info(audio_file_path)
        duration = float((audio_info or {}).get("format", {}).get("duration", 0))
        if duration <= 0:
            return SplitResult(False, [], "Invalid audio duration")

        segments = []
        start_time = 0.0
        for silence_end in silence_points:
            if silence_end > duration:
                break
            if silence_end - start_time >= self.min_track_duration:
                segments.append((start_time, silence_end))
            start_time = silence_end
        if duration - start_time >= self.min_track_duration:
            segments.append((start_time, duration))
        if not segments:
            return SplitResult(False, [], "No segments long enough to split")

        split_files: List[Dict[str, Any]] = []
        base_name = os.path.splitext(os.path.basename(audio_file_path))[0]
        for index, (start, end) in enumerate(segments, start=1):
            output_filename = f"{base_name} - Track {index:02d}.{output_format}"
            output_path = os.path.join(output_dir, output_filename)
            if await self._extract_segment(audio_file_path, output_path, start, end, output_format):
                split_files.append(
                    {
                        "file_path": output_path,
                        "title": f"Track {index}",
                        "duration": int(end - start),
                        "track_number": index,
                    }
                )

        self._remove_original_if_requested(audio_file_path, keep_original, split_files)
        return SplitResult(
            bool(split_files),
            split_files,
            None if split_files else "Failed to split any tracks",
        )

    async def _extract_chapters_from_file(self, audio_file_path: str) -> Optional[List[Dict]]:
        """Extract chapter information from audio file metadata."""
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_chapters",
                audio_file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            if process.returncode != 0:
                return None
            data = json.loads(stdout.decode())
            chapters = data.get("chapters", [])
            if not chapters:
                return None
            return [
                {
                    "title": chapter.get("tags", {}).get("title", f"Chapter {index + 1}"),
                    "start_time": float(chapter.get("start_time", 0)),
                    "end_time": float(chapter.get("end_time", 0)),
                }
                for index, chapter in enumerate(chapters)
            ]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"Error extracting chapters: {exc}")
            return None

    async def _detect_silence(self, audio_file_path: str) -> List[float]:
        """Detect silence points in audio file using ffmpeg."""
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i",
                audio_file_path,
                "-af",
                f"silencedetect=noise={self.silence_threshold}:d={self.silence_duration}",
                "-f",
                "null",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            return [float(value) for value in re.findall(r"silence_end: ([\d.]+)", stderr.decode())]
        except (OSError, ValueError) as exc:
            print(f"Error detecting silence: {exc}")
            return []

    async def _extract_segment(
        self,
        input_path: str,
        output_path: str,
        start_time: float,
        end_time: float,
        output_format: str = "mp3",
    ) -> bool:
        """Extract one segment using the requested audio format."""
        try:
            codec_name, codec_args = self._CODECS[output_format]
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i",
                input_path,
                "-ss",
                str(start_time),
                "-t",
                str(end_time - start_time),
                "-c:a",
                codec_name,
                *codec_args,
                "-y",
                output_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            return process.returncode == 0
        except (KeyError, OSError, ValueError) as exc:
            print(f"Error extracting segment: {exc}")
            return False

    @staticmethod
    def _remove_original_if_requested(
        audio_file_path: str,
        keep_original: bool,
        split_files: List[Dict[str, Any]],
    ) -> None:
        if keep_original or not split_files:
            return
        try:
            os.remove(audio_file_path)
        except OSError as exc:
            print(f"Warning: Failed to remove original file: {exc}")

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        sanitized = re.sub(r'[<>:"/\\|?*]', "", str(filename or ""))
        sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
        return (sanitized[:100].rstrip(" .") or "Track").replace("..", ".")


# Global audio splitter instance
audio_splitter = AudioSplitter()
