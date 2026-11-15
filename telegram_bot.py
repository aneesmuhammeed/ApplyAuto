import os
import re
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import List, Dict, Any, Optional, Tuple

# Cached verified state: None = unverified, True = enabled & verified, False = disabled/invalid
_TELEGRAM_ENABLED: Optional[bool] = None
_TELEGRAM_CHECKED_ONCE: bool = False

def sanitize_log(msg: str) -> str:
    """Strip any Telegram bot tokens or credentials from log strings."""
    # Redact /bot<token>/ and bot tokens of format \d+:[A-Za-z0-9_-]+
    cleaned = re.sub(r'bot\d+:[A-Za-z0-9_\-]+', 'bot[REDACTED_TOKEN]', str(msg))
    cleaned = re.sub(r'\/bot[^\/]+\/', '/bot[REDACTED_TOKEN]/', cleaned)
    return cleaned

def strip_quotes_and_whitespace(val: Any) -> str:
    """Strip any surrounding whitespace, quotes, or trailing comments from env value."""
    if val is None:
        return ""
    s = str(val).strip()
    while s and (s[0] in ('"', "'", ' ', '\t') or s[-1] in ('"', "'", ' ', '\t', '\r', '\n')):
        s = s.strip().strip('"').strip("'").strip()
    return s

def is_valid_telegram_token(token: Optional[str]) -> bool:
    """Check if token matches real Telegram bot token format and is not a placeholder."""
    if not token:
        return False
    t = strip_quotes_and_whitespace(token)
    if not t or ":" not in t:
        return False
    if any(placeholder in t.lower() for placeholder in ["your_bot_token_here", "your_", "placeholder", "token_here", "example", "none", "fake", "test", "<", ">", "changeme", "xxx"]):
        return False
    if t.startswith("bot") and ":" in t:
        t = t[3:]
    return bool(re.match(r'^\d{6,14}:[A-Za-z0-9_\-]{25,55}$', t))

def is_valid_telegram_chat_id(chat_id: Optional[str]) -> bool:
    """Check if chat_id matches real Telegram chat ID format (integer or @channel) and is not a placeholder."""
    if not chat_id:
        return False
    c = strip_quotes_and_whitespace(chat_id)
    if not c:
        return False
    if any(placeholder in c.lower() for placeholder in ["your_chat_id_here", "your_", "placeholder", "chat_id_here", "example", "none", "fake", "test", "<", ">", "changeme", "xxx"]):
        return False
    return bool(re.match(r'^-?\d{4,16}$|^@[A-Za-z0-9_]{4,32}$', c))

def clean_telegram_token(token: Optional[str]) -> Optional[str]:
    """Sanitize and validate Telegram bot token."""
    if not token:
        return None
    t = strip_quotes_and_whitespace(token)
    if t.startswith("bot") and ":" in t:
        t = t[3:]
    return t if is_valid_telegram_token(t) else None

def clean_telegram_chat_id(chat_id: Optional[str]) -> Optional[str]:
    """Sanitize and validate Telegram chat ID."""
    if not chat_id:
        return None
    c = strip_quotes_and_whitespace(chat_id)
    return c if is_valid_telegram_chat_id(c) else None

def get_telegram_config() -> Tuple[Optional[str], Optional[str]]:
    """
    Securely load and validate Telegram bot token and chat ID.
    Strips stray quotes, whitespace, or accidental 'bot' prefixes.
    Rejects placeholders (e.g. from .env.example) so invalid credentials
    never trigger 404 Not Found network errors.
    Never prints or logs the token or chat ID.
    """
    raw_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not raw_token or not raw_chat_id:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("TELEGRAM_BOT_TOKEN=") and not raw_token:
                            raw_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("TELEGRAM_CHAT_ID=") and not raw_chat_id:
                            raw_chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass

    token = clean_telegram_token(raw_token)
    chat_id = clean_telegram_chat_id(raw_chat_id)

    return token, chat_id

