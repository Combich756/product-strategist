import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strategist import budget, cli, qwen, state

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
    request = Request(
        base + "/chat/completions",
        data=json.dumps({"model": "local", "messages": [{"role": "user", "content": "Проверь кейс"}],
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
    "transport": "transport_error",
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


if __name__ == "__main__":
    unittest.main()
