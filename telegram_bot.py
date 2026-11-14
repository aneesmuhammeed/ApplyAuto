import os
import json
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Optional

def get_telegram_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        # Check backend/.env
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("TELEGRAM_BOT_TOKEN=") and not token:
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("TELEGRAM_CHAT_ID=") and not chat_id:
                        chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")
    return token, chat_id

def send_batch_questions_to_telegram(
    company_name: str,
    job_title: str,
    questions: List[Dict[str, Any]],
    app_id: str
) -> bool:
    """
    Sends a formatted notification to Telegram when a batch of questions needs answers.
    Returns True if sent successfully, False otherwise.
    """
    token, chat_id = get_telegram_config()
    if not token or not chat_id:
        print("[Telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured in backend/.env. Skipping Telegram message.")
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
                print(f"[Telegram] Batch of {len(questions)} questions sent to Telegram successfully.")
                return True
    except Exception as e:
        print(f"[Telegram] Failed to send message to Telegram: {e}")

    return False
