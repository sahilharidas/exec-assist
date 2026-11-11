"""LLM calls via OpenRouter.

OpenRouter is the only provider used. config.secret() filters placeholder values
("user_provided") out, so there is no silent fallback to a key that cannot work.
"""
import json
import urllib.error
import urllib.request

from . import config

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
# Short, high-volume jobs (one-line rewrites, classification) do not need the
# big model; keeping them cheap matters because these run on timers. Note these
# are OpenRouter model ids, which differ from the Anthropic API ids.
LIGHT_MODEL = "anthropic/claude-haiku-4.5"


def available():
    return bool(config.secret("OPENROUTER_KEY", "OPENROUTER_API_KEY"))


def ask(prompt, system=None, model=None, max_tokens=1200, temperature=0.3, timeout=120):
    """Single-turn completion. Returns (text, error)."""
    key = config.secret("OPENROUTER_KEY", "OPENROUTER_API_KEY")
    if not key:
        return None, "no OpenRouter key resolved"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "exec-assist",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        return None, f"HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, f"request failed: {exc}"
    try:
        return body["choices"][0]["message"]["content"].strip(), None
    except (KeyError, IndexError):
        return None, f"unexpected response: {json.dumps(body)[:300]}"
