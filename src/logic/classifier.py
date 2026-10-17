"""Choosing a complexity tier for a prompt (DR-003).

One low-cost model call per new prompt picks a tier, so a one-line fix does not
pay for the same machinery an architectural change needs. Two properties matter
more than accuracy:

* **It must not crash.** A classifier that raises turns a working request into
  a failed one. Every failure path lands on a tier.
* **It must fail toward the middle.** Guessing high is expensive; guessing low
  under-powers the request. DR-003 chooses `craft` on total failure for that
  reason.

The response is read as JSON first, then by a deterministic text scan when the
model returns prose — which small local models routinely do, so the scan is a
normal path rather than a last resort.

A reasoning model is also asked, via the standard ``reasoning_effort`` field
(DR-020), to skip thinking for this call: classification does not need it, and
letting a reasoning model think anyway can burn its entire small output budget
before it ever reaches the answer (DR-016). Providers that ignore the field
are unaffected, which is why the reasoning-channel scan below still exists as
a second line of defence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .common.context import ContextBudget
from .errors import AutoSEError
from .inference.base import ChatModel
from .limits import count_limit
from .prompt_overrides import PromptOverrides, format_prompt, resolve_prompt
from .tiers import DEFAULT_TIER, TIER_SPECS, TIERS, normalise_tier
from .tuning import ClassifierTuning

__all__ = ["Classification", "TaskClassifier"]

_SYSTEM_PROMPT = """\
You classify a software engineering request by how much work it needs.

Answer with JSON and nothing else: {{"tier": "<name>"}}

Tiers, from least to most effort:
{tiers}

Guidance:
- Judge the work the request implies, not how politely or briefly it is worded.
- "How does X work?", "what does this file do?", "fix this typo" -> spark.
- "Add feature X", "fix this bug", "write tests for Y", "restructure X",
  anything spanning many files -> craft.
- A short follow-up inside an ongoing task ("yes, do that", "use the other
  approach") is part of that task. Judge it by the work it continues.

Answer with the JSON object only.
"""

_USER_PROMPT = """\
Classify this request:

<request>
{prompt}
</request>

Reply with {{"tier": "..."}} and nothing else.
"""

#: Words that, when a model writes prose instead of JSON, point at a tier.
#: Ordered most-specific first so "not a simple change" does not read as spark.
_TEXT_HINTS: tuple[tuple[str, str], ...] = (
    (r"\bcraft\b", "craft"),
    (r"\bspark\b", "spark"),
    (r"\barchitect\w*\b", "craft"),
    (r"\bredesign\b|\brefactor\b|\brestructure\b", "craft"),
    (r"\bimplement\b|\bfeature\b|\bbug\b", "craft"),
    (r"\btrivial\b|\bsimple question\b|\bone[- ]line\b", "spark"),
)


@dataclass(frozen=True)
class Classification:
    """A tier, and how confidently it was reached."""

    tier: str
    #: ``"model"`` when the JSON parsed, ``"text_scan"`` when prose was read,
    #: ``"fallback"`` when nothing usable came back.
    source: str = "model"
    detail: str = ""

    @property
    def is_confident(self) -> bool:
        return self.source == "model"


class TaskClassifier:
    """Assigns a tier to a prompt, never raising."""

    def __init__(
        self,
        model: ChatModel,
        *,
        context_limit: int | None = None,
        tuning: ClassifierTuning | None = None,
        prompt_overrides: PromptOverrides | None = None,
    ) -> None:
        self._model = model
        tuning = tuning or ClassifierTuning()
        # ``None`` sends no output cap at all (DR-030), which is what a model
        # that thinks before answering needs — and `ContextBudget` already
        # reads `None` as "scale the reservation to the window".
        self._max_output_tokens = count_limit(tuning.max_output_tokens)
        self._budget = ContextBudget(
            context_limit=context_limit,
            reserved_output_tokens=self._max_output_tokens,
        )
        self._reasoning_effort = tuning.reasoning_effort or None
        self._max_history_messages = max(0, tuning.max_history_messages)
        self._prompt_overrides = prompt_overrides

    def classify(
        self, prompt: str, *, history: list[dict] | None = None
    ) -> Classification:
        """Pick a tier for *prompt*, with recent history as read-only context."""
        if not (prompt or "").strip():
            return Classification(DEFAULT_TIER, "fallback", "empty prompt")

        try:
            messages = [{"role": "system", "content": self._system_prompt()}]
            if history:
                messages.extend(history[-self._max_history_messages :])
            messages.append(
                {
                    "role": "user",
                    "content": format_prompt(self._user_prompt(), prompt=prompt),
                }
            )
            prepared = self._budget.prepare(messages)
            result = self._model.complete(
                prepared.messages,
                max_output_tokens=self._max_output_tokens,
                reasoning_effort=self._reasoning_effort,
            )
        except AutoSEError as exc:
            return Classification(DEFAULT_TIER, "fallback", str(exc))
        except Exception as exc:  # noqa: BLE001 - classification must never crash a run
            return Classification(DEFAULT_TIER, "fallback", f"unexpected: {exc}")

        return self._read(result.content, reasoning=result.reasoning)

    # -- parsing -----------------------------------------------------------

    def _system_prompt(self) -> str:
        tiers = "\n".join(
            f"- {name}: {TIER_SPECS[name].classifier_hint}" for name in TIERS
        )
        template = resolve_prompt(
            "classifier_system", _SYSTEM_PROMPT, self._prompt_overrides
        )
        return format_prompt(template, tiers=tiers)

    def _user_prompt(self) -> str:
        return resolve_prompt("classifier_user", _USER_PROMPT, self._prompt_overrides)

    def _read(self, content: str, *, reasoning: str = "") -> Classification:
        text = (content or "").strip()
        if text:
            parsed = self._read_json(text)
            if parsed is not None:
                return Classification(parsed, "model")

            scanned = self._scan(text)
            if scanned is not None:
                return Classification(scanned, "text_scan", text[:200])

        reasoning = (reasoning or "").strip()
        if reasoning:
            scanned = self._scan(reasoning)
            if scanned is not None:
                return Classification(scanned, "text_scan", reasoning[-200:])

        return Classification(
            DEFAULT_TIER, "fallback", text[:200] or reasoning[-200:] or "empty response"
        )

    @staticmethod
    def _read_json(text: str) -> str | None:
        """Read ``{"tier": "..."}``, tolerating fences and surrounding prose."""
        for candidate in _json_candidates(text):
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            value = payload.get("tier") or payload.get("mode") or payload.get("level")
            if isinstance(value, str):
                normalised = normalise_tier(value, fallback="")
                if normalised:
                    return normalised
        return None

    @staticmethod
    def _scan(text: str) -> str | None:
        """Deterministic fallback for a model that answered in prose."""
        lowered = text.lower()
        for pattern, tier in _TEXT_HINTS:
            if re.search(pattern, lowered):
                return tier
        return None


def _json_candidates(text: str) -> list[str]:
    """Every plausible JSON object in *text*, outermost first."""
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    return candidates
