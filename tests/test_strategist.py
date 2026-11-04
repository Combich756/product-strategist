import contextlib
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from strategist.analysis import rates
from strategist.cli import main
from strategist.sources import normalize_url, parse_html, public_addresses
from strategist.state import UserError


class CaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.call("init", "Что делать с падением активации?")
        self.materials = self.root / "test" / "materials"

    def call(self, command, *args, error=None):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = main(["--root", str(self.root), command, "test", *args])
        if error is None:
            self.assertEqual(code, 0, stderr.getvalue())
            return json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertIn(error, json.loads(stderr.getvalue())["error"])

    def local(self, filename, text, *args):
        (self.materials / filename).write_text(text, encoding="utf-8")
        return self.call("ingest", "--file", filename, "--kind", "synthetic", *args)

    def save(self, stage, value, *, expected=None, error=None):
        revision = self.call("status")["revision"] if expected is None else expected
        path = self.root / "input.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.call("save", stage, str(path), "--expect", str(revision), error=error)

    def brief(self, **extra):
        return dict(question="Вопрос", goal="Цель", audience="Аудитория", constraints="Срок",
                    available_data="Данные", questions=["Где проблема?"], **extra)

    def research(self, *sources):
        return {"comparison": "Сопоставление", "gaps": ["Нужна проверка"], "claims": [
            {"id": f"C{index}", "type": "fact", "text": "Учебное наблюдение", "scope": "demo",
             "limits": "Только учебный набор", "refs": [{"source": source, "start": 1, "end": 1}]}
            for index, source in enumerate(sources, 1)
        ]}

    def test_resume_and_stale_revision(self):
        self.save("brief", self.brief(), expected=0, error="Ревизия изменилась")
        self.call("pause")
        self.save("brief", self.brief(), error="приостановлен")
        self.assertEqual(self.call("context")["next"], "brief")
        self.call("resume")
        self.save("brief", self.brief())
        revision = self.call("status")["revision"]
        self.save("brief", self.brief())
        self.assertEqual(self.call("status")["revision"], revision)
        self.assertEqual(self.call("context")["next"], "research")

    def test_replacements_update_impact_and_keep_history(self):
        first = self.local("a.md", "Первая версия")["source"]
        second = self.local("b.md", "Другое наблюдение")["source"]
        self.save("brief", self.brief())
        self.save("research", self.research(first, second))
        reused = self.local("a.md", "Первая версия")
        self.assertTrue(reused["reused"])
        self.assertEqual(self.call("status")["next"], "options")
        self.local("a.md", "Исправлено")
        self.call("revoke", second, "--reason", "отозван")
        status = self.call("status")
        self.assertEqual(status["next"], "research")
        self.assertEqual(status["impact"]["affected_claims"], ["C1", "C2"])
        self.assertEqual(len(self.call("history")), 1)
        self.assertEqual(self.call("view", first)["lines"][0]["text"], "Первая версия")
        self.save("research", self.research(first), error="нужен активный источник")

    def test_reject_bad_reference_and_synthetic_scope(self):
        source = self.local("a.md", "Одна строка")["source"]
        self.save("brief", self.brief())
        research = self.research(source)
        claim = research["claims"][0]
        claim["refs"][0]["end"] = 2
        self.save("research", research, error="неверный диапазон")
        claim["refs"][0]["end"] = 1
        claim["scope"] = "case"
        self.save("research", research, error="требует scope=demo")
        claim["scope"] = "demo"
        self.save("research", research)
        self.save("options", {"comparison": "один", "options": []}, error="минимум 3")

    def test_refuses_file_outside_case_and_unapproved_url(self):
        (self.root / "test" / "outside.txt").write_text("вне материалов", encoding="utf-8")
        self.call("ingest", "--file", "../outside.txt", "--kind", "own", error="в materials/")
        with patch("strategist.sources.fetch_web") as fetch:
            self.call("ingest", "--url", "https://example.org/data", error="списке разрешений")
            fetch.assert_not_called()

    def test_cost_keeps_unknown_separate(self):
        self.call("log", "--kind", "model", "--label", "IDE без счётчика", "--seconds", "15")
        self.call("log", "--kind", "tool", "--label", "открытая копия", "--amount", "0", "--currency", "RUB")
        self.call("log", "--kind", "tool", "--label", "некорректно", "--amount", "NaN", error="конечной")
        result = self.call("cost")
        self.assertEqual(result["known_logged_cost"], {"RUB": "0"})
        self.assertEqual(result["log_entries_without_cost"], 1)
        self.assertEqual(result["logged_seconds"]["model"], 15)


class ToolTests(unittest.TestCase):
    def test_weighted_rates_and_zero_denominator(self):
        source = {"format": "csv", "text": "period,yes,all\na,9,10\na,1,90\nb,0,0\n"}
        groups = rates(source, ["period"], "yes", "all")["groups"]
        self.assertEqual(groups[0]["percent"], 10)
        self.assertEqual(groups[0]["refs"], [{"start": 2, "end": 2}, {"start": 3, "end": 3}])
        self.assertIsNone(groups[1]["percent"])

    def test_missing_and_duplicate_counts(self):
        for row in ("a,,10", "a,4,3", "a,4,10\na,4,10"):
            with self.subTest(row=row), self.assertRaises(UserError):
                rates({"format": "csv", "text": "period,yes,all\n" + row}, ["period"], "yes", "all")

    def test_html_extraction(self):
        html = b"<head><title>skip</title></head><main><p>Hello &amp; goodbye</p><script>skip</script></main>"
        self.assertEqual(parse_html(html, "utf-8"), "Hello & goodbye")

    def test_refuses_non_public_dns_and_url_credentials(self):
        for url in ("http://example.org", "https://x@example.org", "https://example.org:443", "https://example.org/#a"):
            with self.subTest(url=url), self.assertRaises(UserError):
                normalize_url(url)
        with patch("strategist.sources.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]), self.assertRaisesRegex(UserError, "непубличный"):
            public_addresses("example.org")


if __name__ == "__main__":
    unittest.main()
