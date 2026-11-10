"""Validation of artifacts supplied by the IDE agent or by the decision owner."""

import re
from datetime import date

from .analysis import rates, table_records
from .sources import source_lines
from .state import need


def object_(value, field):
    need(isinstance(value, dict), f"{field}: ожидается объект.")
    return value


def list_(value, field, minimum=1):
    need(isinstance(value, list) and len(value) >= minimum, f"{field}: ожидается список, минимум {minimum}.")
    return value


def strings(item, fields):
    for field in fields.split():
        value = item.get(field)
        need(isinstance(value, str) and value.strip(), f"{field}: ожидается непустой текст.")


def texts(value, field):
    for text in list_(value, field):
        need(isinstance(text, str) and text.strip(), f"{field}: все пункты должны быть непустым текстом.")


def indexed(value, field, prefix, minimum=1):
    records = {}
    for item in list_(value, field, minimum):
        item = object_(item, field)
        strings(item, "id")
        key = item["id"]
        need(re.fullmatch(fr"{prefix}[1-9][0-9]*", key) and key not in records, f"{field}: неверный или повторный ID {key}.")
        records[key] = item
    return records


def evidence(state, refs):
    kinds = set()
    for ref in list_(refs, "refs", 0):
        ref = object_(ref, "refs")
        strings(ref, "source")
        key = ref["source"]
        source = state["sources"].get(key)
        need(source is not None and source["status"] == "active", f"{key}: нужен активный источник.")
        first, last = ref.get("start"), ref.get("end")
        need(
            type(first) is int and type(last) is int and 1 <= first <= last <= len(source_lines(source)),
            f"{key}: неверный диапазон строк.",
        )
        kinds.add(source["kind"])
    return kinds


def rate_checks(sources, claim, cache):
    """Recompute declared counts from the same snapshot and rows cited by a claim."""
    checks = list_(claim.get("rate_checks", []), "rate_checks", 0)
    need(len(checks) <= 20, "rate_checks: максимум 20 групп на вывод.")
    verified = []
    for check in checks:
        check = object_(check, "rate_checks")
        strings(check, "source numerator denominator")
        source = check["source"]
        label = f"{claim['id']} · {source}"
        citations = [ref for ref in claim["refs"] if ref["source"] == source]
        need(citations, f"{label}: для пересчёта нужна ссылка на строки этого источника.")
        group = object_(check.get("group"), "group")
        need(all(key.strip() and isinstance(value, str) and value.strip() for key, value in group.items()),
             f"{label}: названия и значения полей группировки должны быть непустым текстом.")
        expected = object_(check.get("expected"), "expected")
        fields = ("numerator", "denominator")
        need(all(type(expected.get(field)) is int for field in fields),
             f"{label}: ожидаемые числитель и знаменатель должны быть целыми числами.")

        numerator, denominator = check["numerator"], check["denominator"]
        key = (source, tuple(group), numerator, denominator)
        if key not in cache:
            computed = rates(sources[source], list(group), numerator, denominator)
            cache[key] = {tuple(row["group"].values()): row for row in computed["groups"]}
        result = cache[key].get(tuple(group.values()))
        need(result is not None, f"{label}: группа {group} отсутствует в таблице.")
        need(all(result[field] == expected[field] for field in fields),
             f"{label}: в группе {group} указано {expected['numerator']}/{expected['denominator']}, "
             f"пересчёт дал {result['numerator']}/{result['denominator']}.")
        for row in result["refs"]:
            need(any(ref["start"] <= row["start"] and row["end"] <= ref["end"] for ref in citations),
                 f"{label}: строки расчёта {row['start']}–{row['end']} выходят за границы ссылок вывода.")
        verified.append({"source": source, "numerator_column": numerator, "denominator_column": denominator,
                         **result})
    return verified


def cell_checks(sources, claim, cache):
    """Verify literal cell evidence; never infer a causal link from matching values."""
    checks = list_(claim.get("cell_checks", []), "cell_checks", 0)
    need(len(checks) <= 200, "cell_checks: максимум 200 ячеек на вывод.")
    verified = []
    for check in checks:
        check = object_(check, "cell_checks")
        strings(check, "source column")
        source = check["source"]
        need(source in sources, f"{source}: источник отсутствует.")
        line = check.get("line")
        need(type(line) is int and line > 1 and isinstance(check.get("expected"), str),
             "cell_checks: line — номер строки данных, expected — точная строка из CSV.")
        key = ("cells", source)
        if key not in cache:
            header, rows = table_records(sources[source])
            cache[key] = (header, {first: (last, row) for first, last, row in rows})
        header, rows = cache[key]
        need(check["column"] in header and line in rows, f"{source}: ячейка отсутствует.")
        last, row = rows[line]
        actual = row[check["column"]]
        need(actual == check["expected"], f"{claim['id']} · {source}: значение ячейки не совпадает со снимком.")
        need(any(ref["source"] == source and ref["start"] <= line and ref["end"] >= last
                 for ref in claim["refs"]), f"{claim['id']} · {source}: ячейка выходит за границы ссылок вывода.")
        verified.append({**check, "actual": actual, "end": last})
    return verified


