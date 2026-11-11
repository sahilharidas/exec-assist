"""Telegram transport: outbound sends and inbound long-polling.

Only one process may long-poll a given bot token at a
time, so ea-bot.py must be the single consumer of getUpdates.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

API = "https://api.telegram.org"
CHUNK_LIMIT = 3800   # Telegram's hard cap is 4096; leave room for headers.

# Telegram allows exactly one getUpdates consumer per bot token, and
# TELEGRAM_BOT_TOKEN belongs to the agent's own bot, which the Hermes agent
# already polls (hermes config: telegram.enabled = true). Two pollers on one
# token silently steal each other's messages, so exec-assist's poller requires a
# token of its own and will not fall back to the shared one.
POLL_TOKEN_NAMES = ("EA_TELEGRAM_BOT_TOKEN", "TELEGRAM_EXEC_ASSIST_TOKEN")
SHARED_TOKEN_NAMES = ("TELEGRAM_BOT_TOKEN",)

_dedicated_only = False


def dedicated_token():
    """exec-assist's own bot token, if one has been configured."""
    return config.secret(*POLL_TOKEN_NAMES)


def require_dedicated():
    """Bind this process to exec-assist's own bot. Returns None if absent.

    Sending is safe on the shared bot -- it never conflicts -- but polling is
    not, so only the daemon calls this.
    """
    global _dedicated_only
    tok = dedicated_token()
    if tok:
        _dedicated_only = True
    return tok


def _token():
    if _dedicated_only:
        return config.secret(*POLL_TOKEN_NAMES, required=True)
    # Outbound goes to the SHARED bot on purpose. Since the migration to Hermes
    # the conversation the user actually reads is the Hermes one, so the timed
    # jobs must land there -- alongside the Morning Brief, which
    # other send-only scripts already use this token. Only the
    # (now-retired) standalone poller uses the dedicated bot, via
    # require_dedicated().
    return config.secret(*SHARED_TOKEN_NAMES, required=True)


def _chat():
    return config.secret("TELEGRAM_HOME_CHANNEL", "TELEGRAM_CHAT_ID", required=True)


def md_to_html(text):
    """Convert the subset of markdown we emit into Telegram-safe HTML.

    Mirrors scripts/brief-to-telegram.py: escape first, then add our own tags, or
    the escaping eats them.
    """
    text = re.sub(r"\\u[eE][0-9a-fA-F]{3}", "", text)
    text = re.sub(r"[-]", "", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"```(?:\w+)?\n(.*?)```", r"<pre>\1</pre>", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^\s*#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])_([^_\n]+)_(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunks(text, limit=CHUNK_LIMIT):
    """Split on paragraph, then line, then hard boundaries."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 <= limit:
            cur += (("\n\n" if cur else "") + para)
            continue
        if cur:
            out.append(cur)
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            out.append(para[:cut])
            para = para[cut:].lstrip("\n")
        cur = para
    if cur:
        out.append(cur)
    return out


def _call(method, params=None, files=None, timeout=60, raw=False):
    url = f"{API}/bot{_token()}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "description": f"network: {exc}"}
    if raw:
        return body
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "description": body[:200].decode("utf-8", "replace")}


def send(text, chat_id=None, html=True, preview=False, reply_markup=None):
    """Send a message, chunked and converted. Returns the list of API results."""
    body = md_to_html(text) if html else text
    results = []
    parts = chunks(body)
    for i, part in enumerate(parts):
        params = {
            "chat_id": chat_id or _chat(),
            "text": part,
            "disable_web_page_preview": "false" if preview else "true",
        }
        if html:
            params["parse_mode"] = "HTML"
        if reply_markup and i == len(parts) - 1:
            params["reply_markup"] = json.dumps(reply_markup)
        res = _call("sendMessage", params)
        if not res.get("ok"):
            # Retry once as plain text: a bad entity in generated content should
            # not swallow the whole message.
            params.pop("parse_mode", None)
            params["text"] = re.sub(r"<[^>]+>", "", part)
            res = _call("sendMessage", params)
        results.append(res)
        if len(parts) > 1:
            time.sleep(0.3)
    return results


def get_updates(offset=None, timeout=50, allowed=None):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    if allowed:
        params["allowed_updates"] = json.dumps(allowed)
    res = _call("getUpdates", params, timeout=timeout + 15)
    if not res.get("ok"):
        return None, res.get("description", "unknown error")
    return res.get("result", []), None


def file_bytes(file_id, max_mb=25):
    """Download a Telegram file (used for voice notes)."""
    res = _call("getFile", {"file_id": file_id})
    if not res.get("ok"):
        return None, res.get("description", "getFile failed")
    info = res["result"]
    if info.get("file_size", 0) > max_mb * 1024 * 1024:
        return None, f"file larger than {max_mb}MB"
    url = f"{API}/file/bot{_token()}/{info['file_path']}"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read(), None
    except (urllib.error.URLError, TimeoutError) as exc:
        return None, f"download failed: {exc}"


def set_commands(commands):
    """Register the slash-command menu so the client offers autocomplete."""
    return _call("setMyCommands", {"commands": json.dumps(commands)})


def me():
    return _call("getMe")
