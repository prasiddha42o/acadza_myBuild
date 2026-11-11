"""The one thin model adapter. Everything else calls LLM.json(role, system, user, schema).

Provider and models come from env vars (defaults = Gemini free tier):
  PROVIDER        gemini | anthropic          (default gemini)
  SOLVER_A_MODEL, SOLVER_B_MODEL, AUDITOR_MODEL, REPLY_MODEL
                  each may be a comma-separated fallback chain, e.g. "gemini-3.8-flash,gemini-3.1-flash-lite":
                  a cached answer from any model in the chain is reused; otherwise the first model whose
                  daily quota is not exhausted is called. LLM.last_model says which one answered.
  GEMINI_API_KEY  / ANTHROPIC_API_KEY         (never committed)
  LLM_MIN_INTERVAL  seconds between calls to the same model (default 6 on Gemini = 10 RPM)
  LLM_TEMPERATURE   default 0; dropped automatically if a model rejects it
  LLM_TIMEOUT       seconds per request (default 150); 2 timeouts in a row skip to the next model

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
    # Free tier: each model has its own small daily cap, so every role is a fallback chain.
    # Solver chains are disjoint so the two solves come from different models where possible;
    # run.py refuses to count agreement when they don't.
    "gemini": {
        "solver_a": "gemini-3.8-flash,gemini-3.1-flash-lite",
        "solver_b": "gemini-3.6-flash,gemini-3.5-flash-lite",
        "auditor": "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite",
        "reply": "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
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


class ModelUnavailable(QuotaExhausted):
    """Model name unknown to this key (404). Treated like an exhausted model: try the next one."""


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
        self.timeout = float(os.environ.get("LLM_TIMEOUT", "150"))
        self.verbose = verbose
        self._client = None
        self._last = {}
        self._dropped = {}          # model -> {"temperature", "thinking"} params it rejected
        self.stats = dict(calls=0, cache_hits=0, retries=0, fallbacks=0)
        self.exhausted = set()      # models whose daily quota ran out during this run
        self.last_model = None
        self.used = {}              # model -> answered calls (cached or live)

    def chain(self, role):
        raw = os.environ.get(ENV[role]) or DEFAULTS[self.provider][role]
        return [m.strip() for m in raw.split(",") if m.strip()]

    def model(self, role):
        return ",".join(self.chain(role))

    def _path(self, model, system, user, schema, effort):
        req = dict(provider=self.provider, model=model, system=system, user=user, schema=schema,
                   effort=effort, temperature=self.temperature)
        key = hashlib.sha256(json.dumps(req, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return req, self.cache_dir / (key + ".json")

    def _answered(self, model):
        self.last_model = model
        self.used[model] = self.used.get(model, 0) + 1

    # -- public -----------------------------------------------------------------------
    def json(self, role, system, user, schema, effort="medium"):
        chain = self.chain(role)
        for model in chain:                                   # 1) any cached answer in the chain
            _, path = self._path(model, system, user, schema, effort)
            if path.exists():
                self.stats["cache_hits"] += 1
                self._answered(model)
                return json.loads(path.read_text(encoding="utf-8"))["parsed"]
        last = None
        for i, model in enumerate(chain):                     # 2) first model with quota left
            if model in self.exhausted:
                continue
            try:
                return self._live(model, system, user, schema, effort, fallback=i > 0)
            except QuotaExhausted as e:
                self.exhausted.add(model)
                last = e
                if self.verbose:
                    print("  [quota] %s %s; %s" % (
                        model, "not available" if isinstance(e, ModelUnavailable) else "exhausted for today", "trying next in chain" if i + 1 < len(chain) else "no fallback left"))
        raise last or QuotaExhausted("every model for %s is exhausted: %s" % (role, chain))

    def _live(self, model, system, user, schema, effort, fallback):
        req, path = self._path(model, system, user, schema, effort)
        text = self._call_with_retry(model, system, user, schema, effort)
        self.stats["fallbacks"] += fallback
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            raise LLMError("%s returned invalid JSON: %r" % (model, (text or "")[:200])) from e
        path.write_text(json.dumps(dict(request={k: v for k, v in req.items() if k != "schema"},
                                        raw=text, parsed=parsed), ensure_ascii=False, indent=1),
                        encoding="utf-8")
        self._answered(model)
        return parsed

    # -- internals --------------------------------------------------------------------
    def _pace(self, model):
        gap = time.time() - self._last.get(model, 0)
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last[model] = time.time()

    def _call_with_retry(self, model, system, user, schema, effort):
        timeouts = 0
        for attempt in range(MAX_TRIES):
            self._pace(model)
            self.stats["calls"] += 1
            try:
                if self.provider == "gemini":
                    return self._gemini(model, system, user, schema, effort)
                return self._anthropic(model, system, user, schema, effort)
            except QuotaExhausted:
                raise
            except _Timeout:
                timeouts += 1
                self.stats["retries"] += 1
                if self.verbose:
                    print("  [timeout %d] %s: no answer in %.0fs" % (timeouts, model, self.timeout))
                if timeouts >= 2:
                    raise ModelUnavailable("%s timed out twice" % model)
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
            self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                                        http_options=types.HttpOptions(timeout=int(self.timeout * 1000)))
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
            if e.code == 404:
                raise ModelUnavailable("%s not available: %s" % (model, msg[:200]))
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
        except Exception as e:                       # httpx timeouts are not genai errors
            if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
                raise _Timeout(str(e))
            raise
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


class _Timeout(Exception):
    pass
