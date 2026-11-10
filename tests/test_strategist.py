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
            result = stdout.getvalue()
            return result if command == "report" else json.loads(result)
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

    def test_rate_check_validates_counts_and_complete_citations(self):
        source = self.local("sample.csv", "week,yes,all\na,9,10\na,1,90\nb,0,0\n")["source"]
        self.save("brief", self.brief())
        research = self.research(source)
        claim = research["claims"][0]
        claim["refs"][0].update(start=2, end=2)
        check = {
            "source": source, "group": {"week": "a"}, "numerator": "yes", "denominator": "all",
            "expected": {"numerator": 11, "denominator": 100},
        }
        claim["rate_checks"] = [check]
        revision = self.call("status")["revision"]
        self.save("research", research, error="пересчёт дал 10/100")
        check["expected"]["numerator"] = 10
        self.save("research", research, error="строки расчёта 3–3 выходят за границы")
        self.assertEqual(self.call("status")["revision"], revision)
        claim["refs"][0]["end"] = 3
        self.save("research", research)

        verified = self.call("claim", "C1")
        self.assertEqual(verified["verified_rates"][0]["percent"], 10)
        self.assertEqual(verified["sources"][0]["lines"], [
            {"line": 2, "text": "a,9,10"}, {"line": 3, "text": "a,1,90"},
        ])
        self.assertIn("week=a — 10/100 = 10%", self.call("report"))

    def test_rate_check_uses_the_original_snapshot_after_replacement(self):
        first = self.local("sample.csv", "yes,all\n2,4\n")["source"]
        self.save("brief", self.brief())
        research = self.research(first)
        claim = research["claims"][0]
        claim["refs"][0].update(start=2, end=2)
        check = {
            "source": first, "group": {}, "numerator": "yes", "denominator": "all",
            "expected": {"numerator": 2, "denominator": 4},
        }
        claim["rate_checks"] = [check]
        self.save("research", research)
        second = self.local("sample.csv", "yes,all\n3,4\n")["source"]
        impact = self.call("status")["impact"]
        self.assertEqual(impact["affected_claims"], ["C1"])
        self.call("claim", "C1", error="отсутствует в выбранном исследовании")
        claim["refs"][0]["source"] = second
        check["source"] = second
        check["expected"]["numerator"] = 3
        self.save("research", research)

        before = self.call("claim", "C1", "--revision", str(impact["baseline_revision"]))
        self.assertTrue(before["archived"])
        self.assertEqual(before["sources"][0]["current_status"], "superseded")
        self.assertEqual(before["verified_rates"][0]["percent"], 50)
        self.assertEqual(self.call("claim", "C1")["verified_rates"][0]["percent"], 75)

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
