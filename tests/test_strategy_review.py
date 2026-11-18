import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_budget as support
import test_strategist as cases
from strategist import budget, qwen, recovery, state, strategy_review


class StrategyReviewTests(unittest.TestCase):
    case_setup = support.BudgetTests.setUp
    seed_compact_case = support.BudgetTests.seed_compact_case
    compact = support.BudgetTests.compact
    compact_reply = staticmethod(support.BudgetTests.compact_reply)

    def setUp(self):
        self.case_setup()
        options = self.seed_compact_case()
        self.call("resume")
        recovery.save_draft(self.folder, "options", options)
        self.strategy = support.BudgetTests.strategy_fixture()
        recovery.save_draft(self.folder, "strategy", self.strategy)

    def call(self, *args, error=None):
        return cases.CaseTests.call(self, *args, case_id="sample", error=error)

    def register(self, result, error=None):
        path = self.root / "result.json"
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return self.call("review-strategy", "--record", str(path), error=error)

    def answer(self, packet, verdict="clear", **extra):
        return {**{key: packet[key] for key in ("case", "version", "ticket")},
                "verdict": verdict, "findings": [], **extra}

    def test_owner_view_and_idempotent_assignment_survive_pause(self):
        self.assertEqual(self.call("review-strategy")["status"], "pending")
        self.call("report", "--for-owner", error="pending")
        def assign(_):
            with state.case(self.folder, write=True) as data:
                return strategy_review.begin(data)
        with ThreadPoolExecutor(max_workers=2) as pool:
            submissions = list(pool.map(assign, range(2)))
        packet, = [item["packet"] for item in submissions if item["dispatch"]]
        self.assertIn("instructions", packet)
        self.assertEqual(packet["metrics"][0]["value"], "48/80 = 60%")
        self.assertEqual(packet["sources"]["S001"]["lines"]["1"], "yes,all,note")
        self.assertFalse({"journal", "history", "materials", "allowed_urls"} & packet.keys())
        self.call("pause")
        self.assertEqual(self.call("review-strategy", "--packet"), packet)
        answer = self.answer(packet)
        self.register(answer, error="приостановлен")
        self.call("resume")
        self.register(answer)
        revision = self.call("status")["revision"]
        self.register(answer)
        self.assertFalse(self.call("review-strategy", "--begin")["dispatch"])
        self.assertEqual(self.call("status")["revision"], revision)
        self.assertIn("Существенных замечаний не обнаружено", self.call("report", "--for-owner"))
        self.assertIsNone(self.call("status")["active_decision"])
        self.assertEqual(self.call("status")["next"], "decision")

    def test_findings_must_quote_a_field_and_name_available_evidence(self):
        packet = self.call("review-strategy", "--begin")["packet"]
        finding = {"field": "rationale", "statement": self.strategy["rationale"], "basis": ["S001:L2", "M1"],
                   "reason": "Нужно явно назвать основание", "fix": "Привязать выбор к доле из M1"}
        revision = self.call("status")["revision"]
        for change, expected in (
            ({"field": "invented"}, "точной цитатой"), ({"statement": "Такой цитаты нет"}, "точной цитатой"),
            ({"basis": ["S001:L99"]}, "основания"), ({"basis": ["C999"]}, "основания"),
            ({"fix": ""}, "непустой текст"), ({"fix": "Контакт x@example.org"}, "чувствительные"),
        ):
            with self.subTest(change=change):
                self.register(self.answer(packet, "changes_requested", findings=[{**finding, **change}]), error=expected)
        self.register(self.answer(packet, "clear", findings=[finding]), error="замечания")
        self.register(self.answer(packet, "changes_requested"), error="замечания")
        self.register(self.answer(packet, "clear", action="decide"), error="поля")
        self.assertEqual(self.call("status")["revision"], revision)
        self.register(self.answer(packet, "changes_requested", findings=[finding]))
        self.call("report", "--for-owner", error="changes_requested")
        self.assertIn(finding["fix"], self.call("report"))
        self.register(self.answer(packet), error="уже проверена")
        self.assertEqual(self.call("context", "--stage", "strategy")["saved"]["strategy"], self.strategy)

    def test_stale_answer_cannot_close_a_new_assignment_or_changed_strategy(self):
        first = self.call("review-strategy", "--begin")["packet"]
        self.call("review-strategy", "--interrupt", first["ticket"])
        second = self.call("review-strategy", "--begin")["packet"]
        self.assertEqual(first["version"], second["version"])
        self.assertNotEqual(first["ticket"], second["ticket"])
        self.register(self.answer(first), error="назначению")
        self.register({**self.answer(second), "case": "foreign"}, error="другому кейсу")
        self.call("reopen", "strategy", "--reason", "Уточнён план", "--expect", str(self.call("status")["revision"]))
        self.call("review-strategy", error="сохраните этап")
        recovery.save_draft(self.folder, "strategy", {**self.strategy, "revisit": "Проверить в следующей когорте."})
        current = self.call("review-strategy", "--begin")["packet"]
        self.assertNotEqual(current["version"], second["version"])
        self.register(self.answer(second), error="назначению")
        self.register(self.answer(current, "inconclusive"), error="reason")
        self.register(self.answer(current, "inconclusive", reason="Нет основания для решения о расширении."))
        self.call("report", "--for-owner", error="inconclusive")

    def test_unrelated_source_keeps_the_review_and_replacement_reopens_research(self):
        packet = self.call("review-strategy", "--begin")["packet"]
        self.register(self.answer(packet))
        (self.folder / "materials" / "note.txt").write_text("Другая тема", encoding="utf-8")
        self.call("ingest", "--file", "note.txt", "--kind", "synthetic")
        self.assertEqual(self.call("review-strategy")["status"], "clear")
        self.assertEqual(self.call("review-strategy")["version"], packet["version"])
        (self.folder / "materials" / "new.csv").write_text("yes,all,note\n50,80,Исправлено\n", encoding="utf-8")
        self.call("ingest", "--file", "new.csv", "--kind", "synthetic", "--replaces", "S001")
        self.assertEqual(self.call("status")["next"], "research")
        self.call("report", "--for-owner", error="сохраните этап")

    def test_budget_and_failed_live_review_return_without_duplicate_calls(self):
        def never(*args):
            self.fail("При недостаточном бюджете провайдер вызываться не должен")
        insufficient, calls = self.compact(never, limit=1000)
        self.assertEqual((calls, insufficient["budget"]["decision"]), (0, "insufficient"))
        self.assertEqual(self.call("review-strategy")["status"], "pending")
        def timeout(*args):
            raise TimeoutError()
        failed, calls = self.compact(timeout)
        self.assertEqual((calls, failed["runs"][0]["outcome"]), (1, "transport_error"))
        self.assertGreater(failed["usage"]["by_stage"]["strategy_review"]["reserved_tokens"], 0)
        self.assertEqual(self.call("review-strategy")["status"], "pending")
        self.call("report", "--for-owner", error="pending")
        malformed, calls = self.compact(lambda *args: self.compact_reply({"verdict": "clear"}))
        self.assertEqual((calls, malformed["runs"][0]["outcome"]), (1, "review_error"))
        self.assertEqual(self.call("review-strategy")["status"], "pending")

    def test_pause_during_review_preserves_receipt_for_resume_without_a_model(self):
        def answer(account, token, body, timeout):
            packet = json.loads(json.loads(body)["messages"][1]["content"])
            self.call("pause")
            return self.compact_reply(self.answer(packet))

        result, calls = self.compact(answer)
        self.assertEqual(calls, 1)
        self.assertTrue(self.call("status")["paused"])
        pending = self.call("recover")["strategy_review"]
        self.assertEqual(pending["status"], "in_progress")
        self.assertEqual(pending["result_file"], result["runs"][0]["stop"]["result_file"])
        self.call("resume")
        self.call("review-strategy", "--record", pending["result_file"])
        self.assertEqual(self.call("review-strategy")["status"], "clear")
        self.assertEqual(self.compact(answer)[1], 0)

    def test_native_runner_can_finish_existing_strategy_without_starting_writer(self):
        def answer(account, token, body, timeout):
            payload = json.loads(body)
            self.assertNotIn("tools", payload)
            self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
            return self.compact_reply(self.answer(json.loads(payload["messages"][1]["content"])))
        with patch.dict(os.environ, {qwen.TOKEN_ENV: "test-token"}), \
                patch.object(qwen, "cloudflare_chat", side_effect=answer) as provider, \
                patch.object(qwen.shutil, "which") as which:
            result = self.call("qwen-run", "--account", "a" * 32, "--limit", "200000")
        self.assertEqual(result["budget"]["decision"], "human_decision")
        self.assertEqual(provider.call_count, 1)
        which.assert_not_called()
        self.assertEqual(budget.runs(self.folder)[0]["stage"], "strategy_review")
