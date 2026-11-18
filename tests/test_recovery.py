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

    def test_research_covers_questions_or_records_what_to_check(self):
        source = self.local("note.md", "Часть окон ещё открыта.\n")["source"]
        brief = self.brief(require_question_results=True)
        brief["questions"] = ["Что показывает выгрузка?", "Дозрела ли вся когорта?"]
        self.save("brief", brief)
        research = self.research(source)
        answered = {"claims": ["C1"], "decision_impact": "Есть предварительное наблюдение."}
        research["question_results"] = {"Q1": answered}
        before = self.call("status")
        self.call("check", "research", self.draft("research.json", research), error="заполните вопросы Q2")
        self.save("research", research, error="заполните вопросы Q2")
        self.assertEqual(self.call("status"), before)
        research["question_results"]["Q2"] = {
            "gap": "Окно последней когорты открыто.",
            "decision_impact": "Порог запуска пока нельзя применить.",
        }
        self.save("research", research, error="Q2.next_check")
        research["question_results"]["Q2"]["next_check"] = "Получить повторный срез после закрытия окна."
        answered["claims"] = ["C99"]
        self.save("research", research, error="вывод 'C99' отсутствует")
        answered["claims"] = ["C1"]
        research["question_results"]["Q3"] = answered
        self.save("research", research, error="неизвестные вопросы Q3")
        del research["question_results"]["Q3"]
        self.assertEqual(self.call("status"), before)
        preview = self.call("check", "research", self.draft("research.json", research))
        self.assertIn("question_coverage", preview["validated"])
        self.save("research", research)
        self.assertEqual(self.call("status")["next"], "options")
        rendered = self.call("report")
        self.assertIn("C1 [S001:L1–L1]", rendered)
        self.assertIn("Получить повторный срез после закрытия окна.", rendered)

    def test_evidence_context_reads_verified_metrics_and_current_claims(self):
        source = self.local("cohort.csv", "returned,eligible\n36,60\n")["source"]
        formula = {"numerator": "returned", "denominator": "eligible"}
        brief = self.brief(require_question_results=True, metrics=[
            {"id": "M1", "label": "Удержание", "unit": "%", "formula": formula},
        ])
        self.save("brief", brief)
        research = self.research(source)
        evidence = self.call("rates", source, "--numerator", "returned", "--denominator", "eligible", "--evidence")
        research["claims"][0].update(evidence)
        research["metrics"] = {"M1": {"claim": "C1", "check": 1}}
        research["question_results"] = {"Q1": {"claims": ["C1"], "decision_impact": "Сохраняем направление."}}
        self.save("research", research)
        self.call("pause")
        status = self.call("status")
        context = self.call("context", "--evidence")
        self.assertEqual(context["next"], "options")
        self.assertTrue(context["paused"])
        self.assertEqual(context["research_questions"], {"Q1": "Где проблема?"})
        self.assertEqual(context["question_results"], research["question_results"])
        self.assertEqual(context["decision_metrics"][0]["value"], "36/60 = 60%")
        self.assertEqual(context["decision_metrics"][0]["refs"], evidence["refs"])
        self.assertEqual(context["claim_refs"], {"C1": evidence["refs"]})
        self.assertNotIn("text", context["sources"][source])
        self.assertNotIn("saved", context)
        self.assertEqual(self.call("status"), status)
        self.call("resume")
        replacement = self.local("correction.csv", "returned,eligible\n42,60\n", "--replaces", source)["source"]
        context = self.call("context", "--evidence")
        self.assertEqual(context["next"], "research")
        self.assertEqual(list(context["sources"]), [replacement])
        self.assertNotIn("question_results", context)
        self.assertNotIn("value", context["decision_metrics"][0])

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

    def test_check_and_save_reject_wrong_percentage_with_verified_counts(self):
        source = self.local("data.csv", "segment,activated,invited\nagencies,20,80\nstores,48,80\n")["source"]
        self.save("brief", self.brief())
        research = self.research(source)
        claim = research["claims"][0]
        claim.update(self.call("rates", source, "--by", "segment", "--numerator", "activated",
                               "--denominator", "invited", "--evidence"))
        claim["text"] = "Первый импорт сделали 48/80 магазинов (99%) и 20/80 агентств (25%)."
        before = self.call("status")
        path = self.draft("research.json", research)
        self.call("check", "research", path, error="пересчёт дал 48/80 = 60%")
        self.save("research", research, error="пересчёт дал 48/80 = 60%")
        self.assertFalse(self.call("recover")["drafts"][0]["valid"])
        self.assertEqual(self.call("status"), before)

        claim["text"] = claim["text"].replace("99%", "60%")
        self.assertTrue(self.call("check", "research", self.draft("research.json", research))["can_save"])

    def test_rate_references_render_checked_counts_and_undefined_rate(self):
        source = self.local("data.csv", "segment,activated,invited\na,1,3\nb,0,0\n")["source"]
        self.save("brief", self.brief())
        research = self.research(source)
        claim = research["claims"][0]
        claim.update(self.call("rates", source, "--by", "segment", "--numerator", "activated",
                               "--denominator", "invited", "--evidence"))
        for text, error in (
            ("Группа A — {{rate:3}}", "расчёт №3 отсутствует"),
            ("Группа A — {{rate:0}}", "синтаксис"),
            ("Группа A — {{rate:1}}, B — 99%", "без дробей и процентов"),
            ("Группа A — 2/3, B — {{rate:2}}", "без дробей и процентов"),
        ):
            with self.subTest(text=text):
                claim["text"] = text
                self.call("check", "research", self.draft("research.json", research), error=error)

        claim["text"] = "В пробе A — {{rate:1}}, B — {{rate:2}}."
        self.save("research", research)
        rendered = "В пробе A — 1/3 = 33.33%, B — 0/0, доля не определена."
        audit = self.call("claim", "C1")
        self.assertEqual(audit["rendered_text"], rendered)
        self.assertEqual(audit["claim"]["text"], claim["text"])
        self.assertIn(rendered, self.call("report"))
        self.assertEqual(self.call("context", "--stage", "research")["saved"]["research"], research)

    def test_paid_pilot_metrics_cover_the_brief_and_appear_without_prose(self):
        source = self.local("paid-result.csv", "segment,offers,paid,refunded,support_minutes\ncafes,40,9,1,540\n")["source"]
        gross_formula = {"numerator": "paid", "denominator": "offers"}
        net_formula = {**gross_formula, "subtract": "refunded"}
        self.save("brief", self.brief(metrics=[
            {"id": "M1", "label": "Первые оплаты", "unit": "%", "formula": gross_formula,
             "threshold": {"op": ">=", "value": 20}},
            {"id": "M2", "label": "Оплаты после возврата", "unit": "%", "formula": net_formula},
            {"id": "M3", "label": "Сопровождение", "unit": "мин", "formula": {"column": "support_minutes"},
             "threshold": {"op": "<=", "value": 480}},
        ]))
        self.assertIn("Оплаты после возврата | Ожидает исследования", self.call("report"))
        research = self.research(source)
        args = ("--by", "segment", "--numerator", "paid", "--denominator", "offers", "--evidence")
        gross = self.call("rates", source, *args)
        net = self.call("rates", source, *args, "--subtract", "refunded")
        support = self.call("cells", source, "--rows", "2", "--columns", "support_minutes")
        research["claims"][0].update(refs=gross["refs"], rate_checks=gross["rate_checks"] + net["rate_checks"],
                                     cell_checks=support["cell_checks"])
        research["metrics"] = {"M1": {"claim": "C1", "check": 1}, "M3": {"claim": "C1", "check": 1}}

        before = self.call("status")
        self.call("check", "research", self.draft("research.json", research), error="research.metrics: M2")
        self.save("research", research, error="research.metrics: M2")
        self.assertEqual(self.call("status"), before)
        research["metrics"]["M2"] = {"claim": "C1", "check": 1}
        self.save("research", research, error="Пересчёт для M2 расходится с формулой")
        research["metrics"]["M2"]["check"] = 2
        self.save("research", research)
        self.call("pause")

        table = self.call("report").split("## Метрики решения\n", 1)[1].split("## Исследование", 1)[0]
        self.assertIn("| M1 · Первые оплаты | 9/40 = 22.5% | ≥ 20 % · выполнено | C1 · S001:L2–L2 |", table)
        self.assertIn("| M2 · Оплаты после возврата | 8/40 = 20% | — | C1 · S001:L2–L2 |", table)
        self.assertIn("| M3 · Сопровождение | 540 мин | ≤ 480 мин · выше лимита на 60 мин |", table)

    def test_decision_metrics_show_gaps_and_do_not_pass_with_zero_denominator(self):
        source = self.local("data.csv", "yes,all\n0,0\n")["source"]
        self.save("brief", self.brief(metrics=[
            {"id": "M1", "label": "Доля", "unit": "%", "formula": {"numerator": "yes", "denominator": "all"},
             "threshold": {"op": ">=", "value": 20}},
            {"id": "M2", "label": "Длительность", "unit": "мин", "formula": {"column": "minutes"}},
        ]))
        research = self.research(source)
        evidence = self.call("rates", source, "--numerator", "yes", "--denominator", "all", "--evidence")
        research["claims"][0].update(evidence)
        research["metrics"] = {"M1": {"claim": "C1", "check": 1}, "M2": {"gap": "В выгрузке нет журнала времени."}}
        self.save("research", research)
        report = self.call("report")
        self.assertIn("| M1 · Доля | 0/0, доля не определена | ≥ 20 % |", report)
        self.assertIn("| M2 · Длительность | Нет данных. В выгрузке нет журнала времени. |", report)

    def test_threshold_uses_fields_from_every_row_of_the_checked_rate(self):
        source = self.local(
            "cohorts.csv",
            "group,returned,accounts,observation_unit,calculation_rule,denominator_scope,window_open\n"
            "dashboard,36,100,account,d7_calendar,registrations,40\n"
            "mature,48,100,account,d7_calendar,closed_windows,0\n"
            "mixed,12,30,account,d7_calendar,closed_windows,0\n"
            "mixed,24,70,device,elapsed,registrations,1\n"
            "blank,36,100,account,,closed_windows,0\n",
        )["source"]
        dashboard = self.local("dashboard.csv", "returned,accounts\n36,100\n")["source"]
        formula = {"numerator": "returned", "denominator": "accounts"}
        conditions = {"observation_unit": "account", "calculation_rule": "d7_calendar",
                      "denominator_scope": "closed_windows", "window_open": "0"}
        groups = ["dashboard", "mature", "mixed", "blank"]
        brief = self.brief(metrics=[
            {"id": f"M{number}", "label": label, "unit": "%", "formula": formula,
             "threshold": {"op": "<=", "value": 50}, "comparability": conditions}
            for number, label in enumerate([*groups, "raw dashboard"], 1)
        ])
        brief["metrics"][-1]["comparability"] = {"window_open": "0"}
        self.save("brief", brief)
        research = self.research(source, dashboard)
        args = ("--numerator", "returned", "--denominator", "accounts", "--evidence")
        checks = self.call("rates", source, "--by", "group", *args)
        research["claims"][0].update(checks)
        research["claims"][1].update(self.call("rates", dashboard, *args))
        index = {item["group"]["group"]: number for number, item in enumerate(checks["rate_checks"], 1)}
        research["metrics"] = {f"M{number}": {"claim": "C1", "check": index[group]}
                               for number, group in enumerate(groups, 1)}
        research["metrics"]["M5"] = {"claim": "C2", "check": 1}

        before = self.call("status")
        check = self.call("check", "research", self.draft("research.json", research))
        self.assertEqual(self.call("status"), before)
        self.assertTrue(check["valid"])
        self.assertIn("M1, M3, M4, M5", check["warnings"][0])
        self.assertEqual(check["metric_comparability"]["M1"]["status"], "incompatible")
        self.save("research", research)
        measured = self.call("context", "--evidence")["decision_metrics"]
        observed, mature, mixed, blank, missing = measured
        for item in (observed, mixed, blank, missing):
            self.assertEqual(item["value"], "36/100 = 36%")
            self.assertNotIn("passes", item)
        self.assertEqual(mature["value"], "48/100 = 48%")
        self.assertTrue(mature["passes"])
        self.assertEqual(mature["comparability_check"], {"status": "compatible", "issues": []})
        self.assertEqual([issue["actual"] for issue in observed["comparability_check"]["issues"]],
                         ["registrations", "40"])
        self.assertEqual([(item["column"], item["ref"]["start"]) for item in mixed["comparability_check"]["issues"]],
                         [(column, 5) for column in conditions])
        self.assertEqual(blank["comparability_check"]["status"], "unverified")
        self.assertEqual(blank["comparability_check"]["issues"][0]["kind"], "missing_value")
        self.assertEqual(missing["comparability_check"]["issues"],
                         [{"column": "window_open", "expected": "0", "kind": "missing_column"}])
        report = self.call("report")
        self.assertIn("36/100 = 36% | ≤ 50 % · порог не оценён", report)
        self.assertIn(r"window\_open = 40 на S001:L2–L2", report)
        self.assertIn("48/100 = 48% | ≤ 50 % · выполнено", report)

    def test_incompatible_scalar_keeps_number_without_assumed_unit(self):
        source = self.local("duration.csv", "duration,measure_unit\n120,s\n30,min\n")["source"]
        definition = {"unit": "мин", "formula": {"column": "duration"}, "threshold": {"op": "<=", "value": 60},
                      "comparability": {"measure_unit": "min"}}
        brief = self.brief(metrics=[{"id": "M1", "label": "Первый замер", **definition},
                                    {"id": "M2", "label": "Второй замер", **definition}])
        for invalid in ({}, {"measure_unit": 7}, {" ": "min"}):
            with self.subTest(invalid=invalid):
                brief["metrics"][0]["comparability"] = invalid
                self.save("brief", brief, error="M1.comparability")
        brief["metrics"][0]["comparability"] = definition["comparability"]
        self.save("brief", brief)
        research = self.research(source)
        research["claims"][0].update(self.call("cells", source, "--rows", "2", "3", "--columns", "duration"))
        research["metrics"] = {"M1": {"claim": "C1", "check": 1}, "M2": {"claim": "C1", "check": 2}}
        self.save("research", research)
        first, second = self.call("context", "--evidence")["decision_metrics"]
        self.assertEqual(first["value"], "120")
        self.assertEqual(first["comparability_check"]["issues"][0]["actual"], "s")
        self.assertNotIn("passes", first)
        self.assertEqual(second["value"], "30 мин")
        self.assertTrue(second["passes"])

    def test_adjusted_rates_sum_over_rows_and_reject_refunds_above_payments(self):
        content = "segment,offers,paid,refunded\ncafes,15,7,1\ncafes,25,2,0\n"
        source = self.local("data.csv", content)["source"]
        args = ("--by", "segment", "--numerator", "paid", "--denominator", "offers", "--subtract", "refunded")
        result = self.call("rates", source, *args, "--evidence")
        self.assertEqual(result["refs"], [{"source": source, "start": 2, "end": 3}])
        self.assertEqual(result["rate_checks"][0]["expected"], {"numerator": 8, "denominator": 40})
        invalid = self.local("data.csv", content.replace("cafes,25,2,0", "cafes,25,2,3"))["source"]
        self.call("rates", invalid, *args, error="вычитаемое ≤ числитель")

    def test_new_support_log_can_reopen_only_related_findings(self):
        source = self.local("first.csv", "minutes\n430\n")["source"]
        note = self.local("policy.md", "Срок пилота — один месяц.")["source"]
        self.save("brief", self.brief(metrics=[{
            "id": "M1", "label": "Сопровождение", "unit": "мин", "formula": {"column": "minutes"},
            "threshold": {"op": "<=", "value": 480},
        }]))
        previous = self.research(source, note)
        previous["claims"][0].update(self.call("cells", source, "--rows", "2", "--columns", "minutes"))
        previous["metrics"] = {"M1": {"claim": "C1", "check": 1}}
        self.save("research", previous)
        self.save("options", {"comparison": "Выбор масштаба", "options": [
            {"id": f"O{index}", "kind": kind, "claims": [claim],
             **dict.fromkeys(("title", "action", "benefit", "objection", "economics"), "Учебный вариант")}
            for index, (kind, claim) in enumerate((("status_quo", "C2"), ("close", "C2"), ("change", "C1")), 1)
        ]})

        update = self.local("support-log.csv", "minutes\n540\n")["source"]
        pending = self.call("status")
        self.assertEqual(pending["next"], "strategy")
        self.assertEqual(pending["pending_source_reviews"], [update])
        self.assertIn("Материалы на оценке применимости.** " + update, self.call("report"))
        args = (update, "--outcome", "relevant", "--reason", "540 минут превышают предел 480",
                "--expect", str(pending["revision"]))
        self.call("review-source", *args, "--claims", "C99", error="Укажите существующие")
        self.assertEqual(self.call("status"), pending)
        revised = self.call("review-source", *args, "--claims", "C1")
        self.assertEqual(revised["next"], "research")
        self.assertEqual(revised["pending_source_reviews"], [])
        self.assertEqual(revised["impact"]["affected_claims"], ["C1"])
        self.assertEqual(revised["impact"]["affected_metrics"], ["M1"])
        self.assertEqual(revised["impact"]["affected_options"], ["O3"])
        self.assertEqual(self.call("history")[-1]["checkpoints"]["research"], previous)
        self.assertIn("540 минут превышают предел 480", self.call("report"))

    def test_source_review_links_questions_and_metrics_with_gaps(self):
        policy = self.local("pilot.md", "Полный D7 требует закрытия всех окон.")["source"]
        brief = self.brief(require_question_results=True, metrics=[{
            "id": "M5", "label": "Итоговый D7 W43", "unit": "%",
            "formula": {"numerator": "returned", "denominator": "eligible"},
        }])
        brief["questions"] = ["Когда доступна метрика?", "Какой итог W43?", "Проверены ли события?"]
        self.save("brief", brief)
        previous = self.research(policy)
        previous["metrics"] = {"M5": {"gap": "Часть окон ещё открыта."}}
        previous["question_results"] = {
            "Q1": {"claims": ["C1"], "decision_impact": "Дождаться закрытия окон."},
            "Q2": {"gap": "Неполная W43.", "decision_impact": "Порог пока неприменим.",
                   "next_check": "Запросить итоговый срез."},
            "Q3": {"gap": "Сверки нет.", "decision_impact": "Проверить полноту счётчиков.",
                   "next_check": "Запросить журнал."},
        }
        self.save("research", previous)
        source = self.local("mature.csv", "returned,eligible\n48,100\n")["source"]
        audit = self.local("audit.md", "Счётчики сверены с журналом.")["source"]
        pending = self.call("status")
        args = ("--reason", "Поступил полный срез.", "--expect", str(pending["revision"]))
        self.call("review-source", source, "--outcome", "relevant", *args, "--metrics", "M99", error="метрик")
        self.call("review-source", source, "--outcome", "relevant", *args, "--questions", "Q99", error="вопросов")
        self.call("review-source", source, "--outcome", "relevant", *args, "--metrics", "M5", "M5", error="метрик")
        self.call("review-source", source, "--outcome", "irrelevant", *args, "--questions", "Q2",
                  error="У несвязанного источника")
        self.assertEqual(self.call("status"), pending)
        self.assertEqual(self.call("history"), [])

        first = self.call("review-source", source, "--outcome", "relevant", *args,
                          "--metrics", "M5", "--questions", "Q2")
        self.assertEqual(first["next"], "research")
        self.assertEqual(first["impact"]["affected_claims"], [])
        self.assertEqual(first["impact"]["affected_questions"], ["Q2"])
        self.assertEqual(first["impact"]["affected_metrics"], ["M5"])
        self.assertNotIn("metric_changes", self.call("context", "--evidence"))
        second = self.call("review-source", audit, "--outcome", "relevant", "--reason", "Сверка счётчиков.",
                           "--expect", str(first["revision"]), "--questions", "Q2", "Q3", "--metrics", "M5")
        self.assertEqual(second["impact"]["affected_questions"], ["Q2", "Q3"])
        self.assertEqual(second["impact"]["affected_metrics"], ["M5"])
        self.assertEqual(second["impact"]["source_changes"][1]["questions"], ["Q2", "Q3"])
        self.assertEqual(self.call("journal")[-1]["metrics"], ["M5"])
        self.assertEqual(self.call("history")[-1]["checkpoints"]["research"], previous)

        revised = self.research(policy, source, audit)
        proof = self.call("rates", source, "--numerator", "returned", "--denominator", "eligible", "--evidence")
        revised["claims"][1].update(proof, text="D7 W43 — {{rate:1}}.")
        revised["metrics"] = {"M5": {"claim": "C2", "check": 1}}
        revised["question_results"] = {
            "Q1": previous["question_results"]["Q1"],
            "Q2": {"claims": ["C2"], "decision_impact": "Можно сравнить с порогом."},
            "Q3": {"claims": ["C3"], "decision_impact": "Счётчики проверены."},
        }
        self.save("research", revised)
        context = self.call("context", "--evidence")
        self.assertEqual(context["next"], "options")
        self.assertEqual(context["metric_changes"], [{
            "id": "M5", "label": "Итоговый D7 W43", "before": {"gap": "Часть окон ещё открыта."},
            "after": {"value": "48/100 = 48%"},
        }])
        self.assertEqual(context["decision_metrics"][0]["refs"], proof["refs"])
        self.assertIn("Нет данных. Часть окон ещё открыта. → 48/100 = 48%", self.call("report"))

    def test_review_context_prefills_only_current_unaffected_facts_and_bindings(self):
        support_log = self.local("minutes.csv", "minutes\n430\n")["source"]
        pricing = self.local("cost.csv", "minutes\n60\n")["source"]
        brief = self.brief(require_question_results=True, metrics=[
            {"id": f"M{number}", "label": label, "unit": "мин", "formula": {"column": "minutes"}}
            for number, label in enumerate(("Сопровождение", "Обучение", "Резерв"), 1)
        ])
        brief["questions"] = ["Что известно об обучении?", "Каков срок?", "Каков резерв?"]
        self.save("brief", brief)
        previous = self.research(support_log, pricing, pricing)
        for claim, source in zip(previous["claims"], (support_log, pricing)):
            claim.update(self.call("cells", source, "--rows", "2", "--columns", "minutes"))
        previous["claims"][2]["type"] = "hypothesis"
        previous["metrics"] = {"M1": {"claim": "C1", "check": 1}, "M2": {"claim": "C2", "check": 1},
                               "M3": {"gap": "Нет оценки резерва."}}
        previous["question_results"] = {
            "Q1": {"claims": ["C2"], "decision_impact": "Учесть трудозатраты обучения."},
            "Q2": {"claims": ["C2"], "decision_impact": "Оценить срок."},
            "Q3": {"claims": ["C2"], "gap": "Резерв неизвестен.", "next_check": "Проверить резерв.",
                   "decision_impact": "Перепроверить доступный ресурс."},
        }
        self.save("research", previous)
        audit = self.local("audit.md", "Оценка сроков требует пересмотра.")["source"]
        updated = self.local("minutes-v2.csv", "minutes\n540\n", "--replaces", support_log)["source"]
        status = self.call("status")
        self.call("review-source", audit, "--outcome", "relevant", "--questions", "Q2", "--metrics", "M3",
                  "--reason", "Уточнилась оценка срока.", "--expect", str(status["revision"]))
        before = self.call("status")
        context = self.call("context", "--review")
        self.assertEqual(self.call("status"), before)
        review = context["review"]
        self.assertEqual(review["baseline_revision"], before["impact"]["baseline_revision"])
        draft = review["draft"]
        self.assertEqual(draft["claims"], [previous["claims"][1]])
        self.assertEqual(draft["metrics"], {"M2": previous["metrics"]["M2"]})
        self.assertEqual(draft["question_results"], {"Q1": previous["question_results"]["Q1"]})
        self.assertEqual([item["id"] for item in review["previous"]["claims"]], ["C1", "C3"])
        self.call("check", "research", self.draft("research-review.json", draft), error="comparison")

        recalculated = self.research(updated)["claims"][0]
        recalculated.update(self.call("cells", updated, "--rows", "2", "--columns", "minutes"))
        draft["claims"].append(recalculated)
        draft.update(comparison="Пересчитаны трудозатраты.", gaps=["Оценка резерва готовится."])
        draft["metrics"].update(M1={"claim": "C1", "check": 1}, M3=previous["metrics"]["M3"])
        draft["question_results"].update(Q2={"claims": ["C1"], "decision_impact": "Срок пересмотрен."},
                                         Q3=previous["question_results"]["Q3"])
        self.save("research", draft)
        self.assertEqual(self.call("context", "--stage", "research")["saved"]["research"], draft)
        self.assertEqual(self.call("status")["next"], "options")

    def test_review_context_starts_fresh_if_the_brief_changed(self):
        source = self.local("note.md", "Учебный результат.")["source"]
        self.save("brief", self.brief())
        self.save("research", self.research(source))
        self.call("context", "--review", error="нужен открытый пересмотр")
        new_policy = self.local("policy.md", "Изменилась цель кейса.")["source"]
        status = self.call("status")
        self.call("review-source", new_policy, "--outcome", "relevant", "--questions", "Q1",
                  "--reason", "Изменилась цель.", "--expect", str(status["revision"]))
        original = self.call("context", "--review")
        self.assertEqual(len(original["review"]["draft"]["claims"]), 1)
        self.call("reopen", "brief", "--reason", "Уточнена постановка.", "--expect", str(original["revision"]))
        brief = self.brief()
        brief["goal"] = "Новая цель."
        self.save("brief", brief)
        current = self.call("context", "--review")
        self.assertEqual(current["review"]["draft"]["claims"], [])
        self.assertEqual(current["review"]["previous"]["claims"][0]["id"], "C1")

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
