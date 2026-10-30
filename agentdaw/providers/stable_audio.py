"""Stability AI -- Stable Audio 3.0.

The most useful of the hosted models for this system, for one specific
reason: it can generate an *isolated part* rather than a finished song. A
prompt that opens with `TrackType: Instrument` returns one instrument
instead of a full arrangement. That is the difference between generative
audio being a source in the production and generative audio being the
whole production.

It also has audio-to-audio (init_noise_level) and inpainting with numeric
mask boundaries, which is how you revise a generated bed without
regenerating it from scratch.

Docs: https://docs.stability.ai/api-reference/stable-audio/text-to-audio
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from . import GenerationRequest, GenerationResult, ProviderError

API_ROOT = "https://api.stability.ai/v2beta/audio"
MODEL = "stable-audio-3"
MAX_DURATION_S = 380.0


class StableAudioProvider:
    name = "stable_audio"

    def __init__(self, api_key: str | None = None, timeout: float = 300.0) -> None:
        self.api_key = api_key or os.environ.get("STABILITY_API_KEY", "")
        self.timeout = timeout

    def _require_key(self) -> str:
        if not self.api_key:
            raise ProviderError(
                "STABILITY_API_KEY is not set. Stable Audio 3.0 is free below "
                "$1M revenue with attribution; get a key at platform.stability.ai."
            )
        return self.api_key

    def build_prompt(self, request: GenerationRequest) -> str:
        """Tag the prompt for an isolated part rather than a full arrangement."""
        prompt = request.prompt.strip()
        if request.isolate and not prompt.lower().startswith("tracktype:"):
            prompt = f"TrackType: Instrument, Format: Solo. {prompt}"
        return prompt

    def generate(self, request: GenerationRequest, out_dir: Path) -> GenerationResult:
        key = self._require_key()
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        prompt = self.build_prompt(request)
        duration = max(1.0, min(request.duration_s, MAX_DURATION_S))

        fields: dict[str, str] = {
            "prompt": prompt,
            "model": MODEL,
            "duration": f"{duration:.0f}",
            "output_format": request.output_format,
            "steps": str(request.extra.get("steps", 8)),
            "cfg_scale": str(request.extra.get("cfg_scale", 1)),
        }
        if request.seed is not None:
            fields["seed"] = str(request.seed)

        headers = {
            "Authorization": f"Bearer {key}",
            "accept": "audio/*",
            "stability-client-id": "agentdaw",
            "stability-client-version": "0.1.0",
        }

        # multipart form, matching the documented curl form
        files = {k: (None, str(v)) for k, v in fields.items()}

        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(
                    f"{API_ROOT}/stable-audio/text-to-audio",
                    headers=headers,
                    files=files,
                )
            except httpx.HTTPError as exc:
                raise ProviderError(f"Stable Audio request failed: {exc}") from exc

            if response.status_code >= 400:
                raise ProviderError(
                    f"Stable Audio returned {response.status_code}: {response.text[:400]}"
                )

            content_type = response.headers.get("content-type", "")
            if "audio" in content_type:
                audio_bytes = response.content
                seed = request.seed
            else:
                data = response.json()
                generation_id = data.get("id")
                if not generation_id:
                    raise ProviderError(f"no generation id in response: {str(data)[:300]}")

                audio_bytes, seed = self._poll(client, headers, generation_id)

        suffix = "wav" if request.output_format == "wav" else "mp3"
        name = f"gen_{abs(hash(prompt)) % 10**8}_{request.seed if request.seed is not None else 0}.{suffix}"
        out_path = out_dir / name
        out_path.write_bytes(audio_bytes)

        return GenerationResult(
            provider=self.name,
            model=MODEL,
            prompt=prompt,
            seed=seed,
            audio_path=out_path,
            duration_s=duration,
            raw={"prompt": prompt, "duration": duration},
        )

    def _poll(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        generation_id: str,
        interval: float = 2.0,
        max_wait: float = 600.0,
    ) -> tuple[bytes, int | None]:
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                result = client.get(f"{API_ROOT}/results/{generation_id}", headers=headers)
            except httpx.HTTPError as exc:
                raise ProviderError(f"Stable Audio poll failed: {exc}") from exc

            if result.status_code == 202:
                time.sleep(interval)
                continue
            if result.status_code >= 400:
                raise ProviderError(
                    f"Stable Audio poll returned {result.status_code}: {result.text[:400]}"
                )

            content_type = result.headers.get("content-type", "")
            if "audio" in content_type:
                return result.content, None

            data = result.json()
            seed = data.get("seed")
            if "audio" in data:
                import base64
                return base64.b64decode(data["audio"]), seed
            raise ProviderError(f"unexpected Stable Audio result: {str(data)[:300]}")

        raise ProviderError("Stable Audio generation timed out")
