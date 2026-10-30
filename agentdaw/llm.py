"""LLM client for the Producer.

Protocol-agnostic on purpose. The same Producer works against the
MiniMax endpoint already configured on this machine, against OpenAI, and
against a local Ollama or LM Studio instance, with no code change. That
matters for the product path, where the Producer should be swappable.

Protocol is detected from the provider's `npm` field in config.yaml,
exactly the way the runtime does it, so the Producer inherits whatever
credentials already exist instead of demanding new ones.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

NPM_TO_PROTOCOL = {
    "@ai-sdk/anthropic": "anthropic",
    "@ai-sdk/openai": "chat-completions",
    "@ai-sdk/google": "gemini",
}

ENV_BASE_URL = "AGENTDAW_LLM_BASE_URL"
ENV_API_KEY = "AGENTDAW_LLM_API_KEY"
ENV_MODEL = "AGENTDAW_LLM_MODEL"
ENV_PROTOCOL = "AGENTDAW_LLM_PROTOCOL"
ENV_AUTH_STYLE = "AGENTDAW_LLM_AUTH_STYLE"

# Values that are clearly placeholders rather than real credentials.
_PLACEHOLDER_KEYS = {"", "sk-xxx", "xxx", "your-api-key", "changeme"}


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    protocol: str = "anthropic"
    provider: str = "unknown"
    auth_style: str = "auto"      # bearer | x-api-key | both
    auth_source: str = ""         # where the credential came from
    credential_loader: Any = None  # callable returning a fresh credential

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)


def _default_config_path() -> Path:
    base = (
        os.environ.get("__MAVIS_PARENT_DATA_DIR")
        or os.environ.get("MINIMAX_DATA_DIR")
        or str(Path.home() / ".minimax")
    )
    return Path(base) / "config.yaml"


def _default_data_dir() -> Path:
    return (
        Path(os.environ["__MAVIS_PARENT_DATA_DIR"])
        if os.environ.get("__MAVIS_PARENT_DATA_DIR")
        else Path(os.environ["MINIMAX_DATA_DIR"])
        if os.environ.get("MINIMAX_DATA_DIR")
        else Path.home() / ".minimax"
    )


def load_oauth_token(data_dir: Path | None = None) -> tuple[str, str]:
    """Find the runtime's OAuth access token.

    Returns (token, source). The runtime stores one per region under
    `auth/<region>/mcode-public/auth.json`; each record holds an
    accessToken plus its own expiry. The token is read fresh every call
    because the runtime rotates it underneath us.
    """
    root = data_dir or _default_data_dir()
    auth_root = root / "auth"
    if not auth_root.exists():
        return "", ""

    for path in sorted(auth_root.rglob("mcode-public/auth.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for record in (data.get("records") or {}).values():
            token = record.get("accessToken")
            if token and token not in _PLACEHOLDER_KEYS:
                return token, str(path)

    return "", ""


def oauth_credential_loader(data_dir: Path | None = None):
    """Return a callable that re-reads the runtime's OAuth token.

    The runtime rotates that token on its own schedule. A token that was
    valid when the process started can be rejected as "invalid access
    token" minutes later, so the credential is refreshed on demand rather
    than cached for the life of the process.
    """
    def loader() -> str:
        token, _ = load_oauth_token(data_dir)
        return token
    return loader


def load_llm_config(
    config_path: str | Path | None = None,
    model: str | None = None,
    provider_key: str | None = None,
) -> LLMConfig:
    """Resolve LLM settings from environment, then the runtime config, then
    the runtime's OAuth store."""
    env_base = os.environ.get(ENV_BASE_URL)
    env_model = os.environ.get(ENV_MODEL)
    env_key = os.environ.get(ENV_API_KEY)

    if env_base and env_model:
        return LLMConfig(
            base_url=env_base,
            api_key=env_key or "",
            model=env_model,
            protocol=os.environ.get(ENV_PROTOCOL, "anthropic"),
            provider="env",
            auth_style=os.environ.get(ENV_AUTH_STYLE, "auto"),
            auth_source="environment",
        )

    path = Path(config_path) if config_path else _default_config_path()
    if not path.exists():
        token, source = load_oauth_token()
        return LLMConfig(
            base_url="", api_key=token, model=model or "",
            auth_style=os.environ.get(ENV_AUTH_STYLE, "auto"),
            auth_source=source,
        )

    data = yaml.safe_load(path.read_text()) or {}
    providers = data.get("provider") or {}

    keys = [provider_key] if provider_key else list(providers.keys())
    for key in keys:
        if not key or key not in providers:
            continue
        conf = providers[key]
        options = conf.get("options") or {}
        models = conf.get("models") or {}
        protocol = NPM_TO_PROTOCOL.get(conf.get("npm", ""), "anthropic")

        chosen = model or env_model
        if chosen is None:
            chosen = next(iter(models.keys()), "")

        api_key = options.get("apiKey", "") or env_key or ""
        source = "config.yaml"
        loader = None
        if api_key in _PLACEHOLDER_KEYS:
            token, token_source = load_oauth_token()
            if token:
                api_key = token
                source = token_source
                loader = oauth_credential_loader()

        return LLMConfig(
            base_url=options.get("baseURL", "") or env_base or "",
            api_key=api_key,
            model=chosen or "",
            protocol=os.environ.get(ENV_PROTOCOL, protocol),
            provider=key,
            auth_style=os.environ.get(ENV_AUTH_STYLE, "auto"),
            auth_source=source,
            credential_loader=loader,
        )

    token, source = load_oauth_token()
    return LLMConfig(
        base_url="", api_key=token, model=model or "",
        auth_style=os.environ.get(ENV_AUTH_STYLE, "auto"),
        auth_source=source,
    )


