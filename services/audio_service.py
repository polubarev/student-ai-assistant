import subprocess
import os
from typing import Optional
from abc import ABC, abstractmethod
from config import Config


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
    
    def extract_audio(self, video_path: str, output_path: str) -> bool:
        """
        Extract audio from video using FFmpeg.
        
        Args:
            video_path: Path to the input video file
            output_path: Path where the audio file should be saved
            
        Returns:
            bool: True if extraction was successful, False otherwise
        """
        try:
            # Create output directory if it doesn't exist
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
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
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            
            return os.path.exists(output_path)
            
        except subprocess.CalledProcessError as e:
            print(f"FFmpeg error: {e}")
            print(f"FFmpeg stderr: {e.stderr}")
            return False
        except Exception as e:
            print(f"Unexpected error during audio extraction: {e}")
            return False


class AudioService:
    """Main audio service that manages audio extraction."""
    
    def __init__(self, extractor: Optional[AudioExtractor] = None):
        self.extractor = extractor or FFmpegAudioExtractor()
    
    def extract_audio_from_video(self, video_path: str, output_path: str) -> bool:
        """
        Extract audio from video file.
        
        Args:
            video_path: Path to the input video file
            output_path: Path where the audio file should be saved
            
        Returns:
            bool: True if extraction was successful, False otherwise
        """
        return self.extractor.extract_audio(video_path, output_path)
