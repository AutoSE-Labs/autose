import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Union

import yaml

_PROMPTS_FILE = Path(__file__).parent / "prompts.json"

with open(_PROMPTS_FILE, "r", encoding="utf-8") as _f:
    _PROMPTS = json.load(_f)["classifier"]

_SYSTEM_PROMPT: str = _PROMPTS["system"]
_CLASSIFICATION_PROMPT: str = _PROMPTS["user"]

_VALID_TYPES = frozenset({"lite", "standard"})


class TaskClassifier:
    """Classifies a user prompt into a task type using a single LLM call."""

    def __init__(self, config_path: Union[str, Path]) -> None:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        inference = config.get("inference", {})
        self._base_url: str = inference.get(
            "base_url", "http://127.0.0.1:11434/v1"
        ).rstrip("/")
        self._api_key: str = inference.get("api_key", "") or ""
        self._model: str = inference.get("model", "")

    def classify(self, prompt: str, history: list[dict] | None = None) -> str:
        """Return 'lite' or 'standard' for the given prompt.

        history is an optional list of prior {"role": ..., "content": ...}
        messages (user/assistant only) providing conversation context.
        """
        raw = self._call_llm(prompt.strip(), history or [])
        return self._parse_response(raw)

    def _call_llm(self, prompt: str, history: list[dict]) -> str:
        # Build messages: system, then prior conversation as context, then
        # the classification request as the final user turn.
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]

        if history:
            # Summarise history as a read-only context block so the model
            # understands what has been discussed before classifying.
            history_text = "\n".join(
                f"{m['role'].capitalize()}: {m['content']}" for m in history
            )
            messages.append({
                "role": "user",
                "content": (
                    "Here is the conversation so far for context:\n\n"
                    f"{history_text}\n\n"
                    "Use this context when classifying the next message below."
                ),
            })
            messages.append({
                "role": "assistant",
                "content": "Understood. I have noted the conversation context.",
            })

        messages.append({
            "role": "user",
            "content": _CLASSIFICATION_PROMPT.format(prompt=prompt),
        })
        payload = json.dumps(
            {
                "model": self._model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }
        ).encode("utf-8")

        headers: dict = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        return result["choices"][0]["message"]["content"]

    def _parse_response(self, raw: str) -> str:
        try:
            data = json.loads(raw.strip())
            task_type = str(data.get("task_type", "")).lower()
            if task_type in _VALID_TYPES:
                return task_type
        except (json.JSONDecodeError, KeyError, AttributeError):
            pass

        # Fallback: scan the raw text for keywords (most specific first)
        lowered = raw.lower()
        if "standard" in lowered:
            return "standard"
        return "lite"
