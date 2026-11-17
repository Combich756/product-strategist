"""Validation of artifacts supplied by the IDE agent or by the decision owner."""

import re
from datetime import date
from decimal import Decimal
from fractions import Fraction
from math import isfinite

from .analysis import format_rate, merged_refs, rates, table_records
from .sources import TABLE_FORMATS, source_lines
from .state import need
from .timing import validate_analysis

RATE_REFERENCE = re.compile(r"\{\{\s*rate\s*:\s*([1-9][0-9]?)\s*\}\}")
RATE_MARKER = re.compile(r"\{\{\s*rate\b")
RATE_FRACTION = r"(?<![\w/.,])(?P<numerator>[0-9]+)\s*(?:/|\bиз\b)\s*(?P<denominator>[0-9]+)"
LITERAL_RATE = re.compile(r"\d\s*[%％]|" + RATE_FRACTION, re.IGNORECASE)
# Old drafts write a fraction followed by =, ≈, or a percentage in parentheses.
LEGACY_RATE = re.compile(
    RATE_FRACTION
    + r"(?:\s*[=≈]|(?:[ \t]+[^\W\d_]+){0,3}\s*(?P<parenthesis>\())\s*"
      r"(?P<percent>[0-9]+(?:[.,][0-9]+)?)\s*[%％](?(parenthesis)\s*\))",
    re.IGNORECASE,
)


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
        if "subtract" in check:
            strings(check, "subtract")
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
        subtract = check.get("subtract")
        key = (source, tuple(group), numerator, denominator, subtract)
        if key not in cache:
            computed = rates(sources[source], list(group), numerator, denominator, subtract=subtract)
            cache[key] = {tuple(row["group"].values()): row for row in computed["groups"]}
        result = cache[key].get(tuple(group.values()))
        need(result is not None, f"{label}: группа {group} отсутствует в таблице.")
        need(all(result[field] == expected[field] for field in fields),
             f"{label}: в группе {group} указано {expected['numerator']}/{expected['denominator']}, "
             f"пересчёт дал {result['numerator']}/{result['denominator']}.")
        for row in result["refs"]:
            need(any(ref["start"] <= row["start"] and row["end"] <= ref["end"] for ref in citations),
                 f"{label}: строки расчёта {row['start']}–{row['end']} выходят за границы ссылок вывода.")
        adjustment = {"subtract_column": subtract} if subtract is not None else {}
        verified.append({"source": source, "numerator_column": numerator, "denominator_column": denominator,
                         **adjustment, **result})
    return verified


def render_claim_text(claim, verified_rates):
    def substitute(match):
        index = int(match[1])
        need(index <= len(verified_rates), f"{claim['id']} · {match[0]}: расчёт №{index} отсутствует в rate_checks.")
        return format_rate(verified_rates[index - 1])

    rendered = RATE_REFERENCE.sub(substitute, claim["text"])
    need(not RATE_MARKER.search(rendered), f"{claim['id']}: используйте синтаксис {{{{rate:1}}}} для ссылки на расчёт.")
    return rendered


def validate_claim_rate_text(claim, verified_rates):
    text = claim["text"]
    if RATE_MARKER.search(text):
        render_claim_text(claim, verified_rates)
        need(not LITERAL_RATE.search(text),
             f"{claim['id']}: подставляйте доли через {{{{rate:1}}}}, без дробей и процентов вручную в том же выводе.")
        return

    # Check unambiguous pairs from declared calculations without guessing what standalone percentages describe.
    by_counts = {(rate["numerator"], rate["denominator"]): rate for rate in verified_rates}
    if not by_counts:
        return
    for match in LEGACY_RATE.finditer(text):
        counts = int(match["numerator"]), int(match["denominator"])
        if counts not in by_counts:
            continue
        numerator, denominator = counts
        percent = match["percent"].replace(",", ".")
        # The exact fraction may be rounded to the precision shown in the draft.
        rounding = Fraction(1, 2 * 10 ** len(percent.partition(".")[2]))
        need(denominator and abs(Fraction(percent) - Fraction(100 * numerator, denominator)) <= rounding,
             f"{claim['id']}: в тексте {match[0]}, пересчёт дал {format_rate(by_counts[counts])}. "
             "Подставьте долю через {{rate:1}} с номером нужного расчёта.")


def snapshot_rows(sources, source, cache):
    key = ("cells", source)
    if key not in cache:
        header, rows = table_records(sources[source])
        cache[key] = (header, {first: (last, row) for first, last, row in rows})
    return cache[key]


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
             "cell_checks: line — номер строки данных, expected — точное значение из снимка таблицы.")
        header, rows = snapshot_rows(sources, source, cache)
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


