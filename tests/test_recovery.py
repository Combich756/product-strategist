import json
import unittest

import test_strategist as support


class ReliabilityTests(unittest.TestCase):
    setUp = support.CaseTests.setUp
    call = support.CaseTests.call
    local = support.CaseTests.local
    save = support.CaseTests.save
    brief = support.CaseTests.brief
    research = support.CaseTests.research

    def draft(self, name, value):
        path = self.root / "test" / "drafts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_numeric_hypothesis_needs_evidence_and_generated_refs_cover_both_rows(self):
        source = self.local("data.csv", "week,connected,orders\na,70,100\nb,110,220\n")["source"]
        self.save("brief", self.brief())
        research = self.research(source)
        claim = research["claims"][0]
        claim.update(type="hypothesis", text="Рост числа заказов 100 → 220 сам по себе не доказывает сбой.")
        claim["refs"][0].update(start=3, end=3)
        # Keep a fact so this test exercises the quantitative hypothesis, not the fact requirement.
        other = self.local("note.md", "Учебный сценарий")["source"]
        fact = self.research(other)["claims"][0]
        fact["id"] = "C2"
        research["claims"].append(fact)
        self.save("research", research, error="требует rate_checks или cell_checks")
        evidence = self.call("cells", source, "--rows", "2", "3", "--columns", "orders")
        self.assertEqual(evidence["refs"], [{"source": source, "start": 2, "end": 3}])
        claim.update(evidence)
        self.save("research", research)
        verified = self.call("claim", "C1")["verified_cells"]
        self.assertEqual([x["actual"] for x in verified], ["100", "220"])

    def test_cell_checks_reject_fabricated_value_or_incomplete_multiline_citation(self):
        source = self.local("data.csv", 'label,count\n"two\nlines",100\n')["source"]
        self.save("brief", self.brief())
        research = self.research(source)
        claim = research["claims"][0]
        claim["text"] = "Значение 100"
        evidence = self.call("cells", source, "--rows", "2", "--columns", "count")
        self.assertEqual(evidence["refs"][0]["end"], 3)
        claim.update(evidence)
        claim["cell_checks"][0]["expected"] = "101"
        self.save("research", research, error="не совпадает")
        claim["cell_checks"][0]["expected"] = "100"
        claim["refs"][0]["end"] = 2
        self.save("research", research, error="за границы ссылок")
        self.call("cells", source, "--rows", "3", "--columns", "count", error="началом CSV")

    def test_rate_evidence_is_reusable_and_snapshot_counts_are_still_checked(self):
        source = self.local("data.csv", "week,yes,all\na,9,10\na,1,90\nb,0,0\n")["source"]
        evidence = self.call("rates", source, "--by", "week", "--numerator", "yes", "--denominator", "all", "--evidence")
        self.save("brief", self.brief())
        research = self.research(source)
        research["claims"][0].update(text="Для a: 10/100 = 10%", refs=evidence["refs"], rate_checks=evidence["rate_checks"])
        self.save("research", research)
        self.assertEqual(self.call("claim", "C1")["verified_rates"][0]["percent"], 10)

    def test_check_and_recover_are_read_only_while_paused(self):
        path = self.draft("brief.json", self.brief())
        self.call("pause")
        before = self.call("status")
        checked = self.call("check", "brief", path)
        self.assertTrue(checked["valid"])
        self.assertFalse(checked["can_save"])
        recovered = self.call("recover")
        self.assertNotIn("suggested_command", recovered["drafts"][0])
        self.assertEqual(self.call("status"), before)
        self.call("resume")
        self.assertIn("--expect", self.call("recover")["drafts"][0]["suggested_command"])

    def test_selected_rate_evidence_keeps_original_line_numbers(self):
        source = self.local("data.csv", "week,yes,all\na,2,4\nb,3,4\n")["source"]
        evidence = self.call("rates", source, "--by", "week", "--numerator", "yes", "--denominator", "all",
                             "--group", "week=b", "--evidence")
        self.assertEqual(evidence["refs"], [{"source": source, "start": 3, "end": 3}])
        self.assertEqual(evidence["rate_checks"][0]["expected"], {"numerator": 3, "denominator": 4})
        self.call("rates", source, "--by", "week", "--numerator", "yes", "--denominator", "all",
                  "--group", "missing=x", error="только столбцы")

    def test_recover_distinguishes_invalid_pending_and_already_saved_drafts(self):
        path = self.draft("brief.json", self.brief())
        self.draft("research.json", {"comparison": "incomplete"})
        self.draft("strategy.json", {})
        broken = self.root / "test" / "drafts" / "brief-broken.json"
        broken.write_text('{"question":', encoding="utf-8")
        self.save("brief", self.brief())
        before = self.call("status")
        drafts = {x["file"].split("/")[-1]: x for x in self.call("recover")["drafts"]}
        self.assertTrue(drafts["brief.json"]["same_as_checkpoint"])
        self.assertFalse(drafts["brief-broken.json"]["valid"])
        self.assertFalse(drafts["research.json"]["valid"])
        self.assertIn("предыдущие этапы", drafts["strategy.json"]["error"])
        self.assertEqual(self.call("status"), before)
        self.call("check", "brief", path)

    def test_check_rejects_duplicate_json_keys_without_changing_state(self):
        path = self.root / "duplicate.json"
        path.write_text('{"question":"first","question":"second"}', encoding="utf-8")
        before = self.call("status")
        self.call("check", "brief", str(path), error="повторное поле")
        self.assertEqual(self.call("status"), before)

    def test_recover_reports_malformed_csv_in_one_draft_without_losing_other_results(self):
        source = self.local("broken.csv", 'label,count\n"unfinished')["source"]
        self.save("brief", self.brief())
        self.draft("brief.json", self.brief())
        research = self.research(source)
        claim = research["claims"][0]
        claim.update(text="Счётчик 100", refs=[{"source": source, "start": 2, "end": 2}],
                     cell_checks=[{"source": source, "line": 2, "column": "count", "expected": "100"}])
        self.draft("research.json", research)
        before = self.call("status")
        drafts = self.call("recover")["drafts"]
        self.assertTrue(drafts[0]["same_as_checkpoint"])
        self.assertFalse(drafts[1]["valid"])
        self.assertTrue(drafts[1]["error"])
        self.assertEqual(self.call("status"), before)

    def test_timeout_logging_records_unknown_cost_without_inventing_usage(self):
        self.assertEqual(self.call("cost")["measurement_status"], "unmeasured")
        self.call("pause")
        self.call("log", "--kind", "model", "--label", "Ошибка запроса", "--seconds", "484",
                  "--outcome", "timeout", "--attempts", "1")
        cost = self.call("cost")
        self.assertEqual(cost["known_logged_cost"], {})
        self.assertIsNone(cost["entries"][0]["input_tokens"])
        self.assertEqual(cost["entries"][0]["outcome"], "timeout")
        self.assertEqual(cost["logged_seconds"]["model"], 484)
        self.call("log", "--kind", "model", "--label", "Ошибка", "--attempts", "0", error="положительное")


if __name__ == "__main__":
    unittest.main()
