import os
import re
import json
import hashlib
from pathlib import Path
from typing import Optional, List, Dict
import httpx
from fastapi import FastAPI, Query, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="CloudStream Subtitle Translation Proxy",
    description="Dedicated translation proxy for CloudStream to translate subtitles on-the-fly and return WebVTT.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "./cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

LANGUAGE_NAMES = {
    "az": "Azerbaijani",
    "tr": "Turkish",
    "en": "English",
    "ru": "Russian",
    "de": "German",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "ar": "Arabic",
    "pt": "Portuguese",
}

def parse_subtitles(content: str) -> List[Dict]:
    """Parses SRT or VTT text into structured cues."""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    # Match standard timestamp patterns: 00:01:23.456 --> 00:01:25.789
    pattern = re.compile(
        r"(?:(\d+)\n)?(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})(?:[^\n]*)?\n((?:(?!\n\n|\n\d+\n|\n\d{1,2}:\d{2}:\d{2}).)+)",
        re.DOTALL
    )
    for match in pattern.finditer(content):
        cue_id = match.group(1) or str(len(cues) + 1)
        start = match.group(2).replace(",", ".")
        end = match.group(3).replace(",", ".")
        text = match.group(4).strip()
        cues.append({
            "id": int(cue_id) if cue_id.isdigit() else len(cues) + 1,
            "start": start,
            "end": end,
            "text": text,
        })
    return cues

async def translate_batch(client: httpx.AsyncClient, texts: List[str], target_lang: str, api_key: str) -> List[str]:
    lang_name = LANGUAGE_NAMES.get(target_lang.lower(), target_lang)
    models = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
    
    prompt = f"""You are a professional subtitle translator.
Translate the following subtitle lines into {lang_name}.
Output JSON format only: a JSON array of strings in the exact same order and length as input.
Input:
{json.dumps(texts, ensure_ascii=False)}"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "thinkingConfig": {
                "thinkingBudget": 0
            }
        }
    }

    last_error = None
    for model in models:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = await client.post(endpoint, json=payload, timeout=60.0)
            if resp.status_code == 200:
                data = resp.json()
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                translated = json.loads(raw_text)
                if isinstance(translated, list) and len(translated) == len(texts):
                    return [str(t) for t in translated]
                elif isinstance(translated, list):
                    res = [str(t) for t in translated]
                    while len(res) < len(texts):
                        res.append(texts[len(res)])
                    return res[:len(texts)]
            else:
                last_error = f"Model {model} returned {resp.status_code}: {resp.text}"
        except Exception as e:
            last_error = str(e)
            continue

    raise HTTPException(status_code=502, detail=f"Translation API failed: {last_error}")

def build_vtt(cues: List[Dict]) -> str:
    lines = ["WEBVTT", ""]
    for cue in cues:
        # Standardize timestamp to 00:00:00.000
        start = cue["start"]
        end = cue["end"]
        if len(start.split(":")[0]) == 1:
            start = "0" + start
        if len(end.split(":")[0]) == 1:
            end = "0" + end
        lines.append(f"{start} --> {end}")
        lines.append(cue["text"])
        lines.append("")
    return "\n".join(lines)

@app.get("/")
def index():
    return {
        "name": "CloudStream Subtitle Translation Proxy",
        "status": "online",
        "supported_languages": LANGUAGE_NAMES,
        "usage": "/translate?url=<subtitle_url>&target=<language_code>"
    }

@app.get("/translate")
async def translate_subtitle(
    url: str = Query(..., description="Original subtitle URL (.srt or .vtt)"),
    target: str = Query("en", description="Target language code (e.g. az, tr, en, ru)"),
    key: Optional[str] = Query(None, description="Optional Google Gemini API key")
):
    api_key = key or DEFAULT_GEMINI_KEY
    if not api_key:
        raise HTTPException(status_code=400, detail="Gemini API key is required. Set GEMINI_API_KEY environment variable or pass ?key=")

    cache_key = hashlib.sha256(f"{url}:{target.lower()}".encode()).hexdigest()
    cache_file = CACHE_DIR / f"{cache_key}.vtt"
    if cache_file.exists():
        return Response(content=cache_file.read_text(encoding="utf-8"), media_type="text/vtt; charset=utf-8")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            sub_resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=30.0
            )
            sub_resp.raise_for_status()
            sub_text = sub_resp.text
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to fetch original subtitle: {e}")

        cues = parse_subtitles(sub_text)
        if not cues:
            raise HTTPException(status_code=422, detail="No subtitle cues found in source file")

        # Translate in batches of 50 cues
        batch_size = 50
        translated_cues = []
        for i in range(0, len(cues), batch_size):
            batch = cues[i:i + batch_size]
            batch_texts = [c["text"] for c in batch]
            translated_texts = await translate_batch(client, batch_texts, target, api_key)
            for cue, trans_text in zip(batch, translated_texts):
                translated_cues.append({
                    "start": cue["start"],
                    "end": cue["end"],
                    "text": trans_text
                })

        vtt_output = build_vtt(translated_cues)
        cache_file.write_text(vtt_output, encoding="utf-8")

        return Response(content=vtt_output, media_type="text/vtt; charset=utf-8")
