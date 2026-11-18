"""Render the saved artifacts for human review."""

import re
from html import escape

from .analysis import format_rate
from .checkpoints import cell_checks, decision_metrics, rate_checks, render_claim_text, research_questions
from .sources import line_location, metadata, source_lines
from .state import analysis_context, archived_brief, followup, need, next_stage, pending_source_reviews, source_review_archive, strategy_review_status


def claim_evidence(state, claim_id, revision):
    saved = state["checkpoints"]
    analysis = analysis_context(state)
    if revision is not None:
        archive = next((entry for entry in state["history"] if entry["revision"] == revision), None)
        need(archive is not None, f"Архивная ревизия {revision} отсутствует. Список ревизий: history.")
        saved = archive["checkpoints"]
        analysis = archive.get("analysis")
    claim = next((item for item in saved.get("research", {}).get("claims", []) if item["id"] == claim_id), None)
    need(claim is not None, f"Вывод {claim_id} отсутствует в выбранном исследовании.")
    excerpts = []
    for ref in claim["refs"]:
        source = state["sources"][ref["source"]]
        preview_end = min(ref["end"], ref["start"] + 11)
        preview = source_lines(source)[ref["start"] - 1:preview_end]
        location = {"sheet": source["sheet"]} if "sheet" in source else {}
        timing = {"timing": source["timing"]} if "timing" in source else {}
        excerpts.append({
            **ref, **location, **timing, "origin": source["origin"], "kind": source["kind"],
            "current_status": source["status"], "captured_at": source["captured_at"],
            "text_sha256": source["text_sha256"],
            "lines": [{"line": index, "text": line[:500], **line_location(source, index)}
                      for index, line in enumerate(preview, ref["start"])],
            "preview_truncated": preview_end < ref["end"] or any(len(line) > 500 for line in preview),
        })
    verified_rates = rate_checks(state["sources"], claim, {})
    return {
        "case": state["case"], "revision": state["revision"] if revision is None else revision,
        "archived": revision is not None, "analysis": analysis, "claim": claim, "sources": excerpts,
        "rendered_text": render_claim_text(claim, verified_rates), "verified_rates": verified_rates,
        "verified_cells": cell_checks(state["sources"], claim, {}),
    }


def inline_text(text):
    return re.sub(r"([\\`*_\[\]()|])", r"\\\1", escape(" ".join(text.split())))


def rate_summary(result):
    group = ", ".join(f"{key}={value}" for key, value in result["group"].items()) or "вся таблица"
    return f"`{result['source']}`, {inline_text(group)} — {format_rate(result)}"


def ref_ranges(refs):
    return ", ".join(f"{ref['source']}:L{ref['start']}–L{ref['end']}" for ref in refs)


def metric_changes(state, metrics, cache):
    affected = set((state["impact"] or {}).get("affected_metrics", []))
    saved = state["checkpoints"]
    if not affected or "research" not in saved:
        return []
    archive = source_review_archive(state)
    if archive is None:
        return []
    brief = saved["brief"]
    previous_brief = archived_brief(state, archive)
    if previous_brief is None:
        return []
    previous_definitions = {metric["id"]: metric for metric in previous_brief.get("metrics", [])}
    comparable = {
        metric["id"] for metric in brief.get("metrics", [])
        if metric["id"] in affected and previous_definitions.get(metric["id"]) == metric
    }
    if not comparable:
        return []
    previous = {metric["id"]: metric for metric in decision_metrics(
        state["sources"], previous_brief, archive["checkpoints"]["research"], cache
    )}
    changes = []
    for metric in metrics:
        key = metric["id"]
        if key in comparable:
            before = {field: previous[key][field] for field in ("value", "gap") if field in previous[key]}
            after = {field: metric[field] for field in ("value", "gap") if field in metric}
            if before != after:
                changes.append({"id": key, "label": metric["label"], "before": before, "after": after})
    return changes