class LLMError(RuntimeError):
    pass


class LLMTruncated(LLMError):
    """The model ran out of token budget before producing an answer.

    Reasoning models spend part of `max_tokens` on deliberation, and that
    budget is shared with the answer. A task that needs a couple of
    thousand tokens of thinking plus a couple of thousand of JSON will
    silently return nothing at all if the cap is too low.
    """


class LLMClient:
    """Minimal, provider-shaped chat client."""

    def __init__(self, config: LLMConfig, timeout: float = 180.0) -> None:
        self.config = config
        self.timeout = timeout

    def complete(
        self,
        system: str,
        prompt: str,
        max_tokens: int = 8000,
        temperature: float = 0.5,
        thinking_budget: int | None = None,
    ) -> str:
        if not self.config.available:
            raise LLMError(
                "no LLM configured. Set AGENTDAW_LLM_BASE_URL and AGENTDAW_LLM_MODEL, "
                f"or provide a config at {_default_config_path()}"
            )

        if self.config.protocol == "anthropic":
            return self._anthropic(system, prompt, max_tokens, temperature, thinking_budget)
        if self.config.protocol == "chat-completions":
            return self._chat_completions(system, prompt, max_tokens, temperature)
        if self.config.protocol == "gemini":
            return self._gemini(system, prompt, max_tokens, temperature)
        raise LLMError(f"unsupported protocol: {self.config.protocol}")

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict:
        """POST with one automatic retry after a credential refresh.

        A rotating credential can be invalidated between the moment it is
        read and the moment the request lands. When that happens the
        server says "invalid access token" -- a token was presented, it
        just is not the current one. Reload and try again once.
        """
        for attempt in range(2):
            try:
                response = httpx.post(url, headers=headers, json=body, timeout=self.timeout)
            except httpx.HTTPError as exc:
                raise LLMError(f"request to {url} failed: {exc}") from exc

            if response.status_code == 401 and attempt == 0:
                if self._reload_credential():
                    headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")}
                    headers.update(self._auth_headers())
                    continue

            if response.status_code >= 400:
                raise LLMError(
                    f"{url} returned {response.status_code}: {response.text[:400]}"
                )
            return response.json()

        raise LLMError(f"{url} kept rejecting the credential")

    def _reload_credential(self) -> bool:
        loader = self.config.credential_loader
        if loader is None:
            return False
        try:
            fresh = loader()
        except Exception:
            return False
        if fresh and fresh != self.config.api_key:
            self.config.api_key = fresh
            return True
        return False

    def _auth_headers(self) -> dict[str, str]:
        """Build the auth header for the resolved protocol.

        The MiniMax gateway wants `Authorization: Bearer`. A first-party
        Anthropic endpoint wants `x-api-key`. Rather than hard-code one,
        resolve by the endpoint and allow an explicit override.
        """
        if not self.config.api_key:
            return {}

        style = self.config.auth_style
        if style == "auto":
            style = "bearer" if "minimax" in self.config.base_url.lower() else "x-api-key"

        headers: dict[str, str] = {}
        if style in ("bearer", "both"):
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if style in ("x-api-key", "both"):
            headers["x-api-key"] = self.config.api_key
        return headers

    def _anthropic(
        self,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        thinking_budget: int | None = None,
    ) -> str:
        url = f"{self.config.base_url.rstrip('/')}/messages"
        headers = {"Content-Type": "application/json"}
        headers.update(self._auth_headers())
        body = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if thinking_budget is not None:
            # Some reasoning models require adaptive thinking and refuse
            # to disable it, but they do accept a budget. Capping it cuts
            # latency a lot without risking the answer.
            body["thinking"] = {"type": "enabled", "budget_tokens": int(thinking_budget)}
        data = self._post(url, headers, body)

        chunks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        text = "".join(chunks).strip()
        if not text:
            usage = data.get("usage") or {}
            details = usage.get("output_tokens_details") or {}
            raise LLMTruncated(
                f"model returned no text (stop_reason={data.get('stop_reason')}, "
                f"thinking_tokens={details.get('thinking_tokens', 0)}, "
                f"output_tokens={usage.get('output_tokens', 0)} of {max_tokens}); "
                "raise max_tokens"
            )
        return text

    def _chat_completions(self, system: str, prompt: str, max_tokens: int, temperature: float) -> str:
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        headers.update(self._auth_headers())
        body = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        data = self._post(url, headers, body)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"no choices in response: {json.dumps(data)[:300]}")
        return (choices[0].get("message") or {}).get("content", "").strip()

    def _gemini(self, system: str, prompt: str, max_tokens: int, temperature: float) -> str:
        url = f"{self.config.base_url.rstrip('/')}/models/{self.config.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["x-goog-api-key"] = self.config.api_key
        body = {            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        data = self._post(url, headers, body)
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose and fences no matter how firmly you ask.
    """
    text = text.strip()

    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    raise ValueError("no valid JSON object found in model output")