def telegram_enabled() -> bool:
    """
    Checks if Telegram integration is configured and enabled.
    Verifies the token ONCE at startup via getMe.
    If disabled or invalid, logs ONE clear warning and skips all sends.
    Never retries a 404 in a loop.
    """
    global _TELEGRAM_ENABLED, _TELEGRAM_CHECKED_ONCE

    if _TELEGRAM_CHECKED_ONCE:
        return bool(_TELEGRAM_ENABLED)

    _TELEGRAM_CHECKED_ONCE = True
    token, chat_id = get_telegram_config()

    if not token or not chat_id:
        _TELEGRAM_ENABLED = False
        print("[Telegram Warning] Telegram integration is disabled: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing or placeholder.")
        return False

    # Verify token once via getMe
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        req = urllib.request.Request(url, headers={"User-Agent": "ApplyMate/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8")
                data = json.loads(body)
                if data.get("ok"):
                    bot_name = data.get("result", {}).get("username", "Bot")
                    print(f"[Telegram] Connected and verified successfully as @{bot_name}.")
                    _TELEGRAM_ENABLED = True
                    return True
    except urllib.error.HTTPError as http_err:
        if http_err.code == 404:
            print("[Telegram Warning] TELEGRAM_BOT_TOKEN is invalid (HTTP 404 Not Found from getMe). Telegram notifications are disabled.")
        elif http_err.code == 401:
            print("[Telegram Warning] TELEGRAM_BOT_TOKEN is unauthorized (HTTP 401). Telegram notifications are disabled.")
        else:
            print(f"[Telegram Warning] getMe failed with HTTP {http_err.code}. Telegram notifications are disabled.")
    except Exception as e:
        safe_e = sanitize_log(str(e))
        print(f"[Telegram Warning] Could not verify Telegram bot token ({safe_e}). Telegram notifications are disabled.")

    _TELEGRAM_ENABLED = False
    return False

def reset_telegram_verification():
    """Reset verification cache for testing."""
    global _TELEGRAM_ENABLED, _TELEGRAM_CHECKED_ONCE
    _TELEGRAM_ENABLED = None
    _TELEGRAM_CHECKED_ONCE = False

def split_telegram_message(msg: str, max_chunk: int = 4000) -> List[str]:
    """Split a message longer than Telegram's 4096 character limit cleanly by newline."""
    if len(msg) <= max_chunk:
        return [msg]
    chunks = []
    lines = msg.split("\n")
    cur = ""
    for line in lines:
        if len(cur) + len(line) + 1 > max_chunk:
            if cur:
                chunks.append(cur)
                cur = ""
            if len(line) > max_chunk:
                for i in range(0, len(line), max_chunk):
                    chunks.append(line[i:i+max_chunk])
            else:
                cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks

def send_batch_questions_to_telegram(
    company_name: str,
    job_title: str,
    questions: List[Dict[str, Any]],
    app_id: str
) -> bool:
    """
    Sends a plain-text notification to Telegram when a batch of questions needs answers.
    Security:
      - Plain text (no parse_mode) ensures unescaped symbols (*, _, [, ]) never break sending.
      - Never logs or prints the bot token or chat ID.
      - Splits messages > 4000 characters.
    """
    if not telegram_enabled():
        return False

    token, chat_id = get_telegram_config()
    if not token or not chat_id:
        return False

    if not questions:
        return False

    header = f"🚨 ApplyMate: {len(questions)} Question(s) Need Answers\n"
    header += f"Company: {company_name or 'Direct Application'}\n"
    if job_title:
        header += f"Role: {job_title}\n"
    header += f"Application ID: {app_id}\n\n"

    lines = []
    for idx, q in enumerate(questions, 1):
        lbl = q.get("label", "")
        q_type = q.get("type", "text")
        scope = q.get("scope", "global")
        scope_str = "Just this company" if scope in ["company", "this company"] else "Every site (Global)"
        ai_sug = q.get("ai_suggestion", "")
        opts = q.get("options", [])

        q_block = f"{idx}. {lbl}\n"
        q_block += f"   • Type: {q_type}\n"
        q_block += f"   • Default Scope: {scope_str}\n"
        if opts:
            opts_preview = ", ".join([str(o) for o in opts[:4]])
            if len(opts) > 4:
                opts_preview += "..."
            q_block += f"   • Options: {opts_preview}\n"
        if ai_sug:
            q_block += f"   • AI Suggestion: {ai_sug}\n"
        lines.append(q_block)

    footer = (
        "\n👉 Reply in Web Dashboard: http://localhost:3000\n"
        "💡 Tip: Scope determines whether your answer applies only to this company or globally."
    )

    full_message = header + "\n".join(lines) + footer
    chunks = split_telegram_message(full_message, max_chunk=4000)
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    all_success = True

    for chunk in chunks:
        try:
            payload = json.dumps({
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True
            }).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status != 200:
                    all_success = False
        except urllib.error.HTTPError as http_err:
            all_success = False
            err_desc = ""
            try:
                err_body = http_err.read().decode("utf-8")
                err_json = json.loads(err_body)
                err_desc = err_json.get("description", "")
            except Exception:
                err_desc = str(http_err)

            if http_err.code == 404:
                print("[Telegram Warning] Bot token not found on Telegram API (HTTP 404). Verify your TELEGRAM_BOT_TOKEN.")
            elif http_err.code == 401:
                print("[Telegram Warning] Unauthorized (HTTP 401). Verify your TELEGRAM_BOT_TOKEN.")
            elif http_err.code == 400:
                print(f"[Telegram Warning] Bad Request (HTTP 400): {err_desc}. Check TELEGRAM_CHAT_ID.")
            else:
                print(f"[Telegram Warning] HTTP Error {http_err.code}: {err_desc}")
        except Exception as e:
            all_success = False
            safe_err = sanitize_log(str(e))
            print(f"[Telegram Warning] Failed to dispatch message: {safe_err}")

    if all_success:
        print(f"[Telegram] Batch notification for {len(questions)} questions dispatched successfully.")
    return all_success

def verify_and_process_telegram_reply(update_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Strict Security: Accepts messages and callback queries ONLY from the configured chat ID.
    Rejects any update or webhook from unauthorized senders.
    Guards against callback reply attacks.
    """
    token, configured_chat_id = get_telegram_config()
    if not token or not configured_chat_id:
        return None

    # Handle standard message or edited message
    message = update_data.get("message") or update_data.get("edited_message")
    if message:
        incoming_chat_id = str(message.get("chat", {}).get("id") or "")
        if not incoming_chat_id or incoming_chat_id != str(configured_chat_id):
            print("[Telegram Security] Blocked message from unauthorized chat ID.")
            return None

        text = (message.get("text") or "").strip()
        return {
            "authorized": True,
            "type": "message",
            "message_id": message.get("message_id"),
            "text": text,
            "date": message.get("date")
        }

    # Handle callback_query (inline button clicks)
    callback = update_data.get("callback_query")
    if callback:
        sender_chat_id = str(callback.get("message", {}).get("chat", {}).get("id") or callback.get("from", {}).get("id") or "")
        if not sender_chat_id or sender_chat_id != str(configured_chat_id):
            print("[Telegram Security] Blocked callback_query from unauthorized chat ID.")
            return None

        data_val = (callback.get("data") or "").strip()
        return {
            "authorized": True,
            "type": "callback_query",
            "callback_id": callback.get("id"),
            "message_id": callback.get("message", {}).get("message_id"),
            "data": data_val
        }

    return None
