"""Google Lyria 3 / Lyria 3 Pro.

Lyria is the structured one: it generates full songs with real form
(intro, verse, chorus, bridge), it takes a BPM, and it has an instrumental
mode. That makes it a good fit for beds and for anything where you need
the arrangement to land in a known tempo and key.

It returns a single mix rather than stems. If you want stems, separate the
result locally (Ableton Live 12 does this offline for free, and BS-RoFormer
via python-audio-separator does it from the command line) or generate
isolated parts with Stable Audio instead.

Docs: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/lyria/lyria-3
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx

from . import GenerationRequest, GenerationResult, ProviderError

GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "lyria-3-pro-preview"
CLIP_MODEL = "lyria-3-clip-preview"
MAX_DURATION_S = 184.0
CLIP_DURATION_S = 30.0


class LyriaProvider:
    name = "lyria"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 300.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
        self.model = model
        self.timeout = timeout

    def _require_key(self) -> str:
        if not self.api_key:
            raise ProviderError(
                "GEMINI_API_KEY is not set. Lyria 3 is available in public "
                "preview through the Gemini API and Google AI Studio."
            )
        return self.api_key

    def build_prompt(self, request: GenerationRequest) -> str:
        prompt = request.prompt.strip()
        if request.isolate and "instrumental" not in prompt.lower():
            prompt = f"Instrumental only. {prompt}"
        return prompt

    def build_body(self, request: GenerationRequest) -> dict:
        """The documented generateAudio payload."""
        duration = max(1.0, min(request.duration_s, CLIP_DURATION_S if self.model == CLIP_MODEL else MAX_DURATION_S))

        config: dict = {"durationSeconds": duration}
        if request.seed is not None:
            config["seed"] = request.seed
        for key in ("bpm", "intensity", "negativePrompt"):
            if key in request.extra:
                config[key] = request.extra[key]

        return {
            "contents": [{"parts": [{"text": self.build_prompt(request)}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "audio": {"durationSeconds": duration},
            },
            "audioConfig": config,
        }

    def generate(self, request: GenerationRequest, out_dir: Path) -> GenerationResult:
        key = self._require_key()
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        body = self.build_body(request)
        url = f"{GEMINI_ROOT}/models/{self.model}:generateContent"

        headers = {"Content-Type": "application/json", "x-goog-api-key": key}

        try:
            response = httpx.post(url, headers=headers, json=body, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Lyria request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"Lyria returned {response.status_code}: {response.text[:400]}"
            )

        data = response.json()
        audio_bytes = self._extract_audio(data)

        suffix = "wav" if request.output_format == "wav" else "mp3"
        name = f"lyria_{abs(hash(body['contents'][0]['parts'][0]['text'])) % 10**8}.{suffix}"
        out_path = out_dir / name
        out_path.write_bytes(audio_bytes)

        return GenerationResult(
            provider=self.name,
            model=self.model,
            prompt=body["contents"][0]["parts"][0]["text"],
            seed=request.seed,
            audio_path=out_path,
            duration_s=body["generationConfig"]["audio"]["durationSeconds"],
            raw={"config": body["generationConfig"]},
        )

    def _extract_audio(self, data: dict) -> bytes:
        candidates = data.get("candidates") or []
        for candidate in candidates:
            parts = (candidate.get("content") or {}).get("parts") or []
            for part in parts:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        raise ProviderError(f"no audio in Lyria response: {str(data)[:300]}")
