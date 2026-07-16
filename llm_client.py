"""
llm_client.py

Thin wrapper around the Groq free-tier API (OpenAI-compatible chat completions).
Groq was chosen because it has a genuinely free tier, an OpenAI-compatible
schema (easy to swap for Ollama / LM Studio / OpenAI later), and very low
latency, which matters for a "60 minute build" style agent.

ENGINEERING IMPROVEMENT IMPLEMENTED: Retry & Fallback logic.
See README.md for the full writeup. In short:
  - Every LLM call is wrapped in exponential-backoff retries.
  - If the LLM is unreachable / unconfigured / rate-limited after retries,
    the client raises LLMUnavailableError instead of crashing the request.
  - The Agent (see agent.py) catches that error and falls back to a
    deterministic, rule-based planner/writer so the API NEVER returns a
    500 to the caller just because a third-party LLM had a bad day.
"""
import json
import os
import time
import logging
from typing import Optional
from dotenv import load_dotenv
import requests
load_dotenv()


logger = logging.getLogger("agent.llm")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")


class LLMUnavailableError(Exception):
    """Raised when the LLM could not be reached after all retries."""


class LLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        max_retries: int = 3,
        base_backoff_seconds: float = 1.5,
        timeout_seconds: float = 20.0,
    ):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.model = model
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.timeout_seconds = timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _call_once(self, system_prompt: str, user_prompt: str, json_mode: bool) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.4,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        resp = requests.post(
            GROQ_API_URL, headers=headers, json=payload, timeout=self.timeout_seconds
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def complete(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
        """
        Calls the LLM with retry + exponential backoff.
        Raises LLMUnavailableError if not configured or all retries fail.
        The caller (agent.py) is expected to catch this and use a fallback.
        """
        if not self.is_configured:
            raise LLMUnavailableError("GROQ_API_KEY is not set; no LLM configured.")

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._call_once(system_prompt, user_prompt, json_mode)
            except Exception as exc:  # network error, timeout, 4xx/5xx, rate limit, etc.
                last_error = exc
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s", attempt, self.max_retries, exc
                )
                if attempt < self.max_retries:
                    sleep_for = self.base_backoff_seconds * (2 ** (attempt - 1))
                    time.sleep(sleep_for)

        raise LLMUnavailableError(
            f"LLM unavailable after {self.max_retries} attempts: {last_error}"
        )

    @staticmethod
    def safe_json_parse(text: str) -> Optional[dict]:
        """Best-effort JSON extraction in case the model wraps JSON in prose/backticks."""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            return json.loads(text)
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except Exception:
                    return None
        return None