def validate_metric_definitions(metrics):
    for key, metric in indexed(metrics, "brief.metrics", "M", 0).items():
        strings(metric, "label unit")
        need(set(metric) <= {"id", "label", "unit", "formula", "threshold", "comparability"},
             f"Для {key} в брифе задайте описание, формулу, порог и условия сопоставимости. "
             "Результат укажите в исследовании.")
        formula = object_(metric.get("formula"), f"{key}.formula")
        if "column" in formula:
            need(set(formula) == {"column"}, f"{key}.formula принимает одно поле column.")
            strings(formula, "column")
        else:
            strings(formula, "numerator denominator")
            need(set(formula) <= {"numerator", "denominator", "subtract"},
                 f"{key}.formula принимает numerator, denominator и необязательное subtract.")
            if "subtract" in formula:
                strings(formula, "subtract")
            need(len(set(formula.values())) == len(formula), f"Для {key} нужны разные столбцы формулы.")
            need(metric["unit"] == "%", f"Для доли {key} укажите единицу %.")
        if "threshold" in metric:
            threshold = object_(metric["threshold"], f"{key}.threshold")
            value = threshold.get("value")
            need(set(threshold) == {"op", "value"} and threshold["op"] in (">=", "<=") and
                 (type(value) is int or type(value) is float and isfinite(value)),
                 f"Для {key} укажите порог с op >= или <= и конечным числом value.")
        if "comparability" in metric:
            required = object_(metric["comparability"], f"{key}.comparability")
            need(required and all(column.strip() and isinstance(value, str) and value.strip()
                                  for column, value in required.items()),
                 f"{key}.comparability: укажите столбцы и их ожидаемые непустые строковые значения.")


def metric_comparability(sources, source, ranges, required, cache):
    header, rows = snapshot_rows(sources, source, cache)
    issues = []
    for column, expected in required.items():
        condition = {"column": column, "expected": expected}
        if column not in header:
            issues.append({**condition, "kind": "missing_column"})
            continue
        missing = None
        for ref in ranges:
            actual = rows[ref["start"]][1][column]
            if actual == expected:
                continue
            location = {"source": source, **ref}
            if actual.strip():
                issues.append({**condition, "kind": "mismatch", "actual": actual, "ref": location})
                break
            if missing is None:
                missing = location
        else:
            if missing is not None:
                issues.append({**condition, "kind": "missing_value", "ref": missing})
    status = "compatible"
    if issues:
        status = "incompatible" if any(issue["kind"] == "mismatch" for issue in issues) else "unverified"
    return {"status": status, "issues": issues}


def measure_metric(sources, metric, claim, index, cache):
    formula = metric["formula"]
    scalar = "column" in formula
    checks = cell_checks(sources, claim, cache) if scalar else rate_checks(sources, claim, cache)
    key = metric["id"]
    field = "cell_checks" if scalar else "rate_checks"
    need(type(index) is int and 1 <= index <= len(checks),
         f"Для {key} укажите номер проверки из {claim['id']}.{field}, начиная с 1.")
    check = checks[index - 1]
    source = check["source"]
    if scalar:
        actual = check["actual"].strip().replace(",", ".")
        need(formula == {"column": check["column"]}, f"Ячейка для {key} расходится с формулой в брифе.")
        need(re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", actual),
             f"Для {key} нужна ячейка с числом в единицах метрики.")
        value = Fraction(actual)
        shown = f"{actual}{' ' if metric['unit'] != '%' else ''}{metric['unit']}"
        ranges = [{"start": check["line"], "end": check["end"]}]
    else:
        columns = {name: check[f"{name}_column"] for name in ("numerator", "denominator", "subtract")
                   if f"{name}_column" in check}
        need(formula == columns, f"Пересчёт для {key} расходится с формулой в брифе.")
        denominator = check["denominator"]
        value = Fraction(100 * check["numerator"], denominator) if denominator else None
        shown = format_rate(check)
        ranges = check["refs"]

    refs = merged_refs(source, ((row["start"], row["end"]) for row in ranges))
    result = {**metric, "value": shown, "claim": claim["id"], "refs": refs}
    if "comparability" in metric:
        comparison = metric_comparability(sources, source, ranges, metric["comparability"], cache)
        result["comparability_check"] = comparison
        if comparison["status"] != "compatible":
            if scalar:
                result["value"] = actual
            return result
    if value is not None and "threshold" in metric:
        threshold = metric["threshold"]
        target = Fraction(str(threshold["value"]))
        excess = value - target if threshold["op"] == "<=" else target - value
        result["passes"] = excess <= 0
        if excess > 0:
            delta = Decimal(excess.numerator) / Decimal(excess.denominator)
            unit = "п. п." if metric["unit"] == "%" else metric["unit"]
            result["deviation"] = f"{delta:.6g} {unit}"
    return result


