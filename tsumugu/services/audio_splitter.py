"""
Audio Splitter - Handles splitting audio files using chapter info or silence detection
"""
import asyncio
import os
import re
import subprocess
import json
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass
import struct


@dataclass
class ChapterInfo:
    """Represents a chapter/track in an audio file"""
    title: str
    start_time: float  # in seconds
    end_time: float  # in seconds
    track_number: int


@dataclass
class SplitResult:
    """Result of splitting an audio file"""
    success: bool
    files: List[Dict[str, Any]]  # List of {file_path, title, duration}
    error: Optional[str] = None


class AudioSplitter:
    """Handles audio file splitting using various methods"""
    
    def __init__(self):
        # ffmpeg silence detection parameters
        self.silence_threshold = "-30dB"  # Silence threshold in dB
        self.silence_duration = 0.5  # Minimum silence duration in seconds
        self.min_track_duration = 10.0  # Minimum track duration in seconds
    
    async def split_audio(
        self,
        audio_file_path: str,
        split_mode: str,
        keep_original: bool = True,
        output_dir: Optional[str] = None,
        chapters: Optional[List[Dict]] = None
    ) -> SplitResult:
        """
        Split an audio file into individual tracks.
        
        Args:
            audio_file_path: Path to the audio file to split
            split_mode: 'chapter_info' or 'silence_detection'
            keep_original: Whether to keep the original unsplit file
            output_dir: Directory to save split files (default: same as input)
            chapters: Chapter metadata from yt-dlp (for chapter_info mode)
        
        Returns:
            SplitResult with list of split files
        """
        if not os.path.exists(audio_file_path):
            return SplitResult(
                success=False,
                files=[],
                error=f"Audio file not found: {audio_file_path}"
            )
        
        # Determine output directory
        if output_dir is None:
            output_dir = os.path.dirname(audio_file_path)
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Get audio file info
        audio_info = await self._get_audio_info(audio_file_path)
        if not audio_info:
            return SplitResult(
                success=False,
                files=[],
                error="Failed to get audio file info"
            )
        
        # Split based on mode
        if split_mode == "chapter_info":
            return await self._split_by_chapters(
                audio_file_path, output_dir, chapters, keep_original
            )
        elif split_mode == "silence_detection":
            return await self._split_by_silence(
                audio_file_path, output_dir, keep_original
            )
        else:
            return SplitResult(
                success=False,
                files=[],
                error=f"Unknown split mode: {split_mode}"
            )
    
    async def _get_audio_info(self, audio_file_path: str) -> Optional[Dict]:
        """Get audio file information using ffprobe"""
        try:
            cmd = [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                audio_file_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                return None
            
            return json.loads(stdout.decode())
            
        except Exception as e:
            print(f"Error getting audio info: {e}")
            return None
    
    async def _split_by_chapters(
        self,
        audio_file_path: str,
        output_dir: str,
        chapters: Optional[List[Dict]],
        keep_original: bool
    ) -> SplitResult:
        """Split audio file using chapter metadata"""
        
        # If no chapters provided, try to extract from file
        if not chapters:
            chapters = await self._extract_chapters_from_file(audio_file_path)
        
        if not chapters:
            return SplitResult(
                success=False,
                files=[],
                error="No chapter information found in the audio file"
            )
        
        # Convert chapters to ChapterInfo objects
        chapter_infos = []
        for i, chapter in enumerate(chapters):
            start_time = chapter.get("start_time", 0)
            end_time = chapter.get("end_time", 0)
            title = chapter.get("title", f"Track {i + 1}")
            
            # Skip chapters that are too short
            if end_time - start_time < self.min_track_duration:
                continue
            
            chapter_infos.append(ChapterInfo(
                title=self._sanitize_filename(title),
                start_time=start_time,
                end_time=end_time,
                track_number=i + 1
            ))
        
        if not chapter_infos:
            return SplitResult(
                success=False,
                files=[],
                error="No valid chapters found (all chapters too short)"
            )
        
        # Split the audio file
        split_files = []
        base_name = os.path.splitext(os.path.basename(audio_file_path))[0]
        
        for chapter_info in chapter_infos:
            output_filename = f"{base_name} - {chapter_info.title:02d}.mp3"
            output_path = os.path.join(output_dir, output_filename)
            
            success = await self._extract_segment(
                audio_file_path,
                output_path,
                chapter_info.start_time,
                chapter_info.end_time
            )
            
            if success:
                duration = chapter_info.end_time - chapter_info.start_time
                split_files.append({
                    "file_path": output_path,
                    "title": chapter_info.title,
                    "duration": int(duration),
                    "track_number": chapter_info.track_number
                })
        
        # Remove original if requested
        if not keep_original and split_files:
            try:
                os.remove(audio_file_path)
            except Exception as e:
                print(f"Warning: Failed to remove original file: {e}")
        
        return SplitResult(
            success=len(split_files) > 0,
            files=split_files,
            error=None if split_files else "Failed to split any tracks"
        )
    
    async def _split_by_silence(
        self,
        audio_file_path: str,
        output_dir: str,
        keep_original: bool
    ) -> SplitResult:
        """Split audio file using silence detection"""
        
        # Detect silence points
        silence_points = await self._detect_silence(audio_file_path)
        
        if not silence_points:
            return SplitResult(
                success=False,
                files=[],
                error="No silence points detected for splitting"
            )
        
        # Get audio duration
        audio_info = await self._get_audio_info(audio_file_path)
        if not audio_info:
            return SplitResult(
                success=False,
                files=[],
                error="Failed to get audio duration"
            )
        
        duration = float(audio_info.get("format", {}).get("duration", 0))
        if duration <= 0:
            return SplitResult(
                success=False,
                files=[],
                error="Invalid audio duration"
            )
        
        # Create segments from silence points
        segments = []
        start_time = 0.0
        
        for silence_end in silence_points:
            if silence_end - start_time >= self.min_track_duration:
                segments.append((start_time, silence_end))
            start_time = silence_end
        
        # Add final segment if it's long enough
        if duration - start_time >= self.min_track_duration:
            segments.append((start_time, duration))
        
        if not segments:
            return SplitResult(
                success=False,
                files=[],
                error="No segments long enough to split"
            )
        
        # Split the audio file
        split_files = []
        base_name = os.path.splitext(os.path.basename(audio_file_path))[0]
        
        for i, (start, end) in enumerate(segments):
            output_filename = f"{base_name} - Track {i + 1:02d}.mp3"
            output_path = os.path.join(output_dir, output_filename)
            
            success = await self._extract_segment(
                audio_file_path,
                output_path,
                start,
                end
            )
            
            if success:
                split_files.append({
                    "file_path": output_path,
                    "title": f"Track {i + 1}",
                    "duration": int(end - start),
                    "track_number": i + 1
                })
        
        # Remove original if requested
        if not keep_original and split_files:
            try:
                os.remove(audio_file_path)
            except Exception as e:
                print(f"Warning: Failed to remove original file: {e}")
        
        return SplitResult(
            success=len(split_files) > 0,
            files=split_files,
            error=None if split_files else "Failed to split any tracks"
        )
    
    async def _extract_chapters_from_file(self, audio_file_path: str) -> Optional[List[Dict]]:
        """Extract chapter information from audio file metadata"""
        try:
            cmd = [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_chapters",
                audio_file_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                return None
            
            data = json.loads(stdout.decode())
            chapters = data.get("chapters", [])
            
            if not chapters:
                return None
            
            # Convert to our format
            chapter_list: List[Dict[str, Any]] = []
            for chapter in chapters:
                start_time = float(chapter.get("start_time", 0))
                end_time = float(chapter.get("end_time", 0))
                tags = chapter.get("tags", {})
                title = tags.get("title", f"Chapter {len(chapter_list) + 1}")
                
                chapter_list.append({
                    "title": title,
                    "start_time": start_time,
                    "end_time": end_time
                })
            
            return chapter_list
            
        except Exception as e:
            print(f"Error extracting chapters: {e}")
            return None
    
    async def _detect_silence(self, audio_file_path: str) -> List[float]:
        """Detect silence points in audio file using ffmpeg"""
        try:
            cmd = [
                "ffmpeg",
                "-i", audio_file_path,
                "-af", f"silencedetect=noise={self.silence_threshold}:d={self.silence_duration}",
                "-f", "null",
                "-"
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            # Parse stderr for silence detection
            stderr_text = stderr.decode()
            
            # Find silence_end times
            silence_points = []
            pattern = r"silence_end: ([\d.]+)"
            matches = re.findall(pattern, stderr_text)
            
            for match in matches:
                try:
                    time = float(match)
                    silence_points.append(time)
                except ValueError:
                    continue
            
            return silence_points
            
        except Exception as e:
            print(f"Error detecting silence: {e}")
            return []
    
    async def _extract_segment(
        self,
        input_path: str,
        output_path: str,
        start_time: float,
        end_time: float
    ) -> bool:
        """Extract a segment from an audio file"""
        try:
            duration = end_time - start_time
            
            cmd = [
                "ffmpeg",
                "-i", input_path,
                "-ss", str(start_time),
                "-t", str(duration),
                "-c:a", "libmp3lame",
                "-q:a", "2",  # High quality VBR
                "-y",  # Overwrite output
                output_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            return process.returncode == 0
            
        except Exception as e:
            print(f"Error extracting segment: {e}")
            return False
    
    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename by removing invalid characters"""
        # Remove invalid characters
        sanitized = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Replace multiple spaces with single space
        sanitized = re.sub(r'\s+', ' ', sanitized).strip()
        # Truncate if too long
        if len(sanitized) > 100:
            sanitized = sanitized[:100]
        return sanitized


# Global audio splitter instance
audio_splitter = AudioSplitter()