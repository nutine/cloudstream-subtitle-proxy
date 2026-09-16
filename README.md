# CloudStream Subtitle Translation Proxy

A lightweight, dedicated subtitle translation proxy server designed for [CloudStream](https://github.com/recloudstream/cloudstream).

Rather than embedding LLM logic or vendor-specific API calls directly into the mobile video player, this proxy decouples translation from the client:
1. CloudStream sends `GET /translate?url=<sub_url>&target=<language_code>`.
2. The proxy fetches the original subtitle (`.srt` or `.vtt`), translates the cues using Google Gemini (with `thinkingBudget: 0` for sub-second batch latency), and caches the result.
3. The proxy returns standard WebVTT (`text/vtt`) directly to ExoPlayer.

## Features
- **Zero Client Bloat:** Keeps the CloudStream client lean and clean.
- **Fast Batching:** Translates in batches of 50 cues with `thinkingBudget: 0` to eliminate LLM reasoning lag.
- **Failover Chain:** Automatically falls back across models (`gemini-3.5-flash` -> `gemini-2.5-flash` -> `gemini-2.5-flash-lite`).
- **Disk Caching:** Caches translated WebVTT files based on URL and target language hash. Subsequent requests return immediately.
- **Standard WebVTT:** Compatible with any video player that accepts WebVTT URLs.

## Quick Start

### Local Setup
```bash
pip install -r requirements.txt
export GEMINI_API_KEY="your_api_key_here"
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Docker
```bash
docker build -t cloudstream-subtitle-proxy .
docker run -p 8000:8000 -e GEMINI_API_KEY="your_api_key_here" cloudstream-subtitle-proxy
```

## API Usage
- `GET /`: Health check and supported language list.
- `GET /translate?url=<encoded_url>&target=<lang>&key=<optional_gemini_key>`: Returns translated WebVTT subtitle stream.
