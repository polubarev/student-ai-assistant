import subprocess
import os
import time
import json
import math
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from abc import ABC, abstractmethod
from config import Config
from utils.logger import get_logger

logger = get_logger(__name__)


class MediaValidationError(ValueError):
    """A media validation failure safe to show to the user."""


class AudioExtractor(ABC):
    """Abstract base class for audio extraction services."""
    
    @abstractmethod
    def extract_audio(self, video_path: str, output_path: str) -> bool:
        """
        Extract audio from video file.
        
        Args:
            video_path: Path to the input video file
            output_path: Path where the audio file should be saved
            
        Returns:
            bool: True if extraction was successful, False otherwise
        """
        pass


class FFmpegAudioExtractor(AudioExtractor):
    """Audio extraction service using FFmpeg."""
    
    def __init__(self):
        self.ffmpeg_path = Config.FFMPEG_PATH
        self.ffprobe_path = self.ffmpeg_path.replace("ffmpeg", "ffprobe")
        logger.info(f"FFmpegAudioExtractor initialized with ffmpeg path: {self.ffmpeg_path} and ffprobe path: {self.ffprobe_path}")

    def validate_audio(self, path: str) -> bool:
        """Allow local media demuxers only and reject unknown or excessive duration."""
        try:
            self._validate_media(path)
            return True
        except MediaValidationError:
            return False

    def require_valid_audio(self, path):
        self._validate_media(path)

    def _validate_media(self, path, *, remote=False):
        if not remote:
            try:
                size = Path(path).stat().st_size
            except OSError:
                raise MediaValidationError("Аудиофайл недоступен. Загрузите его снова.") from None
            if not 0 < size <= Config.MAX_AUDIO_BYTES:
                raise MediaValidationError("Аудиофайл превышает допустимый размер. Используйте «Большая загрузка».")
        cmd = [
            self.ffprobe_path, "-v", "error", "-protocol_whitelist", "https,tls,tcp" if remote else "file,pipe",
            "-format_whitelist", "mov,matroska,webm,avi,asf,flv,ogg,wav,mp3,aac,flac",
            "-select_streams", "a:0", "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60 if remote else 15)
            info = json.loads(result.stdout)
            duration = float(info.get("format", {}).get("duration", 0))
            if not any(stream.get("codec_type") == "audio" for stream in info.get("streams", [])):
                raise MediaValidationError("В файле не найдена аудиодорожка.")
            if not math.isfinite(duration) or duration <= 0:
                raise MediaValidationError("Не удалось определить длительность аудио. Попробуйте MP3, WAV или M4A.")
            # MP3 encoders add a fraction of a second of padding at the boundary.
            if duration > Config.MAX_AUDIO_SECONDS + 0.25:
                raise MediaValidationError(f"Длительность аудио {duration / 3600:.1f} ч превышает лимит {Config.MAX_AUDIO_SECONDS / 3600:g} ч. Разделите лекцию на части.")
            return duration
        except MediaValidationError:
            raise
        except (subprocess.SubprocessError, OSError, ValueError):
            raise MediaValidationError("Не удалось прочитать аудио. Проверьте формат файла (MP3, WAV, M4A).") from None

    def extract_cloud_audio(self, signed_url, output_path):
        """Stream only an application-issued GCS URL into bounded, compressed audio."""
        url = urlparse(signed_url)
        bucket = (Config.GCS_UPLOAD_BUCKET or "").removeprefix("gs://")
        if (url.scheme != "https" or url.hostname != "storage.googleapis.com"
                or not bucket or not url.path.startswith(f"/{bucket}/uploads/")
                or not url.query or url.username or url.password):
            raise MediaValidationError("Загрузка не принадлежит этому приложению.")
        self._validate_media(signed_url, remote=True)
        return self._extract_audio(signed_url, output_path, remote=True)

    def extract_audio(self, video_path: str, output_path: str) -> bool:
        if not self.validate_audio(video_path):
            return False
        return self._extract_audio(video_path, output_path)

    def _extract_audio(self, video_path, output_path, *, remote=False):
        """
        Extract audio from video using FFmpeg.
        
        Args:
            video_path: Path to the input video file
            output_path: Path where the audio file should be saved
            
        Returns:
            bool: True if extraction was successful, False otherwise
        """
        start_time = time.time()
        logger.info("Starting audio extraction (cloud=%s)", remote)
        
        try:
            # Create output directory if it doesn't exist
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                logger.debug(f"Created output directory: {output_dir}")
            
            # FFmpeg command to extract audio
            cmd = [
                self.ffmpeg_path,
                "-v", "error", "-nostdin",
                "-protocol_whitelist", "https,tls,tcp,file,pipe" if remote else "file,pipe",
                "-format_whitelist", "mov,matroska,webm,avi,asf,flv,ogg,wav,mp3,aac,flac",
                "-i", video_path,
                "-t", str(Config.MAX_AUDIO_SECONDS),
                "-fs", str(Config.MAX_AUDIO_BYTES),
                "-vn",  # No video
                "-ac", "1",  # Mono audio
                "-ar", "16000",  # 16kHz sample rate
                *(["-codec:a", "libmp3lame", "-b:a", "32k"] if Path(output_path).suffix == ".mp3" else []),
                "-y",  # Overwrite output file
                output_path
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=Config.MEDIA_PROCESS_TIMEOUT_SECONDS,
            )
            
            success = (os.path.exists(output_path)
                       and 0 < os.path.getsize(output_path) < Config.MAX_AUDIO_BYTES)
            duration = time.time() - start_time
            
            if success:
                file_size = os.path.getsize(output_path)
                logger.info(f"Audio extraction successful in {duration:.2f}s, output size: {file_size} bytes")
            else:
                Path(output_path).unlink(missing_ok=True)
                logger.error("Audio extraction failed - output file not created")
            
            return success
            
        except subprocess.CalledProcessError as e:
            duration = time.time() - start_time
            logger.error("FFmpeg failed after %.2fs", duration)
            Path(output_path).unlink(missing_ok=True)
            return False
        except Exception as e:
            Path(output_path).unlink(missing_ok=True)
            duration = time.time() - start_time
            logger.error("Audio extraction failed after %.2fs: %s", duration, type(e).__name__)
            return False


class AudioService:
    """Main audio service that manages audio extraction."""
    
    def __init__(self, extractor: Optional[AudioExtractor] = None):
        self.extractor = extractor or FFmpegAudioExtractor()
        logger.info(f"AudioService initialized with extractor: {type(self.extractor).__name__}")
    
    def extract_audio_from_video(self, video_path: str, output_path: str) -> bool:
        """
        Extract audio from video file.
        
        Args:
            video_path: Path to the input video file
            output_path: Path where the audio file should be saved
            
        Returns:
            bool: True if extraction was successful, False otherwise
        """
        logger.info(f"AudioService: Starting extraction from {video_path} to {output_path}")
        result = self.extractor.extract_audio(video_path, output_path)
        logger.info(f"AudioService: Extraction result: {result}")
        return result
