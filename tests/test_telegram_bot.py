import os
import sys
import unittest
import urllib.error
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
    verify_and_process_telegram_reply,
    telegram_enabled,
    reset_telegram_verification
)

class TestTelegramBotHardening(unittest.TestCase):

    def setUp(self):
        reset_telegram_verification()

    def tearDown(self):
        reset_telegram_verification()

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
        # First call is getMe in telegram_enabled(), second call is sendMessage
        mock_get_me = MagicMock()
        mock_get_me.status = 200
        mock_get_me.read.return_value = b'{"ok": true, "result": {"username": "ApplyMateBot"}}'
        mock_get_me.__enter__.return_value = mock_get_me

        mock_send_msg = MagicMock()
        mock_send_msg.status = 200
        mock_send_msg.read.return_value = b'{"ok": true}'
        mock_send_msg.__enter__.return_value = mock_send_msg

        mock_urlopen.side_effect = [mock_get_me, mock_send_msg]

        dispatched = send_batch_questions_to_telegram(
            company_name="Stripe",
            job_title="Software Engineer",
            questions=[{"label": "Country", "type": "select", "options": ["India +91"]}],
            app_id="app-123"
        )
        self.assertTrue(dispatched)
        self.assertEqual(mock_urlopen.call_count, 2)
        get_me_req = mock_urlopen.call_args_list[0][0][0]
        self.assertEqual(get_me_req.full_url, "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456/getMe")
        send_req = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(send_req.full_url, "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456/sendMessage")

    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456",
        "TELEGRAM_CHAT_ID": "987654321"
    })
    def test_telegram_enabled_checks_get_me_once_and_handles_404(self, mock_urlopen):
        """telegram_enabled verifies via getMe once. HTTP 404 disables it and prevents repeated calls."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.telegram.org/bot.../getMe",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None
        )
        self.assertFalse(telegram_enabled())
        # Subsequent calls should return cached False without calling urlopen again
        self.assertFalse(telegram_enabled())
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": '  "123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456" \n',
        "TELEGRAM_CHAT_ID": " ' 987654321 ' "
    })
    def test_telegram_enabled_with_quoted_and_whitespace_values(self, mock_urlopen):
        """telegram_enabled strips quotes and whitespace correctly and succeeds if getMe is ok."""
        mock_get_me = MagicMock()
        mock_get_me.status = 200
        mock_get_me.read.return_value = b'{"ok": true, "result": {"username": "ApplyMateBot"}}'
        mock_get_me.__enter__.return_value = mock_get_me
        mock_urlopen.return_value = mock_get_me

        self.assertTrue(telegram_enabled())
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456/getMe")

    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "your_bot_token_here",
        "TELEGRAM_CHAT_ID": "your_chat_id_here"
    })
    def test_telegram_enabled_with_placeholders(self):
        """telegram_enabled rejects template placeholders immediately without network calls."""
        self.assertFalse(telegram_enabled())

    def test_telegram_message_splitting(self):
        """Messages exceeding Telegram character limit must be split safely into chunks."""
        from telegram_bot import split_telegram_message
        long_text = "line " * 1500  # ~7500 chars
        chunks = split_telegram_message(long_text, max_chunk=4000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4000)

    def test_verify_and_process_callback_query(self):
        """Callback queries from unauthorized senders must be blocked; authorized must pass."""
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz_123456",
            "TELEGRAM_CHAT_ID": "987654321"
        }):
            # Unauthorized callback query
            unauth_cb = {
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": 111222333},
                    "message": {"message_id": 42, "chat": {"id": 111222333}},
                    "data": "approve_app_123"
                }
            }
            self.assertIsNone(verify_and_process_telegram_reply(unauth_cb))

            # Authorized callback query
            auth_cb = {
                "callback_query": {
                    "id": "cb2",
                    "from": {"id": 987654321},
                    "message": {"message_id": 42, "chat": {"id": 987654321}},
                    "data": "approve_app_123"
                }
            }
            res = verify_and_process_telegram_reply(auth_cb)
            self.assertIsNotNone(res)
            self.assertEqual(res["type"], "callback_query")
            self.assertEqual(res["data"], "approve_app_123")

if __name__ == "__main__":
    unittest.main()