def evidence_context(state):
    saved = state["checkpoints"]
    result = {
        "sources": {key: metadata(source) for key, source in state["sources"].items()
                    if source["status"] == "active"},
    }
    if "brief" in saved:
        brief = saved["brief"]
        research = saved.get("research")
        result["brief"] = {key: brief[key] for key in ("question", "goal", "audience", "constraints", "available_data")}
        result["research_questions"] = research_questions(brief)
        result["require_question_results"] = brief.get("require_question_results", False)
        cache = {}
        result["decision_metrics"] = decision_metrics(state["sources"], brief, research, cache)
        changes = metric_changes(state, result["decision_metrics"], cache)
        if changes:
            result["metric_changes"] = changes
        if research is not None:
            if result["require_question_results"]:
                result["question_results"] = research["question_results"]
            result["claim_refs"] = {claim["id"]: claim["refs"] for claim in research["claims"]}
    return result


def metric_value(metric):
    if "gap" in metric:
        return f"Нет данных. {metric['gap']}"
    return metric.get("value", "Ожидает исследования")


def metric_comparability_note(check):
    if check["status"] == "compatible":
        return "условия сопоставимости выполнены"
    parts = ["несопоставимо" if check["status"] == "incompatible" else "сопоставимость не подтверждена"]
    for issue in check["issues"]:
        column = issue["column"]
        if issue["kind"] == "missing_column":
            parts.append(f"нет столбца {column}")
            continue
        location = ref_ranges([issue["ref"]])
        if issue["kind"] == "missing_value":
            parts.append(f"пустое значение {column} на {location}")
        else:
            parts.append(f"{column} = {issue['actual']} на {location}, требуется {issue['expected']}")
    return " · ".join(parts)


def metric_table(metrics):
    lines = ["", "## Метрики решения", "",
             "| Показатель | Значение | Условие | Основание |", "|---|---|---|---|"]
    for metric in metrics:
        label = f"{metric['id']} · {metric['label']}"
        value = metric_value(metric)
        comparison = metric.get("comparability_check")
        condition = "—"
        if "threshold" in metric:
            target = metric["threshold"]
            symbol = "≥" if target["op"] == ">=" else "≤"
            condition = f"{symbol} {target['value']} {metric['unit']}"
            if metric.get("passes") is True:
                condition += " · выполнено"
            elif metric.get("passes") is False:
                reason = "выше лимита" if target["op"] == "<=" else "ниже порога"
                condition += f" · {reason} на {metric['deviation']}"
            elif comparison and comparison["status"] != "compatible":
                condition += " · порог не оценён"
        basis = f"{metric['claim']} · {ref_ranges(metric['refs'])}" if "refs" in metric else "—"
        if comparison:
            basis += " · " + metric_comparability_note(comparison)
        lines.append("| " + " | ".join(map(inline_text, (label, value, condition, basis))) + " |")
    return lines


def items(lines, record, labels):
    for key, title in labels.items():
        lines.append(f"- **{title}.** {record[key]}")


def source_timing_label(source):
    timing = source.get("timing")
    if not timing:
        return ""
    parts = []
    for key, label in (("period", "период данных"), ("validity", "срок действия")):
        if key in timing:
            dates = timing[key]
            parts.append(f"{label} {dates.get('start', '…')} — {dates.get('end', '…')}")
    if "data_as_of" in timing:
        parts.append(f"срез {timing['data_as_of']}")
    if "timezone" in timing:
        parts.append(f"часовой пояс {timing['timezone']}")
    parts.append(f"импорт {source['captured_at']}")
    return " · " + " · ".join(parts)