def decision_metrics(sources, brief, research, cache):
    definitions = brief.get("metrics", [])
    if research is None or not definitions:
        return definitions

    bindings = object_(research.get("metrics", {}), "research.metrics")
    expected = {metric["id"] for metric in definitions}
    missing = expected - bindings.keys()
    unexpected = bindings.keys() - expected
    need(not missing, f"Заполните метрики из брифа в research.metrics: {', '.join(sorted(missing))}. "
         "Укажите проверенный расчёт или gap с причиной.")
    need(not unexpected, f"Метрики вне брифа в research.metrics: {', '.join(sorted(unexpected))}.")
    claims = {claim["id"]: claim for claim in research["claims"]}
    results = []
    for metric in definitions:
        key = metric["id"]
        binding = object_(bindings[key], f"research.metrics.{key}")
        if set(binding) == {"gap"}:
            strings(binding, "gap")
            results.append({**metric, **binding})
        else:
            need(set(binding) == {"claim", "check"}, f"Для {key} укажите claim и check или причину gap.")
            strings(binding, "claim")
            claim = claims.get(binding["claim"])
            need(claim is not None and claim["type"] == "fact",
                 f"Для {key} выберите существующий факт с проверкой источника.")
            results.append(measure_metric(sources, metric, claim, binding["check"], cache))
    return results


def research_questions(brief):
    return {f"Q{number}": question for number, question in enumerate(brief["questions"], 1)}


def validate_question_results(brief, research, claims):
    questions = research_questions(brief)
    results = object_(research.get("question_results", {}), "research.question_results")
    missing = questions.keys() - results.keys()
    unexpected = results.keys() - questions.keys()
    need(not missing, f"research.question_results: заполните вопросы {', '.join(sorted(missing))}. "
         "Укажите факты или пробел, влияние на выбор и следующую проверку.")
    need(not unexpected, f"research.question_results: неизвестные вопросы {', '.join(sorted(unexpected))}.")
    for key, result in results.items():
        field = f"research.question_results.{key}"
        result = object_(result, field)
        allowed = {"claims", "decision_impact"}
        fields = ["decision_impact"]
        if "gap" in result:
            allowed.update(("gap", "next_check"))
            fields.extend(("gap", "next_check"))
        need(result.keys() <= allowed, f"{field}: укажите claims или gap с next_check и decision_impact.")
        for name in fields:
            value = result.get(name)
            need(isinstance(value, str) and value.strip(), f"{field}.{name}: ожидается непустой текст.")
        if "gap" not in result or "claims" in result:
            linked(result.get("claims"), f"{field}.claims", claims, fact_only=True)


def validate_brief(state, data):
    strings(data, "question goal audience constraints available_data")
    texts(data.get("questions"), "questions")
    if "require_question_results" in data:
        need(type(data["require_question_results"]) is bool, "require_question_results: ожидается true или false.")
    if "analysis" in data:
        validate_analysis(data["analysis"])
    if "metrics" in data:
        validate_metric_definitions(data["metrics"])


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
        verified_rates = rate_checks(state["sources"], claim, cache)
        validate_claim_rate_text(claim, verified_rates)
        cell_checks(state["sources"], claim, cache)
        if re.search(r"\d", claim["text"]):
            tables = {ref["source"] for ref in claim["refs"]
                      if state["sources"][ref["source"]]["format"] in TABLE_FORMATS}
            checked = {check["source"] for field in ("rate_checks", "cell_checks") for check in claim.get(field, [])}
            need(tables <= checked,
                 f"{claim['id']}: числовой вывод со ссылкой на таблицу требует rate_checks или cell_checks. "
                 "Получите готовые проверки и refs через rates --evidence или cells.")
    need(fact_found, "Для перехода нужен хотя бы один факт с источником.")
    brief = state["checkpoints"]["brief"]
    metrics = decision_metrics(state["sources"], brief, data, cache)
    if brief.get("require_question_results"):
        validate_question_results(brief, data, claims)
    return metrics


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
