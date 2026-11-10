"""The one thin model adapter. Everything else calls LLM.json(role, system, user, schema).

Provider and models come from env vars (defaults = Gemini free tier):
  PROVIDER        gemini | anthropic          (default gemini)
  SOLVER_A_MODEL, SOLVER_B_MODEL, AUDITOR_MODEL, REPLY_MODEL
  GEMINI_API_KEY  / ANTHROPIC_API_KEY         (never committed)
  LLM_MIN_INTERVAL  seconds between calls to the same model (default 6 on Gemini = 10 RPM)
  LLM_TEMPERATURE   default 0; dropped automatically if a model rejects it

Responses are cached on disk by a hash of the full request, so reruns cost nothing and
results.json is reproducible."""
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / ".cache" / "llm"

DEFAULTS = {
    "gemini": {
        "solver_a": "gemini-3.8-flash",
        "solver_b": "gemini-3.6-flash",
        "auditor": "gemini-3.8-flash",
        "reply": "gemini-3.5-flash-lite",
    },
    "anthropic": {
        "solver_a": "claude-opus-5-5",
        "solver_b": "claude-sonnet-5-5",
        "auditor": "claude-sonnet-5-5",
        "reply": "claude-haiku-5-5",
    },
}
ENV = {"solver_a": "SOLVER_A_MODEL", "solver_b": "SOLVER_B_MODEL", "auditor": "AUDITOR_MODEL", "reply": "REPLY_MODEL"}
MAX_TRIES = 7


class QuotaExhausted(RuntimeError):
    """Daily quota gone. Stop cleanly; the cache keeps finished calls, rerun later."""


class LLMError(RuntimeError):
    pass


def _strip_additional(schema):
    """Gemini's schema subset may reject additionalProperties; code validates shape anyway."""
    if isinstance(schema, dict):
        return {k: _strip_additional(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_strip_additional(x) for x in schema]
    return schema


def _retry_delay(err_text, attempt):
    m = re.search(r"retry(?:Delay)?[\"']?\s*[:=]?\s*[\"']?(\d+(?:\.\d+)?)s", err_text, re.I)
    base = float(m.group(1)) if m else min(5 * 2 ** attempt, 120)
    return base + random.uniform(0, 2)


class LLM:
    def __init__(self, provider=None, cache_dir=CACHE, verbose=True):
        self.provider = (provider or os.environ.get("PROVIDER", "gemini")).lower()
        if self.provider not in DEFAULTS:
            raise ValueError("PROVIDER must be gemini or anthropic")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        default_gap = "6" if self.provider == "gemini" else "0"
        self.min_interval = float(os.environ.get("LLM_MIN_INTERVAL", default_gap))
        self.temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
        self.verbose = verbose
        self._client = None
        self._last = {}
        self._dropped = {}          # model -> {"temperature", "thinking"} params it rejected
        self.stats = dict(calls=0, cache_hits=0, retries=0)

    def model(self, role):
        return os.environ.get(ENV[role], DEFAULTS[self.provider][role])

    # -- public -----------------------------------------------------------------------
    def json(self, role, system, user, schema, effort="medium"):
        model = self.model(role)
        req = dict(provider=self.provider, model=model, system=system, user=user, schema=schema,
                   effort=effort, temperature=self.temperature)
        key = hashlib.sha256(json.dumps(req, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.cache_dir / (key + ".json")
        if path.exists():
            self.stats["cache_hits"] += 1
            return json.loads(path.read_text(encoding="utf-8"))["parsed"]
        text = self._call_with_retry(model, system, user, schema, effort)
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            raise LLMError("%s returned invalid JSON: %r" % (model, (text or "")[:200])) from e
        path.write_text(json.dumps(dict(request={k: v for k, v in req.items() if k != "schema"},
                                        raw=text, parsed=parsed), ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return parsed

    # -- internals --------------------------------------------------------------------
    def _pace(self, model):
        gap = time.time() - self._last.get(model, 0)
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last[model] = time.time()

    def _call_with_retry(self, model, system, user, schema, effort):
        for attempt in range(MAX_TRIES):
            self._pace(model)
            self.stats["calls"] += 1
            try:
                if self.provider == "gemini":
                    return self._gemini(model, system, user, schema, effort)
                return self._anthropic(model, system, user, schema, effort)
            except QuotaExhausted:
                raise
            except _Retryable as e:
                self.stats["retries"] += 1
                wait = _retry_delay(str(e), attempt)
                if self.verbose:
                    print("  [retry %d] %s: %s; sleeping %.0fs" % (attempt + 1, model, str(e)[:120], wait))
                time.sleep(wait)
        raise LLMError("%s failed after %d tries" % (model, MAX_TRIES))

    # Gemini (google-genai) ---------------------------------------------------------------
    def _gemini(self, model, system, user, schema, effort):
        from google import genai
        from google.genai import errors, types
        if self._client is None:
            if not os.environ.get("GEMINI_API_KEY"):
                raise LLMError("GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        dropped = self._dropped.setdefault(model, set())
        cfg = dict(system_instruction=system, response_mime_type="application/json",
                   response_json_schema=_strip_additional(schema))
        if "temperature" not in dropped:
            cfg["temperature"] = self.temperature
        if "thinking" not in dropped:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_level=effort.upper())
        try:
            resp = self._client.models.generate_content(
                model=model, contents=user, config=types.GenerateContentConfig(**cfg))
        except errors.ClientError as e:
            msg = str(e)
            if e.code == 429:
                if re.search(r"per ?day|PerDay|daily", msg, re.I):
                    raise QuotaExhausted("%s daily quota exhausted: %s" % (model, msg[:200]))
                raise _Retryable(msg)
            if e.code == 400 and re.search(r"temperature|sampling", msg, re.I) and "temperature" not in dropped:
                dropped.add("temperature")
                print("  [note] %s rejects temperature; retrying without it" % model)
                raise _Retryable("drop temperature")
            if e.code == 400 and re.search(r"thinking", msg, re.I) and "thinking" not in dropped:
                dropped.add("thinking")
                print("  [note] %s rejects thinking_level=%s; retrying without it" % (model, effort))
                raise _Retryable("drop thinking")
            raise LLMError("%s: %s" % (model, msg[:300]))
        except errors.ServerError as e:
            raise _Retryable(str(e))
        if not resp.text:
            raise _Retryable("empty response (finish: %s)" % getattr(resp.candidates[0], "finish_reason", "?")
                             if resp.candidates else "empty response, no candidates")
        return resp.text

    # Anthropic (optional switch) ----------------------------------------------------------
    def _anthropic(self, model, system, user, schema, effort):
        import anthropic
        if self._client is None:
            self._client = anthropic.Anthropic()
        try:
            # temperature is not sent: current Claude models reject sampling parameters.
            resp = self._client.messages.create(
                model=model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            raise _Retryable(str(e))
        if resp.stop_reason == "refusal":
            raise LLMError("%s refused the request" % model)
        if resp.stop_reason == "max_tokens":
            raise LLMError("%s hit max_tokens" % model)
        return next(b.text for b in resp.content if b.type == "text")


class _Retryable(Exception):
    pass