def linked(value, field, claims, fact_only=False):
    for key in list_(value, field):
        need(isinstance(key, str) and key in claims, f"{field}: вывод {key!r} отсутствует.")
        if fact_only:
            need(claims[key]["type"] == "fact", f"{field}: {key} должен иметь тип fact.")


def validate_brief(state, data):
    strings(data, "question goal audience constraints available_data")
    texts(data.get("questions"), "questions")


def validate_research(state, data):
    strings(data, "comparison")
    texts(data.get("gaps"), "gaps")
    claims = indexed(data.get("claims"), "claims", "C")
    fact_found = False
    cache = {}
    for claim in claims.values():
        strings(claim, "type text scope limits")
        need(claim["type"] in {"fact", "hypothesis", "forecast", "unknown"}, "Неизвестный тип вывода.")
        need(claim["scope"] in {"case", "public", "demo"}, "scope: case, public или demo.")
        kinds = evidence(state, claim.get("refs"))
        if "synthetic" in kinds:
            need(claim["scope"] == "demo", "Вывод с синтетическим материалом требует scope=demo.")
        if claim["type"] == "fact":
            need(bool(kinds), "Факт должен ссылаться на источник.")
            if kinds == {"public"}:
                need(claim["scope"] == "public", "Факт из открытых источников требует scope=public.")
            fact_found = True
        rate_checks(state["sources"], claim, cache)
        cell_checks(state["sources"], claim, cache)
        if re.search(r"\d", claim["text"]):
            tables = {ref["source"] for ref in claim["refs"]
                      if state["sources"][ref["source"]]["format"] in {"csv", "tsv"}}
            checked = {check["source"] for field in ("rate_checks", "cell_checks") for check in claim.get(field, [])}
            need(tables <= checked,
                 f"{claim['id']}: числовой вывод со ссылкой на таблицу требует rate_checks или cell_checks. "
                 "Получите готовые проверки и refs через rates --evidence или cells.")
    need(fact_found, "Для перехода нужен хотя бы один факт с источником.")


def validate_options(state, data):
    strings(data, "comparison")
    options = indexed(data.get("options"), "options", "O", 3)
    claims = {item["id"]: item for item in state["checkpoints"]["research"]["claims"]}
    kinds = []
    for item in options.values():
        strings(item, "kind title action benefit objection economics")
        need(item["kind"] in {"status_quo", "close", "change"}, "Неизвестный тип варианта.")
        kinds.append(item["kind"])
        linked(item.get("claims"), "claims", claims)
    need(kinds.count("status_quo") == 1 and kinds.count("close") == 1 and "change" in kinds,
         "Нужны ровно по одному status_quo и close, а также минимум один change.")


def validate_strategy(state, data):
    strings(data, "option rationale revisit")
    options = state["checkpoints"]["options"]["options"]
    need(any(item["id"] == data["option"] for item in options), "Рекомендуемый вариант отсутствует.")
    claims = {item["id"]: item for item in state["checkpoints"]["research"]["claims"]}
    product = object_(data.get("product"), "product")
    strings(product, "audience job alternatives direction tradeoffs economics")
    linked(product.get("claims"), "product.claims", claims)
    communication = object_(data.get("communication"), "communication")
    strings(communication, "audience situation barrier message proof channel_roles promise_limit")
    linked(communication.get("proof_claims"), "communication.proof_claims", claims, fact_only=True)
    test = object_(data.get("test"), "test")
    strings(test, "expected metric denominator control window threshold guardrails")
    linked(test.get("claims"), "test.claims", claims)


def validate_decision(state, data):
    strings(data, "option approved_by rationale")
    need(data["option"] == state["checkpoints"]["strategy"]["option"],
         "Решение расходится со стратегией. Сначала пересохраните стратегию с выбранным вариантом.")
    for item in indexed(data.get("commitments"), "commitments", "K").values():
        strings(item, "task owner due metric trigger")
        need(re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["due"]), "due: ожидается ГГГГ-ММ-ДД.")
        date.fromisoformat(item["due"])


VALIDATORS = {
    "brief": validate_brief, "research": validate_research, "options": validate_options,
    "strategy": validate_strategy, "decision": validate_decision,
}
