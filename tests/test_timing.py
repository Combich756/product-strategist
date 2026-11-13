import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import test_strategist as support


class TimingTests(unittest.TestCase):
    setUp = support.CaseTests.setUp
    call = support.CaseTests.call
    local = support.CaseTests.local
    save = support.CaseTests.save
    brief = support.CaseTests.brief
    research = support.CaseTests.research

    def test_analysis_clock_survives_pause_and_followup_date(self):
        self.call("init", "За прошлую неделю", "--analysis-at", "2026-11-08T23:30:00Z",
                  "--timezone", "+03:00", case_id="timed")
        clock = self.call("context", case_id="timed")["analysis"]
        self.assertEqual(clock, {"as_of": "2026-11-09T02:30:00+03:00", "timezone": "+03:00", "periods": []})
        self.call("pause", case_id="timed")
        with patch("strategist.state.utc_now", return_value="2026-12-01T09:00:00+00:00"):
            self.assertEqual(self.call("context", case_id="timed")["analysis"], clock)
            self.call("resume", case_id="timed")
        self.assertEqual(self.call("context", case_id="timed")["analysis"], clock)
        self.assertIn(clock["as_of"], self.call("report", "--as-of", "2026-12-01", case_id="timed"))

    def test_comparison_periods_and_source_dates_stay_with_archived_claim(self):
        dates = {"as_of": "2026-11-08T12:00:00+00:00", "timezone": "UTC", "periods": [
            {"label": "База", "start": "2026-10-05", "end": "2026-10-11"},
            {"label": "Сравнение", "start": "2026-10-12", "end": "2026-10-18"},
        ]}
        content = "Стоимость пакета 500 ₽"
        first = self.local("tariff.md", content, "--period", "2026-10-05", "2026-10-18",
                           "--data-as-of", "2026-10-22T12:00:00+03:00", "--valid-from", "2026-10-01",
                           "--valid-through", "2026-10-31", "--timezone", "+03:00")
        self.save("brief", self.brief(analysis=dates))
        self.save("research", self.research(first["source"]))
        self.call("pause")
        self.assertEqual(self.call("context")["analysis"], dates)
        self.assertEqual(self.call("claim", "C1")["sources"][0]["timing"], first["timing"])
        self.call("resume")
        self.assertTrue(self.local("tariff.md", content)["reused"])

        updated = self.local("tariff.md", content, "--data-as-of", "2026-10-23")
        self.assertEqual(updated["replaces"], first["source"])
        self.assertEqual(updated["text_sha256"], first["text_sha256"])
        self.assertEqual(updated["timing"], {**first["timing"], "data_as_of": "2026-10-23"})
        status = self.call("status")
        self.assertEqual(status["next"], "research")
        self.assertEqual(status["impact"]["affected_claims"], ["C1"])
        baseline = str(status["impact"]["baseline_revision"])
        self.call("reopen", "brief", "--reason", "Новый период", "--expect", str(status["revision"]))
        new_dates = {**dates, "as_of": "2026-12-01T00:00:00+00:00", "periods": []}
        self.save("brief", self.brief(analysis=new_dates))
        self.assertEqual(self.call("context")["analysis"], new_dates)

        old = self.call("claim", "C1", "--revision", baseline)
        self.assertEqual(old["analysis"], dates)
        self.assertEqual(old["sources"][0]["timing"], first["timing"])
        self.assertEqual(old["sources"][0]["captured_at"], first["captured_at"])
        self.assertIn("срок действия 2026-10-01 — 2026-10-31", self.call("report"))
        changed = self.local("tariff.md", "Стоимость пакета 600 ₽")
        self.assertEqual(changed["replaces"], updated["source"])
        self.assertNotIn("timing", changed)

    def test_invalid_dates_leave_state_unchanged(self):
        (self.materials / "data.md").write_text("Данные", encoding="utf-8")
        args = ("--file", "data.md", "--kind", "synthetic")
        status = self.call("status")
        self.call("ingest", *args, "--period", "2026-10-18", "2026-10-05", error="начало позже окончания")
        self.call("ingest", *args, "--data-as-of", "2026-10-23T12:00:00", error="со смещением")
        self.call("ingest", *args, "--valid-through", "2026-02-30", error="календарная дата")
        self.call("ingest", *args, "--timezone", "Invalid/Timezone", error="неизвестный часовой пояс")
        clock = {"as_of": "2026-11-08T12:00:00+03:00", "timezone": "UTC", "periods": []}
        self.save("brief", self.brief(analysis=clock), error="смещение не соответствует")
        clock["timezone"] = "+03:00"
        clock["periods"] = [{"label": "Период", "start": "2026-11-01"}]
        self.save("brief", self.brief(analysis=clock), error="нужны даты start и end")
        self.assertEqual(self.call("status"), status)

    def test_old_case_recovers_creation_time_without_migration(self):
        database = self.root / "test" / "state.sqlite3"
        with closing(sqlite3.connect(database)) as conn, conn:
            data = json.loads(conn.execute("SELECT payload FROM snapshot").fetchone()[0])
            del data["analysis"]
            conn.execute("UPDATE snapshot SET payload=?", (json.dumps(data),))
        before = database.read_bytes()
        with patch("strategist.state.utc_now", return_value="2030-01-01T12:00:00+00:00"):
            clock = self.call("context")["analysis"]
        self.assertEqual(clock["as_of"], data["journal"][0]["at"])
        self.assertEqual(clock["timezone"], "UTC")
        self.assertEqual(database.read_bytes(), before)
