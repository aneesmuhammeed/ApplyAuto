import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Support running both from project root and from backend directory
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
if (ROOT_DIR / "backend").exists():
    sys.path.insert(0, str(ROOT_DIR / "backend"))

from telegram_bot import (
    get_telegram_config,
    is_valid_telegram_token,
    is_valid_telegram_chat_id,
    clean_telegram_token,
    clean_telegram_chat_id,
    send_batch_questions_to_telegram,
    verify_and_process_telegram_reply
)

class TestTelegramBotHardening(unittest.TestCase):

    def test_placeholder_detection(self):
        """Placeholders from .env.example or rotated template should be rejected."""
        self.assertFalse(is_valid_telegram_token("your_rotated_telegram_bot_token_here"))
        self.assertFalse(is_valid_telegram_token("your_telegram_bot_token_here"))
        self.assertFalse(is_valid_telegram_token("PLACEHOLDER_TOKEN"))
        self.assertFalse(is_valid_telegram_token(""))
        self.assertFalse(is_valid_telegram_token(None))

        self.assertFalse(is_valid_telegram_chat_id("your_telegram_chat_id_here"))
        self.assertFalse(is_valid_telegram_chat_id("chat_id_placeholder"))
        self.assertFalse(is_valid_telegram_chat_id(""))
        self.assertFalse(is_valid_telegram_chat_id(None))

    def test_valid_token_and_chat_id_format(self):
        """Real Telegram bot tokens and chat IDs should be recognized."""
        sample_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456"
        self.assertTrue(is_valid_telegram_token(sample_token))

        # Accidental 'bot' prefix
        self.assertTrue(is_valid_telegram_token("bot" + sample_token))

        # Personal chat ID (positive int)
        self.assertTrue(is_valid_telegram_chat_id("123456789"))
        # Group/Supergroup chat ID (negative int)
        self.assertTrue(is_valid_telegram_chat_id("-1001234567890"))
        # Channel username
        self.assertTrue(is_valid_telegram_chat_id("@my_alerts_channel"))

    def test_cleaning_stray_quotes_and_whitespace(self):
        """Stray quotes, whitespace, and 'bot' prefixes must be stripped cleanly."""
        raw_token = '  "bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456" \n'
        cleaned = clean_telegram_token(raw_token)
        self.assertEqual(cleaned, "123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456")

        raw_chat = " ' -1001234567890 ' "
        cleaned_chat = clean_telegram_chat_id(raw_chat)
        self.assertEqual(cleaned_chat, "-1001234567890")

    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "your_rotated_telegram_bot_token_here",
        "TELEGRAM_CHAT_ID": "your_telegram_chat_id_here"
    })
    def test_get_telegram_config_rejects_placeholders_without_network_call(self):
        """Config should return (None, None) for placeholders to prevent 404 HTTP errors."""
        token, chat_id = get_telegram_config()
        self.assertIsNone(token)
        self.assertIsNone(chat_id)

        # Dispatch should cleanly skip and return False without making any network call
        dispatched = send_batch_questions_to_telegram(
            company_name="Stripe",
            job_title="Software Engineer",
            questions=[{"label": "Country", "type": "select"}],
            app_id="app-123"
        )
        self.assertFalse(dispatched)

    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456",
        "TELEGRAM_CHAT_ID": "987654321"
    })
    def test_successful_dispatch_with_valid_credentials(self, mock_urlopen):
        """With valid credentials, Request is created with properly formed URL."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        dispatched = send_batch_questions_to_telegram(
            company_name="Stripe",
            job_title="Software Engineer",
            questions=[{"label": "Country", "type": "select", "options": ["India +91"]}],
            app_id="app-123"
        )
        self.assertTrue(dispatched)
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456/sendMessage")

if __name__ == "__main__":
    unittest.main()
