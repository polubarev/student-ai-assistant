# Video Audio Processor

A Streamlit application that extracts audio from videos, transcribes it using AssemblyAI, and generates AI-powered summaries using OpenRouter.

## Features

- 🎥 **Video Upload**: Support for multiple video formats (MP4, AVI, MOV, MKV, WMV, FLV, WebM)
- ☁️ **Large File Upload via UI**: Browser uploads large files directly to GCS without hitting Cloud Run upload limits
- 🎵 **Audio Extraction**: Uses FFmpeg to extract high-quality audio from videos
- 🎤 **Speech Transcription**: Powered by AssemblyAI with support for multiple languages
- 🤖 **AI Summarization**: Generates intelligent summaries via OpenRouter models
- 📊 **Progress Tracking**: Real-time processing status and statistics
- 📥 **Export Options**: Download transcripts and summaries as TXT/PDF files

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
   uv pip install --system --require-hashes -r requirements.lock
   ```
   Or with pip:
   ```bash
   pip install --require-hashes -r requirements.lock
   ```
   PDF export uses WeasyPrint and Pango; the production image installs the native
   libraries. For local PDF export, install the platform-specific Pango libraries.
   For running tests locally:
   ```bash
   uv pip install --system --require-hashes -r requirements-dev.lock
   # or: pip install --require-hashes -r requirements-dev.lock
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

5. Reset login passwords using a secure local prompt:
   ```bash
   python -m scripts.reset_passwords
   ```
   Existing local usernames are detected automatically. For new accounts, pass
   usernames as arguments. The command writes Argon2id hashes to ignored
   `secrets/users.json`; plaintext passwords are never printed or saved.
   Set `APP_USERS_FILE=secrets/users.json` in `.env`. Legacy SHA-256 login records
   are rejected, so this step is required before starting the hardened app.

## Usage

1. Run the Streamlit app:
   ```bash
   streamlit run app.py
   ```

2. Open your browser and navigate to the provided URL (usually `http://localhost:8501`)

3. Sign in with the reset credentials. Configure API keys, the model, and the
   system prompt on the server; ordinary users cannot edit those settings.
   Users can select the transcription language.

4. Choose input source:
   - **Local upload** for small files (recommended <= 32 MB on Cloud Run)
   - **Large file upload** for files up to 10 GiB
   - If `GCS_UPLOAD_BUCKET` and `APP_BASE_URL` are configured, use **Prepare secure browser upload** for UI-only large file upload.

5. View the results:
   - Full transcript in the "Full Transcript" tab
   - AI-generated summary in the "AI Summary" tab
   - Download options for both transcript and summary (TXT/PDF)

Security limits: 32 MiB through Streamlit, 10 GiB through GCS, 2 MiB for text,
400,000 transcript characters, and four hours of audio. Cloud media is streamed
through FFmpeg into a bounded mono MP3; the original video is never copied into
the instance's memory-backed temporary storage. Paid processing is limited
to 10 requests per account per hour with one heavy job at a time per process.
Limits reset when the process is replaced; they are not a distributed billing cap.

Transcription saves its provider job ID before waiting. If the 30-second wait
expires, **Check transcription** resumes the same job without submitting or
charging for another job. Errors remain visible and retain the lecture.
After a browser refresh, signing in with the same account restores the latest
lecture checkpoint on the current instance for up to six hours. Start over and
logout discard that checkpoint. Instance replacement still loses checkpoints;
shared durable storage is required for recovery across server restarts.

## Deployment

For the production deployment flow (Podman + Cloud Run + Secret Manager) and troubleshooting notes from real failures, see:

- [DEPLOYMENT.md](DEPLOYMENT.md)

Quick update deploy:
```bash
bash deploy.sh
```

Optional Cloud Run resource overrides (useful for heavy PDF/summarization workloads):
```bash
SERVICE_MEMORY=2Gi SERVICE_CPU=2 SERVICE_TIMEOUT=900 bash deploy.sh
```

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
- Check the server-side `.env` configuration or Secret Manager bindings
- If using `.env` file, make sure it's in the same directory as `app.py`

### Large File Processing
- Large video files may take longer to process
- Consider compressing videos before upload for faster processing
- Cloud Run direct upload has request-size limits (413 errors). Use Large file upload mode for large files.

### GCS Permission Issues
If direct upload fails with permission errors, check the dedicated runtime
identity and the conditional `uploads/` grants configured by `deploy.sh`.
See [DEPLOYMENT.md](DEPLOYMENT.md). Do not grant bucket-wide read access to the
Compute Engine default account as a workaround. Unsupported-size or expired
uploads must be prepared again from the signed-in session.

## License

This project is open source and available under the MIT License.
