import os
import re
import json
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Optional, Tuple

def sanitize_log(msg: str) -> str:
    """Strip any Telegram bot tokens or credentials from log strings."""
    # Redact /bot<token>/ and bot tokens of format \d+:[A-Za-z0-9_-]+
    cleaned = re.sub(r'bot\d+:[A-Za-z0-9_\-]+', 'bot[REDACTED_TOKEN]', str(msg))
    cleaned = re.sub(r'\/bot[^\/]+\/', '/bot[REDACTED_TOKEN]/', cleaned)
    return cleaned

def get_telegram_config() -> Tuple[Optional[str], Optional[str]]:
    """
    Securely load Telegram bot token and chat ID.
    Never prints or logs the token or chat ID.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("TELEGRAM_BOT_TOKEN=") and not token:
                            token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("TELEGRAM_CHAT_ID=") and not chat_id:
                            chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    return token, chat_id

def send_batch_questions_to_telegram(
    company_name: str,
    job_title: str,
    questions: List[Dict[str, Any]],
    app_id: str
) -> bool:
    """
    Sends a formatted notification to Telegram when a batch of questions needs answers.
    Security: Never logs or prints the bot token or chat ID.
    """
    token, chat_id = get_telegram_config()
    if not token or not chat_id:
        # Generic message without revealing paths or credential names
        print("[Telegram] Bot token or chat ID is not configured. Notification skipped.")
        return False

    if not questions:
        return False

    header = f"🚨 *ApplyMate: {len(questions)} Question(s) Need Answers*\n"
    header += f"🏢 *Company:* {company_name or 'Direct Application'}\n"
    if job_title:
        header += f"💼 *Role:* {job_title}\n"
    header += "\n"

    lines = []
    for idx, q in enumerate(questions, 1):
        lbl = q.get("label", "").replace("*", "").replace("_", "")
        q_type = q.get("type", "text")
        scope = q.get("scope", "global")
        scope_str = "🏢 Just this company" if scope in ["company", "this company"] else "🌐 Every site (Global)"
        ai_sug = q.get("ai_suggestion", "")
        opts = q.get("options", [])

        q_block = f"*{idx}. {lbl}*\n"
        q_block += f"   • *Type:* `{q_type}`\n"
        q_block += f"   • *Default Scope:* {scope_str}\n"
        if opts:
            opts_preview = ", ".join([str(o) for o in opts[:4]])
            if len(opts) > 4:
                opts_preview += "..."
            q_block += f"   • *Options:* {opts_preview}\n"
        if ai_sug:
            q_block += f"   • *AI Suggestion:* _{ai_sug}_\n"
        lines.append(q_block)

    footer = (
        "\n👉 *Reply in Web Dashboard:* [Open ApplyMate](http://localhost:3000)\n"
        "💡 *Tip:* Scope determines whether 'willing to relocate' or salary applies only to this company or globally."
    )

    full_message = header + "\n".join(lines) + footer

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": full_message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                print(f"[Telegram] Batch notification for {len(questions)} questions dispatched successfully.")
                return True
    except Exception as e:
        safe_err = sanitize_log(str(e))
        print(f"[Telegram] Failed to dispatch message: {safe_err}")

    return False

def verify_and_process_telegram_reply(update_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Strict Security: Accepts messages ONLY from the configured chat ID.
    Rejects any update or webhook from unauthorized senders.
    """
    token, configured_chat_id = get_telegram_config()
    if not token or not configured_chat_id:
        return None

    message = update_data.get("message") or update_data.get("edited_message")
    if not message:
        return None

    incoming_chat_id = str(message.get("chat", {}).get("id") or "")
    if not incoming_chat_id or incoming_chat_id != str(configured_chat_id):
        # Reject unauthorized senders immediately
        print("[Telegram Security] Blocked message from unauthorized chat ID.")
        return None

    # Authorized sender confirmed
    text = (message.get("text") or "").strip()
    return {
        "authorized": True,
        "message_id": message.get("message_id"),
        "text": text,
        "date": message.get("date")
    }
