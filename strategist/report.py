"""Render the saved artifacts for human review."""

from .state import followup, next_stage


def items(lines, record, labels):
    for key, title in labels.items():
        lines.append(f"- **{title}.** {record[key]}")


def report(state, as_of):
    saved = state["checkpoints"]
    lines = [
        f"# Стратегия · {state['case']}",
        "", f"**Исходный запрос.** {state['request']}",
        "", f"**Состояние.** Ревизия {state['revision']}, следующий этап `{next_stage(state)}`"
        + (", кейс приостановлен." if state["paused"] else "."),
    ]
    if "brief" in saved:
        brief = saved["brief"]
        lines += ["", "## Постановка", ""]
        items(lines, brief, {"question": "Вопрос", "goal": "Цель", "audience": "Для кого",
                             "constraints": "Ограничения", "available_data": "Материалы"})
        lines += ["", "Вопросы исследования", ""]
        lines += [f"- {question}" for question in brief["questions"]]
    if "research" in saved:
        research = saved["research"]
        lines += ["", "## Исследование", "", research["comparison"], ""]
        for claim in research["claims"]:
            refs = ", ".join(f"{ref['source']}:L{ref['start']}–L{ref['end']}" for ref in claim["refs"])
            lines.append(f"- **{claim['id']} · {claim['type']} · {claim['scope']}.** {claim['text']}"
                         + (f" [{refs}]." if refs else "")
                         + f" **Граница вывода.** {claim['limits']}")
        lines += ["", "Открытые вопросы", ""]
        lines += [f"- {gap}" for gap in research["gaps"]]
    if "options" in saved:
        lines += ["", "## Варианты", "", saved["options"]["comparison"]]
        for option in saved["options"]["options"]:
            lines += ["", f"### {option['id']} · {option['title']}", ""]
            items(lines, option, {"action": "Действие", "benefit": "Польза", "objection": "Возражение",
                                  "economics": "Экономика"})
            lines.append("- **Основания.** " + ", ".join(option["claims"]))
    if "strategy" in saved:
        strategy = saved["strategy"]
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
    if "decision" in saved:
        decision = saved["decision"]
        lines += ["", "## Решение и обязательства", "",
                  f"**{decision['option']} · {decision['approved_by']}.** {decision['rationale']}", ""]
        for item in followup(state, as_of)["commitments"]:
            lines.append(f"- **{item['id']} · {item['owner']} · {item['due']} · {item['due_state']}.** "
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
    if state["sources"]:
        lines += ["", "## Источники", ""]
        for key, source in state["sources"].items():
            origin = source["origin"]
            lines.append(f"- **{key} · {source['kind']} · {source['status']}.** "
                         f"{source['acquisition']}. {origin}")
    return "\n".join(lines) + "\n"
