import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import app


class VssBotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir = app.DATA_DIR
        self.old_db_path = app.DB_PATH
        app.DATA_DIR = Path(self.temp.name)
        app.DB_PATH = app.DATA_DIR / "test.sqlite3"
        app.prepare_database()

    def tearDown(self):
        app.DATA_DIR = self.old_data_dir
        app.DB_PATH = self.old_db_path
        self.temp.cleanup()

    @staticmethod
    def message(command: str) -> dict:
        return {
            "text": command,
            "chat": {"id": 123, "type": "private", "username": "vlknarslan"},
            "from": {"id": 123, "username": "vlknarslan"},
        }

    def test_single_run_is_consumed_once(self):
        app.set_setting("mode", "once")
        first = app.claim_decision()
        second = app.claim_decision()
        self.assertTrue(first["active"])
        self.assertTrue(first["consumed"])
        self.assertFalse(second["active"])

    def test_always_mode_remains_active(self):
        app.set_setting("mode", "always")
        self.assertTrue(app.claim_decision()["active"])
        self.assertTrue(app.claim_decision()["active"])
        self.assertEqual(app.get_setting("mode"), "always")

    def test_commands_change_mode_and_cancel_everything(self):
        with patch.object(app, "send_message"):
            app.apply_command(self.message("/surekli"))
            self.assertEqual(app.get_setting("mode"), "always")
            app.apply_command(self.message("/iptal"))
            self.assertEqual(app.get_setting("mode"), "off")

    def test_other_username_is_ignored(self):
        message = self.message("/surekli")
        message["chat"]["username"] = "someoneelse"
        message["from"]["username"] = "someoneelse"
        with patch.object(app, "send_message") as send:
            app.apply_command(message)
        self.assertEqual(app.get_setting("mode"), "off")
        send.assert_not_called()

    def test_admin_can_enable_and_disable_volkan_account(self):
        message = self.message("/surekli")
        message["chat"]["username"] = "JackTheRipppper"
        message["from"]["username"] = "JackTheRipppper"
        message["chat"]["id"] = 456
        message["from"]["id"] = 456
        with patch.object(app, "send_message"):
            app.apply_command(message)
            self.assertEqual(app.get_setting("mode"), "always")
            message["text"] = "/iptal"
            app.apply_command(message)
            self.assertEqual(app.get_setting("mode"), "off")


if __name__ == "__main__":
    unittest.main()
