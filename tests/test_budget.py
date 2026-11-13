import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strategist import budget, cli, qwen, recovery, state

FAKE_QWEN = """#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base = sys.argv[sys.argv.index("--openai-base-url") + 1]
case = os.environ["STRATEGIST_QWEN_CASE"]
stage = os.environ["STRATEGIST_QWEN_STAGE"]
root = Path(os.environ["STRATEGIST_TEST_CASES"])
mode = os.environ["STRATEGIST_TEST_MODE"]
assert "STRATEGIST_CF_TOKEN" not in os.environ

def call():
    content = "Проверь кейс"
    if mode == "privacy":
        content += " contact" + "@" + "example.invalid"
    request = Request(
        base + "/chat/completions",
        data=json.dumps({"model": "local", "messages": [{"role": "user", "content": content}],
                         "stream": True, "max_tokens": 7000}).encode(),
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                 "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as result:
            return result.read().decode()
    except HTTPError as error:
        return json.loads(error.read())["error"]["code"]

first = call()
if mode == "save":
    assert '"tool_calls"' in first and '"usage"' in first and "[DONE]" in first
    command = [sys.executable, "-B", "-m", "strategist", "--root", str(root)]
    revision = json.loads(subprocess.check_output(command + ["status", case]))["revision"]
    draft = root / case / "drafts" / (stage + ".json")
    draft.parent.mkdir(exist_ok=True)
    if stage == "brief":
        artifact = {name: "Учебный кейс" for name in
                    ("question", "goal", "audience", "constraints", "available_data")}
        artifact["questions"] = ["Что проверяем?"]
    else:
        assert stage == "research"
        artifact = {
            "comparison": "Сопоставление", "gaps": ["Мало данных"],
            "claims": [{"id": "C1", "text": "Учебный факт", "type": "fact", "scope": "demo",
                        "limits": "Учебный пример", "refs": [{"source": "S001", "start": 1, "end": 1}]}],
        }
    draft.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    subprocess.check_call(command + ["save", case, stage, str(draft), "--expect", str(revision)],
                          stdout=subprocess.DEVNULL)
    assert call() == "checkpoint_reached"
    sys.exit(1)

expected = {
    "budget": "request_budget", "missing": "missing_usage", "rejected": "provider_error",
    "transport": "transport_error", "privacy": "privacy_review",
}[mode]
assert call() == expected, (first, expected)
sys.exit(1)
"""


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = state.case_dir(self.root, "sample")
        state.init(self.folder, "Разобрать продукт")

    def call(self, *args):
        out = io.StringIO()
        with tempfile.TemporaryFile(mode="w+") as errors, redirect_stderr(errors), redirect_stdout(out):
            code = cli.main(["--root", str(self.root), args[0], "sample", *args[1:]])
            errors.seek(0)
            self.assertEqual(code, 0, errors.read())
        return json.loads(out.getvalue())

    def seed_compact_case(self):
        (self.folder / "materials" / "data.csv").write_text(
            "yes,all,note\n48,80,Подробности исходника\n", encoding="utf-8",
        )
        self.call("ingest", "--file", "data.csv", "--kind", "synthetic")
        evidence = self.call("rates", "S001", "--numerator", "yes", "--denominator", "all", "--evidence")
        brief = dict.fromkeys("question goal audience constraints available_data".split(), "Пилот")
        brief["questions"] = ["Как оценить эффект?"]
        recovery.save_draft(self.folder, "brief", brief)
        recovery.save_draft(self.folder, "research", {
            "comparison": "Доступны наблюдения пилота", "gaps": ["Эффект неизвестен"],
            "claims": [{"id": "C1", "text": "Доля активаций — {{rate:1}}.", "type": "fact", "scope": "demo",
                        "limits": "Учебный пример", "refs": evidence["refs"], "rate_checks": evidence["rate_checks"]}],
        })
        self.call("pause")
        return {
            "comparison": "Сравнить труд и измеримый эффект", "assumptions": ["Затраты уточнить у команды"],
            "options": [
                {"id": f"O{index}", "kind": kind, "title": kind, "action": "Действие",
                 "benefit": "Польза", "objection": "Возражение", "economics": "Затраты", "claims": ["C1"]}
                for index, kind in enumerate(("status_quo", "close", "change"), start=1)
            ],
        }

    def compact(self, complete, *, limit=200_000):
        with patch.dict(os.environ, {qwen.TOKEN_ENV: "test-token"}), \
                patch.object(qwen, "cloudflare_chat", side_effect=complete) as provider, \
                patch.object(qwen.shutil, "which") as find_qwen:
            result = self.call("qwen-run", "--compact", "--account", "a" * 32,
                               "--limit", str(limit), "--max-output-tokens", "4096")
        find_qwen.assert_not_called()
        return result, provider.call_count

    @staticmethod
    def compact_reply(artifact):
        return 200, json.dumps({
            "choices": [{"message": {"content": json.dumps(artifact)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 900},
        }).encode()

    def test_compact_resumes_legacy_budget_with_one_request_per_checkpoint(self):
        options = self.seed_compact_case()
        strategy = {
            "option": "O3", "rationale": "Проверим эффект пилота", "revisit": "Проверить результат",
            "assumptions": ["Порог уточнить по базе сравнения"],
            "product": {**dict.fromkeys("audience job alternatives direction tradeoffs economics".split(), "Пилот"),
                        "claims": ["C1"]},
            "communication": {
                **dict.fromkeys("audience situation barrier message proof channel_roles promise_limit".split(), "Пилот"),
                "proof_claims": ["C1"],
            },
            "test": {**dict.fromkeys("expected metric denominator control window threshold guardrails".split(), "Пилот"),
                     "claims": ["C1"]},
        }
        with budget.connection(self.folder, write=True) as conn:
            conn.execute("""
                CREATE TABLE qwen_runs (
                    id TEXT PRIMARY KEY, stage TEXT NOT NULL, model TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL DEFAULT 'running',
                    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                    reserved_tokens INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
                    measured_requests INTEGER NOT NULL DEFAULT 0, rejected_requests INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                INSERT INTO qwen_runs (id, stage, model, started_at, outcome, input_tokens, output_tokens,
                                       requests, measured_requests)
                VALUES ('legacy', 'options', ?, ?, 'request_budget', 147990, 1018, 8, 8)
            """, (budget.DEFAULT_MODEL, state.utc_now()))
        seen = []

        def complete(account, token, body, timeout):
            payload = json.loads(body)
            self.assertEqual(payload["max_completion_tokens"], 4096)
            self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
            self.assertNotIn("tools", payload)
            self.assertNotIn("Подробности исходника", body.decode())
            context = json.loads(payload["messages"][1]["content"])
            self.assertEqual(context["saved"]["research"]["claims"][0]["text"], "Доля активаций — 48/80 = 60%.")
            seen.append(list(context["saved"]))
            return self.compact_reply(strategy if "options" in context["saved"] else options)

        result, calls = self.compact(complete)
        self.assertEqual(calls, 2)
        self.assertEqual(seen, [["brief", "research"], ["brief", "research", "options"]])
        self.assertEqual([run["outcome"] for run in result["runs"]], ["checkpoint_saved"] * 2)
        self.assertEqual(result["budget"]["decision"], "human_decision")
        self.assertEqual(result["budget"]["known_spent_tokens"], 153_208)
        self.assertEqual(result["usage"]["measured_requests"], 10)
        self.assertEqual(self.call("status")["checkpoints"], ["brief", "research", "options", "strategy"])
        records = budget.runs(self.folder)
        self.assertEqual([record["mode"] for record in records], ["qwen_code", "compact", "compact"])
        self.assertEqual(budget.stage_samples(self.folder, records, "options", budget.DEFAULT_MODEL), [])

    def test_compact_reports_exhausted_output_without_a_draft(self):
        self.seed_compact_case()
        reply = {
            "choices": [{"finish_reason": "length", "message": {"content": None}}],
            "usage": {
                "prompt_tokens": 2815, "completion_tokens": 4096,
                "completion_tokens_details": {"reasoning_tokens": 4096},
            },
        }
        result, calls = self.compact(lambda *args: (200, json.dumps(reply).encode()))
        self.assertEqual(calls, 1)
        self.assertEqual(result["runs"][0]["stop"], {
            "reason": "truncated_completion", "finish_reason": "length",
            "completion_tokens": 4096, "reasoning_tokens": 4096,
        })
        self.assertEqual(result["budget"]["known_spent_tokens"], 6911)
        recovered = self.call("recover")
        self.assertTrue(recovered["paused"])
        self.assertEqual(recovered["next"], "options")
        self.assertEqual(recovered["drafts"], [])

    def test_compact_invalid_reply_is_recoverable_and_pauses(self):
        options = self.seed_compact_case()
        options["options"][0]["claims"] = ["C404"]
        result, calls = self.compact(lambda *args: self.compact_reply(options))
        self.assertEqual(calls, 1)
        self.assertEqual(result["runs"][0]["stop"]["reason"], "draft_invalid_or_stale")
        self.assertEqual(result["budget"]["known_spent_tokens"], 2100)
        recovered = self.call("recover")
        self.assertTrue(recovered["paused"])
        self.assertEqual(recovered["next"], "options")
        self.assertFalse(recovered["drafts"][0]["valid"])
        self.assertEqual(recovered["drafts"][0]["stage"], "options")

    def test_compact_does_not_save_options_after_research_is_reopened(self):
        options = self.seed_compact_case()

        def complete(*args):
            with state.case(self.folder, write=True) as data:
                state.archive(data, "research", "Проверить новое основание")
                state.event(data, "reopen", stage="research")
            return self.compact_reply(options)

        result, calls = self.compact(complete)
        self.assertEqual(calls, 1)
        self.assertEqual(result["budget"]["next"], "research")
        stop = result["runs"][0]["stop"]
        self.assertEqual(stop["reason"], "draft_invalid_or_stale")
        self.assertIn("Ревизия изменилась", stop["detail"])
        self.assertTrue(Path(stop["draft"]).is_file())
        self.assertEqual(self.call("status")["checkpoints"], ["brief"])

    def test_compact_checks_budget_and_private_data_before_dispatch(self):
        self.seed_compact_case()
        before = self.call("status")
        result, calls = self.compact(None, limit=1000)
        self.assertEqual(calls, 0)
        self.assertEqual(result["budget"]["estimate_source"], "request")
        self.assertEqual(result["budget"]["decision"], "insufficient")
        self.assertEqual(self.call("status"), before)

        with state.case(self.folder, write=True, while_paused=True) as data:
            data["request"] = "Устаревший запрос person" + "@" + "example.invalid"
            state.event(data, "fixture")
        result, calls = self.compact(None)
        self.assertEqual(calls, 0)
        self.assertEqual(result["runs"][0]["stop"], {"reason": "privacy_review", "findings": {"email": 1}})
        self.assertEqual(result["usage"]["requests"], 0)

    def test_forecast_uses_completed_measured_stages_and_leaves_legacy_case_unchanged(self):
        original = self.call("status")
        self.assertEqual(self.call("budget", "--limit", "80000")["decision"], "needs_estimate")
        for number, amount in enumerate((10_000, 20_000, 30_000)):
            folder = state.case_dir(self.root, f"history-{number}")
            state.init(folder, "Учебный прогон")
            run = budget.start_run(folder, "brief", budget.DEFAULT_MODEL)
            self.assertIsNone(budget.reserve_request(folder, run, "brief", 100_000, amount))
            budget.settle_request(folder, run, amount, (amount - 200, 200))
            budget.finish_run(folder, run, "checkpoint_saved" if number < 2 else "provider_error")
        forecast = self.call("budget", "--limit", "29000")
        self.assertEqual(forecast["sample_count"], 2)
        self.assertEqual(forecast["estimate_tokens"], 20_000)
        self.assertEqual(forecast["decision"], "insufficient")
        self.assertEqual(forecast["shortfall_tokens"], 1000)
        self.assertEqual(self.call("status"), original)

    def test_parallel_reservations_share_budget_without_changing_checkpoint_revision(self):
        before = self.call("status")
        ids = [budget.start_run(self.folder, "brief", budget.DEFAULT_MODEL) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(
                lambda run: budget.reserve_request(self.folder, run, "brief", 3000, 2000), ids
            ))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(next(item for item in results if item)["reason"], "request_budget")
        self.assertEqual(budget.usage(self.folder)["reserved_tokens"], 2000)
        self.assertEqual(self.call("status"), before)

    def test_insufficient_forecast_does_not_start_qwen_or_resume_paused_case(self):
        before = self.call("pause")
        with patch.object(qwen.shutil, "which") as find_qwen:
            result = self.call("qwen-run", "--account", "a" * 32,
                               "--limit", "1000", "--estimate", "2000", "--reserve", "0")
        find_qwen.assert_not_called()
        self.assertEqual(result["runs"], [])
        self.assertEqual(result["budget"]["decision"], "insufficient")
        self.assertEqual(self.call("status"), before)

    def simulate(self, mode, *, limit, estimate, measured=(1200, 100), status=200):
        if os.name == "nt":
            self.skipTest("Локальная имитация Qwen использует POSIX-исполняемый файл.")
        fake = self.root / "qwen"
        fake.write_text(FAKE_QWEN, encoding="utf-8")
        fake.chmod(0o700)
        upstream = []

        def complete(account, token, body, timeout):
            self.assertEqual(account, "a" * 32)
            self.assertEqual(token, "test-token")
            payload = json.loads(body)
            upstream.append(payload)
            if mode == "transport":
                raise TimeoutError("Нет ответа после отправки")
            if status == 429:
                return 429, b'{"errors":[{"code":4006,"message":"daily quota"}]}'
            response = {
                "id": "completion-test", "created": 1, "model": budget.DEFAULT_MODEL,
                "choices": [{
                    "index": 0, "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "call_test", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}},
                    ]},
                }],
            }
            if measured is not None:
                response["usage"] = {"prompt_tokens": measured[0], "completion_tokens": measured[1]}
            return status, json.dumps(response).encode()

        with patch.dict(os.environ, {
            "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
            qwen.TOKEN_ENV: "test-token", "STRATEGIST_TEST_CASES": str(self.root),
            "STRATEGIST_TEST_MODE": mode,
        }), patch.object(qwen, "cloudflare_chat", side_effect=complete):
            result = self.call("qwen-run", "--account", "a" * 32, "--limit", str(limit),
                               "--estimate", str(estimate), "--reserve", "0",
                               "--max-output-tokens", "512", "--wall-time", "15")
        for request in upstream:
            self.assertEqual(request["model"], budget.DEFAULT_MODEL)
            self.assertEqual(request["max_completion_tokens"], 512)
            self.assertFalse(request["stream"])
        return result, upstream

    def test_saves_two_checkpoints_then_stops_before_next_stage_estimate(self):
        (self.folder / "materials" / "note.md").write_text("Учебный факт\n", encoding="utf-8")
        self.call("ingest", "--file", "note.md", "--kind", "synthetic")
        self.call("pause")
        result, upstream = self.simulate("save", limit=4700, estimate=2400)
        self.assertEqual(len(upstream), 2)
        self.assertEqual([run["outcome"] for run in result["runs"]], ["checkpoint_saved", "checkpoint_saved"])
        self.assertEqual(result["budget"]["next"], "options")
        self.assertEqual(result["budget"]["decision"], "insufficient")
        cost = self.call("cost", "--summary")
        self.assertEqual(cost["qwen_usage"]["by_stage"]["brief"]["input_tokens"], 1200)
        self.assertEqual(cost["qwen_usage"]["by_stage"]["research"]["output_tokens"], 100)
        self.assertEqual(self.call("status")["checkpoints"], ["brief", "research"])

    def test_blocks_followup_request_when_remaining_budget_is_too_small(self):
        result, upstream = self.simulate("budget", limit=3000, estimate=1000, measured=(1300, 450))
        self.assertEqual(len(upstream), 1)
        self.assertEqual(result["runs"][0]["stop"]["reason"], "request_budget")
        self.assertEqual(result["budget"]["remaining_tokens"], 1250)
        self.assertTrue(self.call("status")["paused"])

    def test_unmeasured_response_keeps_reservation_and_stops_followup(self):
        result, upstream = self.simulate("missing", limit=3000, estimate=1000, measured=None)
        self.assertEqual(len(upstream), 1)
        self.assertEqual(result["runs"][0]["stop"]["reason"], "missing_usage")
        self.assertEqual(result["usage"]["measured_requests"], 0)
        self.assertGreater(result["budget"]["unmeasured_reserved_tokens"], 0)

    def test_quota_rejection_releases_reservation_and_stops_retry(self):
        result, upstream = self.simulate("rejected", limit=3000, estimate=1000, status=429)
        self.assertEqual(len(upstream), 1)
        self.assertEqual(result["runs"][0]["stop"]["status"], 429)
        self.assertEqual(result["usage"]["rejected_requests"], 1)
        self.assertEqual(result["budget"]["unmeasured_reserved_tokens"], 0)
        self.assertTrue(self.call("status")["paused"])

    def test_transport_loss_keeps_reservation_across_runs(self):
        result, upstream = self.simulate("transport", limit=3000, estimate=1000)
        self.assertEqual(len(upstream), 1)
        self.assertEqual(result["runs"][0]["stop"]["reason"], "transport_error")
        held = result["budget"]["unmeasured_reserved_tokens"]
        self.assertGreater(held, 0)
        forecast = self.call("budget", "--limit", "3000", "--estimate", "2500", "--reserve", "0")
        self.assertEqual(forecast["remaining_tokens"], 3000 - held)
        self.assertEqual(forecast["decision"], "insufficient")

    def test_sensitive_request_stops_before_reservation_and_provider_call(self):
        result, upstream = self.simulate("privacy", limit=3000, estimate=1000)
        self.assertEqual(upstream, [])
        self.assertEqual(result["runs"][0]["stop"], {"reason": "privacy_review", "findings": {"email": 1}})
        self.assertEqual(result["usage"]["requests"], 0)
        self.assertTrue(self.call("status")["paused"])


if __name__ == "__main__":
    unittest.main()
