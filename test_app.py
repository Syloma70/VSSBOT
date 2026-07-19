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

    def test_admin_can_set_global_schedule_hours(self):
        message = self.message("/saat 02 08:00, 14 20")
        message["chat"]["username"] = "JackTheRipppper"
        message["from"]["username"] = "JackTheRipppper"
        message["chat"]["id"] = 456
        message["from"]["id"] = 456
        with patch.object(app, "send_message") as send:
            app.apply_command(message)
        self.assertEqual(app.schedule_state()["hours"], [2, 8, 14, 20])
        self.assertTrue(app.schedule_state()["enabled"])
        self.assertIn("02:00", send.call_args.args[1])

    def test_non_admin_cannot_change_global_schedule(self):
        with patch.object(app, "send_message"):
            app.apply_command(self.message("/saat kapat"))
        self.assertTrue(app.schedule_state()["enabled"])

    def test_schedule_can_be_disabled_and_restored_to_hourly(self):
        app.save_schedule(False, [])
        self.assertFalse(app.schedule_state()["enabled"])
        app.save_schedule(True, [])
        self.assertEqual(app.schedule_text().splitlines()[2], "Her saat başı")

    def test_invalid_schedule_hour_is_rejected(self):
        with self.assertRaises(ValueError):
            app.parse_schedule_command("/saat 24")

    def test_event_commands_show_totals_and_accounts(self):
        app.save_events([
            {"external_id": "1", "occurred_at": "2026-07-16 01:00:00", "account": "A", "reward": "Motor"},
            {"external_id": "2", "occurred_at": "2026-07-16 02:00:00", "account": "B", "reward": "Motor"},
            {"external_id": "3", "occurred_at": "2026-07-16 03:00:00", "account": "A", "reward": "Tank"},
        ])
        with patch.object(app, "send_message") as send:
            app.apply_command(self.message("/etkinlik"))
            self.assertIn("Motor: 2 kez", send.call_args.args[1])
            app.apply_command(self.message("/etkinlikhesap"))
            self.assertIn("A — 2 kutu", send.call_args.args[1])
            self.assertIn("\n   • Motor", send.call_args.args[1])

    def test_duplicate_event_is_ignored(self):
        event = {"external_id": "same", "occurred_at": "2026-07-16 01:00:00", "account": "A", "reward": "Motor"}
        self.assertEqual(app.save_events([event]), 1)
        self.assertEqual(app.save_events([event]), 0)

    def test_event_control_defaults_off_and_can_be_enabled(self):
        self.assertFalse(app.event_control_state()["enabled"])
        self.assertTrue(app.save_event_control(True)["enabled"])
        self.assertTrue(app.event_control_state()["enabled"])

    def test_dashboard_combines_control_states_and_event_totals(self):
        app.save_mode("always")
        app.save_schedule(True, [2, 8])
        app.save_event_control(True)
        app.save_events([{
            "external_id": "dashboard-event",
            "occurred_at": "2026-07-16 01:00:00",
            "account": "A",
            "reward": "Motor",
        }])
        dashboard = app.dashboard_state()
        self.assertEqual(dashboard["mode"], "always")
        self.assertEqual(dashboard["schedule"]["hours"], [2, 8])
        self.assertTrue(dashboard["event"]["enabled"])
        self.assertEqual(dashboard["event"]["reward_count"], 1)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            app.save_mode("danger")

    def test_long_messages_are_split(self):
        parts = app.split_text(("x" * 2000 + "\n") * 3)
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all(len(part) <= 4000 for part in parts))


if __name__ == "__main__":
    unittest.main()
