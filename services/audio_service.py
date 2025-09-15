import subprocess
import os
import time
from typing import Optional
from abc import ABC, abstractmethod
from config import Config
from utils.logger import get_logger

logger = get_logger(__name__)


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

    def _has_audio_stream(self, video_path: str) -> bool:
        """Check if the video file has an audio stream using ffprobe."""
        try:
            cmd = [
                self.ffprobe_path,
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout.strip() == "audio"
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def extract_audio(self, video_path: str, output_path: str) -> bool:
        """
        Extract audio from video using FFmpeg.
        
        Args:
            video_path: Path to the input video file
            output_path: Path where the audio file should be saved
            
        Returns:
            bool: True if extraction was successful, False otherwise
        """
        start_time = time.time()
        logger.info(f"Starting audio extraction from {video_path} to {output_path}")

        if not self._has_audio_stream(video_path):
            logger.warning(f"No audio stream found in {video_path}. Skipping extraction.")
            return False
        
        try:
            # Create output directory if it doesn't exist
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                logger.debug(f"Created output directory: {output_dir}")
            
            # FFmpeg command to extract audio
            cmd = [
                self.ffmpeg_path,
                "-i", video_path,
                "-vn",  # No video
                "-ac", "1",  # Mono audio
                "-ar", "16000",  # 16kHz sample rate
                "-y",  # Overwrite output file
                output_path
            ]
            
            logger.debug(f"FFmpeg command: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            
            success = os.path.exists(output_path)
            duration = time.time() - start_time
            
            if success:
                file_size = os.path.getsize(output_path)
                logger.info(f"Audio extraction successful in {duration:.2f}s, output size: {file_size} bytes")
            else:
                logger.error("Audio extraction failed - output file not created")
            
            return success
            
        except subprocess.CalledProcessError as e:
            duration = time.time() - start_time
            logger.error(f"FFmpeg error after {duration:.2f}s: {e}")
            logger.error(f"FFmpeg stderr: {e.stderr}")
            logger.error(f"FFmpeg stdout: {e.stdout}")
            return False
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Unexpected error during audio extraction after {duration:.2f}s: {e}", exc_info=True)
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