def report(state, as_of):
    saved = state["checkpoints"]
    cache = {}
    metrics = []
    lines = [
        f"# Стратегия · {state['case']}",
        "", f"**Исходный запрос.** {state['request']}",
        "", f"**Состояние.** Ревизия {state['revision']}, следующий этап `{next_stage(state)}`"
        + (", кейс приостановлен." if state["paused"] else "."),
    ]
    analysis = analysis_context(state)
    if analysis:
        lines += ["", f"**Момент анализа.** {analysis['as_of']} ({analysis['timezone']})."]
        if analysis.get("periods"):
            lines += ["", "## Периоды сравнения", ""]
            for period in analysis["periods"]:
                label = escape(" ".join(period["label"].split()))
                lines.append(f"- {label} · {period['start']} — {period['end']}.")
    if "brief" in saved:
        brief = saved["brief"]
        lines += ["", "## Постановка", ""]
        items(lines, brief, {"question": "Вопрос", "goal": "Цель", "audience": "Для кого",
                             "constraints": "Ограничения", "available_data": "Материалы"})
        lines += ["", "Вопросы исследования", ""]
        if brief.get("require_question_results"):
            lines += [f"- **{key}.** {inline_text(question)}" for key, question in research_questions(brief).items()]
        else:
            lines += [f"- {question}" for question in brief["questions"]]
        metrics = decision_metrics(state["sources"], brief, saved.get("research"), cache)
        if metrics:
            lines += metric_table(metrics)
    if "research" in saved:
        research = saved["research"]
        lines += ["", "## Исследование", "", research["comparison"], ""]
        for claim in research["claims"]:
            refs = ref_ranges(claim["refs"])
            checks = rate_checks(state["sources"], claim, cache)
            text = render_claim_text(claim, checks)
            lines.append(f"- **{claim['id']} · {claim['type']} · {claim['scope']}.** {text}"
                         + (f" [{refs}]." if refs else "")
                         + f" **Граница вывода.** {claim['limits']}")
            if checks:
                lines.append("  - **Пересчёт по таблице.** " + ", ".join(map(rate_summary, checks)) + ".")
        lines += ["", "Открытые вопросы", ""]
        lines += [f"- {gap}" for gap in research["gaps"]]
        if saved["brief"].get("require_question_results"):
            lines += ["", "### Ответы на вопросы исследования", ""]
            claims = {claim["id"]: claim for claim in research["claims"]}
            for key, question in research_questions(saved["brief"]).items():
                result = research["question_results"][key]
                lines.append(f"- **{key} · {inline_text(question)}**")
                if "gap" in result:
                    lines.append(f"  - **Пробел.** {inline_text(result['gap'])}")
                if "claims" in result:
                    refs = [f"{claim} [{ref_ranges(claims[claim]['refs'])}]" for claim in result["claims"]]
                    lines.append("  - **Основания.** " + ", ".join(refs) + ".")
                lines.append(f"  - **Влияние на выбор.** {inline_text(result['decision_impact'])}")
                if "next_check" in result:
                    lines.append(f"  - **Следующая проверка.** {inline_text(result['next_check'])}")
    if "options" in saved:
        lines += ["", "## Варианты", "", saved["options"]["comparison"]]
        for option in saved["options"]["options"]:
            lines += ["", f"### {option['id']} · {option['title']}", ""]
            items(lines, option, {"action": "Действие", "benefit": "Польза", "objection": "Возражение",
                                  "economics": "Экономика"})
            lines.append("- **Основания.** " + ", ".join(option["claims"]))
        if saved["options"].get("assumptions"):
            lines += ["", "Допущения и оценки", "", *[f"- {x}" for x in saved["options"]["assumptions"]]]
    if "strategy" in saved:
        strategy = saved["strategy"]
        review = strategy_review_status(state)
        labels = {
            "pending": "Проверка черновика ожидает назначения", "in_progress": "Идёт проверка черновика",
            "clear": "Существенных замечаний не обнаружено", "changes_requested": "Требуются исправления",
            "inconclusive": "Для проверки недостаточно оснований",
        }
        lines += ["", f"**Независимая проверка стратегии.** {labels[review['status']]}."]
        result = review.get("result", {})
        if "reason" in result:
            lines.append(inline_text(result["reason"]))
        for item in result.get("findings", []):
            lines.append(f"- **{inline_text(item['field'])}.** «{inline_text(item['statement'])}». "
                         f"{inline_text(item['reason'])} ({', '.join(map(inline_text, item['basis']))}). "
                         f"Правка — {inline_text(item['fix'])}")
        lines += ["", "## Рекомендация", "", f"**{strategy['option']}.** {strategy['rationale']}",
                  "", f"**Пересмотр.** {strategy['revisit']}", "", "### Продукт", ""]
        items(lines, strategy["product"], {"audience": "Аудитория", "job": "Задача",
              "alternatives": "Альтернативы", "direction": "Направление", "tradeoffs": "Отказы",
              "economics": "Экономика"})
        lines.append("- **Основания.** " + ", ".join(strategy["product"]["claims"]))
        lines += ["", "### Коммуникации", ""]
        items(lines, strategy["communication"], {"audience": "Аудитория", "situation": "Ситуация",
              "barrier": "Барьер", "message": "Сообщение", "proof": "Подтверждение",
              "channel_roles": "Роли каналов", "promise_limit": "Предел обещания"})
        lines.append("- **Основания.** " + ", ".join(strategy["communication"]["proof_claims"]))
        lines += ["", "### План проверки", ""]
        items(lines, strategy["test"], {"expected": "Ожидаемый эффект", "metric": "Метрика",
              "denominator": "Единица и знаменатель", "control": "Сравнение", "window": "Срок",
              "threshold": "Порог", "guardrails": "Защитные показатели"})
        lines.append("- **Основания.** " + ", ".join(strategy["test"]["claims"]))
        if strategy.get("assumptions"):
            lines += ["", "Допущения и основания порогов", "", *[f"- {x}" for x in strategy["assumptions"]]]
    monitoring = followup(state, as_of)
    decision = monitoring["active_decision"]
    if decision is not None:
        lines += ["", "## Решение и обязательства", "",
                  f"**{decision['option']} · {decision['approved_by']}.** {decision['rationale']}", ""]
        if decision["under_review"]:
            lines += ["**Состояние решения.** Действует, идёт пересмотр оснований.", ""]
        for item in monitoring["commitments"]:
            status = item["status"] if item["status"] != "open" else item["due_state"]
            lines.append(f"- **{item['id']} · {item['owner']} · {item['due']} · {status}.** "
                         f"{item['task']} Метрика — {item['metric']}. Условие пересмотра — {item['trigger']}.")
    if state["impact"]:
        impact = state["impact"]
        changes = [f"{change['replaced'] or '∅'} → {change['source'] or '∅'}" for change in impact["source_changes"]]
        lines += ["", "## Пересмотр после изменения материалов", "",
                  f"- **Изменения.** {', '.join(changes)}.",
                  f"- **Прежние выводы.** {', '.join(impact['affected_claims']) or 'Прямых ссылок нет'}.",
                  f"- **Варианты.** {', '.join(impact['affected_options']) or 'Прямых связей нет'}.",
                  f"- **Разделы стратегии.** {', '.join(impact['affected_strategy_sections']) or 'Прямых связей нет'}.",
                  f"- **Прежнее решение.** {impact['previous_decision'] or 'Не было утверждено'}.",
                  f"- **Повторное утверждение.** {'Ожидается' if impact['review_required'] else 'Внесено'}."]
        if impact.get("affected_metrics"):
            lines.append(f"- **Метрики.** {', '.join(impact['affected_metrics'])}.")
        if impact.get("affected_questions"):
            lines.append(f"- **Вопросы.** {', '.join(impact['affected_questions'])}.")
        for change in impact["source_changes"]:
            if change.get("reason"):
                lines.append(f"- **{change['source']}.** {inline_text(change['reason'])}")
        changes = metric_changes(state, metrics, cache)
        if changes:
            lines += ["", "### Изменение метрик", ""]
            for change in changes:
                label = inline_text(change["label"])
                before = inline_text(metric_value(change["before"]))
                after = inline_text(metric_value(change["after"]))
                lines.append(f"- **{change['id']} · {label}.** {before} → {after}.")
    pending = pending_source_reviews(state)
    if pending:
        lines += ["", f"**Материалы на оценке применимости.** {', '.join(pending)}."]
    if state["sources"]:
        lines += ["", "## Источники", ""]
        for key, source in state["sources"].items():
            origin = source["origin"]
            sheet = f" · лист «{escape(' '.join(source['sheet'].split()))}»" if "sheet" in source else ""
            lines.append(f"- **{key} · {source['kind']} · {source['status']}.** "
                         f"{source['acquisition']}. {origin}{sheet}{source_timing_label(source)}")
    return "\n".join(lines) + "\n"
