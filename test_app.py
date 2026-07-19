import tempfile
from datetime import datetime, timedelta, timezone
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
        self.assertEqual(app.schedule_state()["times"], ["02:00", "08:00", "14:00", "20:00"])
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
        app.save_schedule(True, ["02:00", "08:16"])
        app.save_event_control(True)
        app.save_events([{
            "external_id": "dashboard-event",
            "occurred_at": "2026-07-16 01:00:00",
            "account": "A",
            "reward": "Motor",
        }])
        dashboard = app.dashboard_state()
        self.assertEqual(dashboard["mode"], "always")
        self.assertEqual(dashboard["schedule"]["times"], ["02:00", "08:16"])
        self.assertTrue(dashboard["event"]["enabled"])
        self.assertEqual(dashboard["event"]["reward_count"], 1)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            app.save_mode("danger")

    def test_bot_control_and_heartbeat_are_in_dashboard(self):
        app.save_bot_control(True)
        app.save_agent_status({
            "health": "running", "task_state": "Running",
            "next_run": "2026-07-19 18:00:00", "last_result": 0,
            "progress": {"phase": "normal", "current_account": "FANDA", "account_index": 4, "total_accounts": 13},
        })
        status = app.dashboard_state()["bot"]
        self.assertTrue(status["enabled"])
        self.assertEqual(status["health"], "running")
        self.assertEqual(status["next_run"], "2026-07-19 18:00:00")
        self.assertEqual(status["progress"]["current_account"], "FANDA")
        self.assertEqual(app.save_bot_control(False)["health"], "off")

    def test_named_viewer_sees_only_assigned_accounts(self):
        saved = app.save_app_user("ali", "guvenli-test-123", ["YAVUZ", "FANDA"])
        self.assertEqual(saved["username"], "ali")
        session = app.create_session("ali", "guvenli-test-123")
        self.assertEqual(session["role"], "viewer")
        self.assertEqual(session["username"], "ali")
        self.assertEqual(app.session_role(session["token"]), "viewer")
        self.assertEqual(app.assigned_accounts("ali"), ["FANDA", "YAVUZ"])
        self.assertNotIn("guvenli-test-123", str(app.list_app_users()))

    def test_user_update_invalidates_old_session_and_delete_removes_user(self):
        app.save_app_user("mehmet", "ilk-sifre-123", ["A"])
        session = app.create_session("mehmet", "ilk-sifre-123")
        app.save_app_user("mehmet", "yeni-sifre-456", ["B"])
        self.assertIsNone(app.session_role(session["token"]))
        self.assertEqual(app.assigned_accounts("mehmet"), ["B"])
        self.assertTrue(app.delete_app_user("mehmet")["deleted"])
        with self.assertRaises(PermissionError):
            app.create_session("mehmet", "yeni-sifre-456")

    def test_skip_request_is_claimed_once_for_account(self):
        app.save_skip_request("FANDA", "tur-1")
        self.assertFalse(app.claim_skip_request("FANDA", "tur-2")["skip"])
        self.assertTrue(app.claim_skip_request("FANDA", "tur-1")["skip"])
        self.assertFalse(app.claim_skip_request("FANDA", "tur-1")["skip"])

    def test_account_statuses_are_derived_from_live_progress(self):
        app.save_agent_status({
            "health": "running",
            "progress": {
                "status": "running", "phase": "normal", "account_index": 2,
                "account_names": ["A", "B", "C"], "skipped_accounts": ["A"],
            },
        })
        states = app.account_status_state()["accounts"]
        self.assertEqual([item["status"] for item in states], ["skipped", "running", "pending"])

        filtered = app.account_status_state(["B", "ÖZEL"])
        self.assertEqual([item["name"] for item in filtered["accounts"]], ["B", "ÖZEL"])
        self.assertEqual([item["status"] for item in filtered["accounts"]], ["running", "not_scheduled"])
        self.assertEqual(filtered["bot"]["progress"]["account_names"], ["B"])
        self.assertEqual(app.account_catalog(), ["A", "B", "C"])

    def test_earnings_daily_and_all_time_reports(self):
        today = datetime.now().strftime("%Y-%m-%d")
        entries = [
            {"external_id": "e1", "occurred_at": today + " 01:00:00", "account": "A", "operation": "Uzay Farmı", "result": "10 taş"},
            {"external_id": "e2", "occurred_at": "2020-01-01 01:00:00", "account": "B", "operation": "Mermi fabrikası", "result": "100 mermi"},
        ]
        self.assertEqual(app.save_earnings(entries), 2)
        self.assertEqual(app.save_earnings(entries), 0)
        reports = app.earnings_dashboard()
        self.assertEqual(reports["daily"]["total"], 1)
        self.assertEqual(reports["all_time"]["total"], 2)

    def test_material_report_parses_products_and_compares_with_yesterday(self):
        turkey_today = datetime.now(timezone(timedelta(hours=3))).date()
        today = turkey_today.isoformat()
        yesterday = (turkey_today - timedelta(days=1)).isoformat()
        app.save_earnings([
            {"external_id": "m1", "occurred_at": today + " 01:00:00", "account": "A", "operation": "Uzay Farmı", "result": "30 adet aurorium | 4 adet carbon | 30 XP"},
            {"external_id": "m2", "occurred_at": today + " 02:00:00", "account": "A", "operation": "Enerji fabrikası", "result": "Kazanılan Enerji : 14 - Kazanılan Bonus Exp : 20000"},
            {"external_id": "m3", "occurred_at": yesterday + " 02:00:00", "account": "A", "operation": "Enerji fabrikası", "result": "Kazanılan Enerji : 7 - Kazanılan Bonus Exp : 10000"},
            {"external_id": "m4", "occurred_at": today + " 03:00:00", "account": "A", "operation": "Eyalet sanayisi", "result": "18 adet ürün deponuza aktarıldı."},
        ])
        report = app.earnings_dashboard()["materials"]
        by_key = {item["key"]: item for item in report["comparison"]}
        self.assertEqual(by_key["enerji"]["today"], 14)
        self.assertEqual(by_key["enerji"]["yesterday"], 7)
        self.assertEqual(by_key["enerji"]["percent"], 100.0)
        self.assertEqual(by_key["sanayi parcasi"]["today"], 18)
        self.assertEqual(by_key["aurorium"]["today"], 30)
        self.assertEqual(by_key["xp"]["today"], 20030)
        self.assertNotIn("bonus exp", by_key)

    def test_schedule_accepts_minute_and_dot_formats(self):
        enabled, times = app.parse_schedule_command("/saat 04.16 08:05 14")
        self.assertTrue(enabled)
        self.assertEqual(times, ["04:16", "08:05", "14:00"])
        app.save_schedule(enabled, times)
        self.assertEqual(app.schedule_state()["times"], times)

    def test_long_messages_are_split(self):
        parts = app.split_text(("x" * 2000 + "\n") * 3)
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all(len(part) <= 4000 for part in parts))


if __name__ == "__main__":
    unittest.main()
