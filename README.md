# Video Audio Processor

A Streamlit application that extracts audio from videos, transcribes it using AssemblyAI, and generates AI-powered summaries using OpenRouter.

## Features

- 🎥 **Video Upload**: Support for multiple video formats (MP4, AVI, MOV, MKV, WMV, FLV, WebM)
- ☁️ **Large File Upload via UI**: Browser uploads large files directly to GCS without hitting Cloud Run upload limits
- 🎵 **Audio Extraction**: Uses FFmpeg to extract high-quality audio from videos
- 🎤 **Speech Transcription**: Powered by AssemblyAI with support for multiple languages
- 🤖 **AI Summarization**: Generates intelligent summaries via OpenRouter models
- 📊 **Progress Tracking**: Real-time processing status and statistics
- 📥 **Export Options**: Download transcripts and summaries as text files

## Prerequisites

1. **FFmpeg**: Required for audio extraction
   - Windows: Download from [FFmpeg website](https://ffmpeg.org/download.html) and add to PATH
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`

2. **API Keys**:
   - AssemblyAI API key for transcription
   - OpenRouter API key for text processing

## Installation

1. Clone or download this repository
2. Install Python dependencies (recommended: `uv`):
   ```bash
   uv pip install --system -r requirements.txt
   ```
   Or with pip:
   ```bash
   pip install -r requirements.txt
   ```
   For running tests locally:
   ```bash
   uv pip install --system -r requirements-dev.txt
   # or: pip install -r requirements-dev.txt
   ```
3. Create a `.env` file from the example:
   ```bash
   cp .env_example .env
   ```
4. Edit the `.env` file and add your API keys:
   ```
   ASSEMBLYAI_API_KEY=your_actual_assemblyai_key
   OPENROUTER_API_KEY=your_actual_openrouter_key
   ```

## Usage

1. Run the Streamlit app:
   ```bash
   streamlit run app.py
   ```

2. Open your browser and navigate to the provided URL (usually `http://localhost:8501`)

3. If you've set up your `.env` file, the API keys will be loaded automatically. Otherwise, configure your API keys in the sidebar:
   - Enter your AssemblyAI API key
   - Enter your OpenRouter API key
   - Select transcription language
   - Enter OpenRouter model id (for example: `google/gemini-3-flash-preview`)

4. Choose input source:
   - **Local upload** for small files (recommended <= 32 MB on Cloud Run)
   - **Large file upload** for big files
   - If `GCS_UPLOAD_BUCKET` and `APP_BASE_URL` are configured, use **Prepare secure browser upload** for UI-only large file upload.

5. View the results:
   - Full transcript in the "Full Transcript" tab
   - AI-generated summary in the "AI Summary" tab
   - Download options for both transcript and summary

## Deployment

For the production deployment flow (Podman + Cloud Run + Secret Manager) and troubleshooting notes from real failures, see:

- [DEPLOYMENT.md](DEPLOYMENT.md)

## Project Structure

```
student_ai_assistant/
├── app.py                          # Main Streamlit application
├── config.py                      # Configuration management
├── requirements.txt                # Python dependencies
├── README.md                      # This file
├── .env_example                   # Environment variables template
├── .env                          # Your environment variables (create this)
└── services/                      # Service layer for modularity
    ├── __init__.py
    ├── audio_service.py           # Audio extraction service
    ├── transcription_service.py   # Transcription service
    └── llm_service.py            # LLM processing service
```

## Service Architecture

The application uses a service-based architecture for easy provider switching:

- **AudioService**: Handles audio extraction (currently FFmpeg)
- **TranscriptionService**: Manages transcription providers (currently AssemblyAI)
- **LLMService**: Handles text processing (currently OpenRouter)

Each service uses abstract base classes, making it easy to swap providers in the future.

## Supported Languages

The transcription service supports multiple languages including:
- Russian (ru) - Default
- English (en)
- Spanish (es)
- French (fr)
- German (de)
- Italian (it)
- Portuguese (pt)
- Japanese (ja)
- Korean (ko)
- Chinese (zh)

## Troubleshooting

### FFmpeg Not Found
If you get an error about FFmpeg not being found:
1. Ensure FFmpeg is installed on your system
2. Add FFmpeg to your system PATH
3. Restart your terminal/command prompt

### API Key Issues
- Make sure your API keys are valid and have sufficient credits
- Check that the keys are entered correctly in the sidebar or `.env` file
- If using `.env` file, make sure it's in the same directory as `app.py`

### Large File Processing
- Large video files may take longer to process
- Consider compressing videos before upload for faster processing
- Cloud Run direct upload has request-size limits (413 errors). Use Large file upload mode for large files.

### GCS Permission Issues
If large file upload fails with permission errors:
1. Grant your Cloud Run runtime service account read access to the bucket/object.
2. Example:
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://MY_BUCKET \
     --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
     --role="roles/storage.objectViewer"
   ```

## License

This project is open source and available under the MIT License.
