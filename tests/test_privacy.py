import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strategist import cli, qwen, state

EMAIL = "person" + "@" + "example.invalid"
PHONE = "+7 (000) 000-00-00"
TOKEN = "gsk_" + "X" * 32


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.case = state.case_dir(self.root, "sample")
        state.init(self.case, "Учебный кейс")

    def call(self, *args, stdin=None, expected=0):
        out, errors = io.StringIO(), io.StringIO()
        with patch("sys.stdin", stdin if stdin is not None else io.StringIO()), \
                redirect_stdout(out), redirect_stderr(errors):
            code = cli.main(["--root", str(self.root), args[0], "sample", *args[1:]])
        self.assertEqual(code, expected, errors.getvalue())
        return (json.loads(out.getvalue()) if code == 0 else None), errors.getvalue()

    def material(self, text):
        (self.case / "materials" / "data.csv").write_text(text, encoding="utf-8")
        return "ingest", "--file", "data.csv", "--kind", "synthetic"

    def test_contacts_need_confirmation_and_saved_snapshot_is_masked(self):
        before, _ = self.call("status")
        command = self.material(f"email,phone,count\n{EMAIL},{PHONE},7\n")
        _, errors = self.call(*command, expected=2)
        self.assertIn("Нужна ручная проверка", errors)
        self.assertNotIn(EMAIL, errors)
        _, errors = self.call(*command, stdin=TerminalInput("нет\n"), expected=2)
        self.assertIn("Сохранение отменено", errors)
        self.assertEqual(self.call("status")[0], before)
        imported, prompt = self.call(*command, stdin=TerminalInput("да\n"))
        self.assertIn("[да/нет]", prompt)
        self.assertNotIn(EMAIL, prompt)
        self.assertNotIn(PHONE, prompt)
        self.assertEqual(imported["masked_contacts"], {"email": 1, "phone": 1})
        snapshot, _ = self.call("view", imported["source"], "--start", "2", "--end", "2")
        self.assertEqual(snapshot["lines"][0]["text"], "[PII],[PII],7")
        self.assertNotIn(EMAIL.encode(), (self.case / "state.sqlite3").read_bytes())
        journal, _ = self.call("journal")
        self.assertEqual(journal[-1]["masked_contacts"], {"email": 1, "phone": 1})

    def test_confirming_from_guarded_qwen_cannot_save_even_masked_copy(self):
        command = self.material(f"email\n{EMAIL}\n")
        with patch.dict("os.environ", {qwen.RUN_ENV: "test-run"}):
            _, error = self.call(*command, stdin=TerminalInput("да\n"), expected=2)
        self.assertIn("Нужна ручная проверка", error)
        self.assertEqual(self.call("sources")[0], {})

    def test_token_is_rejected_without_echoing_its_value(self):
        command = self.material(f"account\n{TOKEN}\n")
        before, _ = self.call("status")
        _, errors = self.call(*command, stdin=TerminalInput("да\n"), expected=2)
        self.assertIn("ключи и токены", errors)
        self.assertNotIn(TOKEN, errors)
        self.assertNotIn("[да/нет]", errors)
        self.assertEqual(self.call("status")[0], before)

    def test_token_url_is_rejected_before_entering_allowlist(self):
        url = f"https://example.invalid/report?%61ccess_token={TOKEN}"
        _, errors = self.call("allow-web", url, expected=2)
        self.assertNotIn(TOKEN, errors)
        self.assertEqual(self.call("context", "--compact")[0]["allowed_urls"], [])

    def test_check_refuses_sensitive_draft_and_plain_numbers_are_allowed(self):
        command = self.material("order_id,day,count\n89991234567,2026-11-12,7\n")
        self.assertEqual(self.call(*command)[0]["source"], "S001")
        draft = self.root / "brief.json"
        artifact = {field: "Учебный кейс" for field in
                    ("question", "goal", "audience", "constraints", "available_data")}
        artifact.update(audience=EMAIL, questions=["Что проверяем?"])
        draft.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
        _, errors = self.call("check", "brief", str(draft), expected=2)
        self.assertIn("Черновик этапа", errors)
        self.assertNotIn(EMAIL, errors)

    def test_new_case_records_only_the_masked_request(self):
        def create(answer):
            out, errors = io.StringIO(), io.StringIO()
            with patch("sys.stdin", TerminalInput(answer)), redirect_stdout(out), redirect_stderr(errors):
                code = cli.main(["--root", str(self.root), "init", "new-case", f"Разобрать обращения {EMAIL}"])
            self.assertNotIn(EMAIL, errors.getvalue())
            return code

        self.assertEqual(create("нет\n"), 2)
        self.assertFalse((self.root / "new-case").exists())
        self.assertEqual(create("да\n"), 0)
        with state.case(self.root / "new-case") as data:
            self.assertEqual(data["request"], "Разобрать обращения [PII]")


if __name__ == "__main__":
    unittest.main()
