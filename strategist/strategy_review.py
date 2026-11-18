"""Hand one checked strategy to a read-only reviewer and bind its answer to the evidence."""

import os
import uuid
from importlib import resources
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import checkpoints, privacy, sources, state


def packet(data, review):
    saved = data["checkpoints"]
    strategy = saved["strategy"]
    brief, research = saved["brief"], saved["research"]
    cache = {}
    metrics = checkpoints.decision_metrics(data["sources"], brief, research, cache)
    linked = set(strategy["product"]["claims"] + strategy["communication"]["proof_claims"] + strategy["test"]["claims"])
    for option in saved["options"]["options"]:
        linked.update(option["claims"])
    linked.update(metric["claim"] for metric in metrics if "claim" in metric)
    claims, cited = {}, {}
    for claim in research["claims"]:
        if claim["id"] not in linked:
            continue
        rates = checkpoints.rate_checks(data["sources"], claim, cache)
        claims[claim["id"]] = {
            "type": claim["type"], "scope": claim["scope"], "limits": claim["limits"],
            "text": checkpoints.render_claim_text(claim, rates), "refs": claim["refs"],
        }
        for ref in claim["refs"]:
            cited.setdefault(ref["source"], set()).update(range(ref["start"], ref["end"] + 1))

    excerpts = {}
    for key, numbers in sorted(cited.items()):
        source = data["sources"][key]
        rows = sources.source_lines(source)
        if source["format"] in sources.TABLE_FORMATS:
            numbers.add(1)  # Include the column names even when a citation starts at a data row.
        excerpts[key] = {
            **{field: source[field] for field in ("kind", "origin", "sheet", "timing") if field in source},
            "lines": {str(i): rows[i - 1] for i in sorted(numbers)},
        }
    content = {
        "instructions": resources.files("strategist").joinpath("strategy_reviewer.md").read_text(encoding="utf-8"),
        "case": data["case"], "version": review["version"], "ticket": review["ticket"],
        "brief": {field: brief[field] for field in ("question", "goal", "audience", "constraints")},
        "analysis": state.analysis_context(data), "strategy": strategy, "options": saved["options"],
        "metrics": metrics, "claims": claims, "sources": excerpts, "gaps": research["gaps"],
    }
    privacy.require_clean(state.dump(content), "Пакет для проверки стратегии")
    return content


def current(data):
    review = state.strategy_review_status(data)
    state.need(review is not None, "Сначала проверьте и сохраните этап strategy.")
    return review


def begin(data):
    review = current(data)
    if review["status"] != "pending":
        return {"dispatch": False, "review": review}
    version = review["version"]
    record = {"status": "in_progress", "ticket": uuid.uuid4().hex}
    review = {"version": version, **record}
    content = packet(data, review)
    data.setdefault("strategy_reviews", {})[version] = record
    state.event(data, "strategy-review-start", version=version, ticket=record["ticket"])
    return {"dispatch": True, "review": review, "packet": content}


def interrupt(data, ticket, *, reason="interrupted"):
    review = current(data)
    state.need(review["status"] == "in_progress" and review["ticket"] == ticket,
               "Назначение устарело или уже закрыто. Проверьте review-strategy.")
    data["strategy_reviews"][review["version"]] = {"status": "pending"}
    state.event(data, "strategy-review-interrupted", version=review["version"], ticket=ticket, reason=reason)
    return {"version": review["version"], "status": "pending"}


def strategy_fields(strategy):
    fields = {}
    for key, value in strategy.items():
        if isinstance(value, dict):
            fields.update({f"{key}.{name}": text for name, text in value.items()})
        else:
            fields[key] = value
    return fields


def save_reply(folder, content, result):
    """Keep the transport receipt if a pause or a crash precedes registration."""
    privacy.require_clean(state.dump(result), "Ответ проверяющего")
    state.need(isinstance(result, dict) and all(result.get(k) == content[k] for k in ("case", "version", "ticket")),
               "Ответ не соответствует назначению проверки.")
    validate(content, result)
    directory = folder / "reviews"
    state.need(not directory.is_symlink(), "Папка reviews должна находиться внутри кейса.")
    directory.mkdir(exist_ok=True)
    path = directory / (content["ticket"] + ".json")
    body = state.dump(result) + "\n"
    state.need(len(body.encode()) <= 1_000_000, "Ответ проверяющего больше 1 МБ.")
    with NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as tmp:
        tmp.write(body)
    try:
        os.replace(tmp.name, path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    return path


def validate(content, result):
    verdict = result.get("verdict")
    state.need(verdict in ("clear", "changes_requested", "inconclusive"), "Неизвестный verdict.")
    fields = {"case", "version", "ticket", "verdict", "findings"}
    if verdict == "inconclusive":
        fields.add("reason")
        checkpoints.strings(result, "reason")
    state.need(set(result) == fields, "Некорректные поля результата проверки стратегии.")
    findings = result["findings"]
    state.need(isinstance(findings, list) and bool(findings) == (verdict == "changes_requested"),
               "Для changes_requested нужны замечания, для остальных вердиктов нужен пустой findings.")
    texts = strategy_fields(content["strategy"])
    basis = (set(content["claims"]) | {item["id"] for item in content["metrics"]}
             | {item["id"] for item in content["options"]["options"]}
             | {f"strategy.{key}" for key in texts}
             | {f"{key}:L{line}" for key, src in content["sources"].items() for line in src["lines"]})
    for finding in findings:
        state.need(isinstance(finding, dict) and set(finding) == {"field", "statement", "basis", "reason", "fix"},
                   "В замечании нужны field, statement, basis, reason и fix.")
        checkpoints.strings(finding, "field statement reason fix")
        value = texts.get(finding["field"])
        values = value if isinstance(value, list) else [value]
        state.need(any(isinstance(v, str) and finding["statement"] in v for v in values),
                   "Утверждение должно быть точной цитатой из указанного поля стратегии.")
        refs = finding["basis"]
        state.need(isinstance(refs, list) and refs and all(isinstance(ref, str) and ref in basis for ref in refs),
                   "Для замечания укажите доступные основания из выданного пакета.")


def record(data, result):
    review = current(data)
    state.need(isinstance(result, dict) and result.get("case") == data["case"]
               and result.get("version") == review["version"] and result.get("ticket") == review.get("ticket"),
               "Результат относится к другому кейсу, версии или назначению.")
    if "result" in review and review["result"] == result:
        return review
    state.need(review["status"] == "in_progress", "Эта версия уже проверена или ещё не назначена.")
    privacy.require_clean(state.dump(result), "Ответ проверяющего")
    validate(packet(data, review), result)
    entry = {"status": result["verdict"], "ticket": review["ticket"], "result": result}
    data["strategy_reviews"][review["version"]] = entry
    state.event(data, "strategy-review-finish", version=review["version"], ticket=review["ticket"],
                verdict=result["verdict"])
    return {"version": review["version"], **entry}
