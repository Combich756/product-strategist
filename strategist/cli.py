"""Command-line tools invoked by the product strategist inside an IDE."""

import argparse
import csv
import hashlib
import http.client
import re
import sqlite3
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from time import perf_counter

from . import analysis, checkpoints, recovery, sources, state
from .report import claim_evidence, report


def source_or_error(data, key):
    source = data["sources"].get(key)
    checkpoints.need(source is not None, f"Источник {key} отсутствует.")
    return source


def allow_web(folder, args):
    url = sources.normalize_url(args.url)
    with state.case(folder, write=True) as data:
        if url not in data["allowed_urls"]:
            data["allowed_urls"].append(url)
            state.event(data, "allow-web", url=url)
        return {"allowed_url": url}


def ingest(folder, args):
    started = perf_counter()
    origin = sources.normalize_url(args.url or args.origin) if args.url or args.origin else None
    with state.case(folder) as data:
        checkpoints.need(not data["paused"], "Кейс приостановлен.")
        revision = data["revision"]
        if origin:
            checkpoints.need(origin in data["allowed_urls"], "URL отсутствует в списке разрешений.")
        if args.replaces:
            source_or_error(data, args.replaces)
    if args.url:
        checkpoints.need(not args.origin and args.kind in (None, "public"), "Для --url вид источника уже public.")
        source = sources.fetch_web(origin)
    else:
        checkpoints.need(args.kind is not None, "Для локального файла укажите --kind.")
        source = sources.read_local(folder, args.file, args.kind, origin)

    with state.case(folder, write=True, expect=revision) as data:
        replaced = args.replaces
        for key, old in data["sources"].items():
            if old["status"] == "active" and old["origin"] == source["origin"]:
                if (
                    old["text_sha256"] == source["text_sha256"] and old["kind"] == source["kind"]
                    and old["acquisition"] == source["acquisition"]
                ):
                    checkpoints.need(not replaced or replaced == key, "Этот материал уже активен под другим ID.")
                    return {"source": key, "reused": True, **sources.metadata(old)}
                checkpoints.need(not replaced or replaced == key, "Источник с таким origin уже активен.")
                replaced = key
        if replaced:
            old = source_or_error(data, replaced)
            checkpoints.need(old["status"] == "active", f"{replaced} уже неактивен.")
            old["status"] = "superseded"
        key = f"S{len(data['sources']) + 1:03d}"
        source["replaces"] = replaced
        data["sources"][key] = source
        state.invalidate_for_source(data, key, replaced)
        state.event(data, "ingest", source=key, replaced=replaced,
                    duration_ms=round(1000 * (perf_counter() - started)))
        return {"source": key, "reused": False, **sources.metadata(source)}


def revoke(folder, args):
    with state.case(folder, write=True) as data:
        source = source_or_error(data, args.source)
        if source["status"] == "revoked":
            return state.summary(data)
        checkpoints.need(source["status"] == "active", "Отозвать можно только активный источник.")
        source["status"] = "revoked"
        source["revocation_reason"] = args.reason
        state.invalidate_for_source(data, None, args.source)
        state.event(data, "revoke", source=args.source, reason=args.reason)
        return state.summary(data)


def save(folder, args):
    stage = "decision" if args.command == "decide" else args.stage
    artifact = checkpoints.object_(state.load_json(args.input), stage)
    with state.case(folder, write=True, expect=args.expect) as data:
        if data["checkpoints"].get(stage) == artifact:
            return state.summary(data)
        checkpoints.need(state.next_stage(data) == stage,
                         f"Следующий этап — {state.next_stage(data)}. Для исправления используйте reopen.")
        review = recovery.check_draft(data, stage, artifact)
        data["checkpoints"][stage] = artifact
        if stage == "decision" and data["impact"]:
            data["impact"]["review_required"] = False
        state.event(data, "save", stage=stage,
                    artifact_sha256=hashlib.sha256(state.dump(artifact).encode()).hexdigest())
        return {**state.summary(data), "warnings": review["warnings"]}


def reopen(folder, args):
    with state.case(folder, write=True, expect=args.expect) as data:
        checkpoints.need(args.stage in data["checkpoints"], "Этап ещё не сохранён.")
        state.archive(data, args.stage, args.reason)
        if data["impact"]:
            data["impact"]["review_required"] = True
        state.event(data, "reopen", stage=args.stage, reason=args.reason)
        return state.summary(data)


def pause_or_resume(folder, args):
    with state.case(folder, write=True, while_paused=True) as data:
        paused = args.command == "pause"
        if data["paused"] != paused:
            data["paused"] = paused
            state.event(data, args.command)
        return state.summary(data)


def track(folder, args):
    with state.case(folder, write=True, expect=args.expect) as data:
        decision = data["checkpoints"].get("decision", {})
        checkpoints.need(any(item["id"] == args.commitment for item in decision.get("commitments", [])),
                         "Обязательство отсутствует в действующем решении.")
        check = {"status": args.status, "note": args.note}
        previous = data["tracking"].get(args.commitment)
        if not previous or any(previous[key] != value for key, value in check.items()):
            data["tracking"][args.commitment] = {**check, "at": state.utc_now()}
            state.event(data, "track", commitment=args.commitment, **check)
        return state.followup(data, date.today().isoformat())


def log(folder, args):
    amount = Decimal(args.amount) if args.amount is not None else None
    if amount is not None:
        checkpoints.need(amount.is_finite() and amount >= 0, "Стоимость должна быть конечной и неотрицательной.")
        checkpoints.need(args.currency and re.fullmatch(r"[A-Z]{3}", args.currency), "Укажите валюту из трёх букв.")
    else:
        checkpoints.need(args.currency is None, "Валюта указывается вместе с суммой.")
    for value in (args.seconds, args.input_tokens, args.output_tokens):
        checkpoints.need(value is None or value >= 0, "Метрики не могут быть отрицательными.")
    checkpoints.need(args.attempts >= 1, "attempts: нужно положительное число.")
    with state.case(folder, write=True, while_paused=True) as data:
        state.event(data, "log", kind=args.kind, label=args.label, seconds=args.seconds,
                    input_tokens=args.input_tokens, output_tokens=args.output_tokens,
                    amount=str(amount) if amount is not None else None, currency=args.currency,
                    outcome=args.outcome, attempts=args.attempts)
        return state.summary(data)


def cost(data):
    amounts = {}
    totals = {"model": 0, "tool": 0, "human": 0}
    unknown = 0
    for item in data["journal"]:
        if item["action"] != "log":
            continue
        totals[item["kind"]] += item["seconds"] or 0
        if item["amount"] is None:
            unknown += 1
        else:
            currency = item["currency"]
            amounts[currency] = amounts.get(currency, Decimal(0)) + Decimal(item["amount"])
    return {
        "measurement_status": "logged_only" if any(item["action"] == "log" for item in data["journal"]) else "unmeasured",
        "known_logged_cost": {currency: str(amount) for currency, amount in amounts.items()},
        "logged_seconds": totals, "log_entries_without_cost": unknown,
        "successful_ingest_ms": sum(item.get("duration_ms", 0) for item in data["journal"]),
        "entries": [item for item in data["journal"] if item["action"] == "log"],
        "note": "Учтены только записи log. Ноль записей не означает нулевую стоимость. "
                "Интервалы между сохранениями не измеряют время модели; API-вызовы IDE автоматически не видны.",
    }


def inspect(folder, args):
    with state.case(folder) as data:
        match args.command:
            case "status":
                return state.summary(data)
            case "context":
                return {"request": data["request"], **state.summary(data),
                        "saved": data["checkpoints"], "allowed_urls": data["allowed_urls"]}
            case "sources":
                return {key: sources.metadata(source) for key, source in data["sources"].items()}
            case "view":
                source = source_or_error(data, args.source)
                lines = sources.source_lines(source)
                last = min(args.end if args.end is not None else args.start + 39, len(lines))
                checkpoints.need(1 <= args.start <= last and last - args.start < 100, "Выберите до 100 строк.")
                checkpoints.need(sum(map(len, lines[args.start - 1:last])) <= 24_000,
                                 "Диапазон слишком длинный. Сузьте его или разбейте длинную строку во входном файле.")
                return {"source": args.source, **sources.metadata(source),
                        "lines": [{"line": index + 1, "text": lines[index]} for index in range(args.start - 1, last)]}
            case "find":
                return analysis.find(data, args.query, args.limit)
            case "claim":
                return claim_evidence(data, args.claim, args.revision)
            case "rates":
                source = source_or_error(data, args.source)
                checkpoints.need(source["status"] == "active", "Используйте активный источник.")
                result = analysis.select_groups(
                    analysis.rates(source, args.by, args.numerator, args.denominator), args.group)
                return ({"source": args.source, **analysis.rate_evidence(args.source, result)} if args.evidence
                        else {"source": args.source, **result})
            case "cells":
                source = source_or_error(data, args.source)
                checkpoints.need(source["status"] == "active", "Используйте активный источник.")
                return analysis.cells(args.source, source, args.rows, args.columns)
            case "check":
                return recovery.check_draft(data, args.stage, state.load_json(args.input))
            case "recover":
                return recovery.recover(folder, data)
            case "history":
                return data["history"]
            case "journal":
                return data["journal"]
            case "followup":
                return state.followup(data, args.as_of)
            case "cost":
                return cost(data)
            case "report":
                return report(data, args.as_of)


def parser():
    app = argparse.ArgumentParser(description="Инструменты агента «Стратег Фабрики продуктов».")
    app.add_argument("--root", default="cases", help="Папка рабочих кейсов, по умолчанию cases.")
    commands = app.add_subparsers(dest="command", required=True)

    def command(name, help_):
        sub = commands.add_parser(name, help=help_)
        sub.add_argument("case", help="ID кейса")
        return sub

    command("init", "Создать кейс").add_argument("request")
    command("allow-web", "Разрешить конкретный HTTPS URL").add_argument("url")
    sub = command("ingest", "Импортировать снимок")
    target = sub.add_mutually_exclusive_group(required=True)
    target.add_argument("--file", help="Путь внутри materials/ кейса")
    target.add_argument("--url", help="Явно разрешённый HTTPS URL")
    sub.add_argument("--kind", choices=("own", "synthetic", "public"))
    sub.add_argument("--origin", help="Разрешённый URL для локальной копии или выписки")
    sub.add_argument("--replaces", metavar="SOURCE")
    sub = command("revoke", "Отозвать активный источник")
    sub.add_argument("source")
    sub.add_argument("--reason", required=True)
    for name, help_ in (("save", "Сохранить артефакт этапа"), ("decide", "Записать решение человека")):
        sub = command(name, help_)
        if name == "save":
            sub.add_argument("stage", choices=state.STAGES[:-1])
        sub.add_argument("input", help="Путь к JSON")
        sub.add_argument("--expect", type=int, required=True, help="Текущая ревизия из status")
    sub = command("reopen", "Архивировать этап и зависимые этапы")
    sub.add_argument("stage", choices=state.STAGES)
    sub.add_argument("--reason", required=True)
    sub.add_argument("--expect", type=int, required=True)
    sub = command("track", "Записать проверку обязательства")
    sub.add_argument("commitment")
    sub.add_argument("--status", choices=("open", "done", "cancelled"), required=True)
    sub.add_argument("--note", required=True)
    sub.add_argument("--expect", type=int, required=True)
    sub = command("log", "Записать измерение времени, токенов или стоимости")
    sub.add_argument("--kind", choices=("model", "tool", "human"), required=True)
    sub.add_argument("--label", required=True)
    sub.add_argument("--seconds", type=int)
    sub.add_argument("--input-tokens", type=int)
    sub.add_argument("--output-tokens", type=int)
    sub.add_argument("--amount")
    sub.add_argument("--currency")
    sub.add_argument("--outcome", choices=("success", "timeout", "error", "cancelled", "unknown"), default="unknown")
    sub.add_argument("--attempts", type=int, default=1)
    sub = command("view", "Посмотреть строки снимка")
    sub.add_argument("source")
    sub.add_argument("--start", type=int, default=1)
    sub.add_argument("--end", type=int)
    sub = command("find", "Найти строки в активных источниках")
    sub.add_argument("query")
    sub.add_argument("--limit", type=int, default=10)
    sub = command("claim", "Показать вывод, фрагменты источников и пересчёт долей")
    sub.add_argument("claim", help="ID вывода, например C1")
    sub.add_argument("--revision", type=int, help="Архивная ревизия исследования из history")
    sub = command("rates", "Пересчитать доли по CSV/TSV")
    sub.add_argument("source")
    sub.add_argument("--by", nargs="+", default=[])
    sub.add_argument("--numerator", required=True)
    sub.add_argument("--denominator", required=True)
    sub.add_argument("--evidence", action="store_true", help="Выдать готовые refs и rate_checks для вывода")
    sub.add_argument("--group", action="append", default=[], metavar="COLUMN=VALUE",
                     help="Выбрать группы из --by; можно повторить")
    sub = command("cells", "Получить проверяемые значения ячеек и готовые ссылки")
    sub.add_argument("source")
    sub.add_argument("--rows", nargs="+", type=int, required=True)
    sub.add_argument("--columns", nargs="+", required=True)
    sub = command("check", "Проверить черновик без сохранения и без модели")
    sub.add_argument("stage", choices=state.STAGES)
    sub.add_argument("input", help="Путь к JSON-черновику")
    for name, help_ in (("report", "Вывести отчёт Markdown"), ("followup", "Проверить сроки обязательств")):
        sub = command(name, help_)
        sub.add_argument("--as-of", type=lambda value: date.fromisoformat(value).isoformat(),
                         default=date.today().isoformat())
    for name, help_ in (
        ("status", "Показать этап и ревизию"), ("context", "Показать сохранённый контекст"),
        ("sources", "Показать реестр источников"), ("history", "Показать архив решений"),
        ("journal", "Показать журнал изменений"), ("cost", "Показать учтённые затраты"),
        ("pause", "Поставить работу на паузу"), ("resume", "Снять паузу"),
        ("recover", "Найти сохранённые и незавершённые этапы после сбоя"),
    ):
        command(name, help_)
    return app


def main(argv=None):
    args = parser().parse_args(argv)
    handlers = {
        "allow-web": allow_web, "ingest": ingest, "revoke": revoke, "save": save, "decide": save,
        "reopen": reopen, "pause": pause_or_resume, "resume": pause_or_resume, "track": track, "log": log,
    }
    try:
        folder = state.case_dir(args.root, args.case)
        if args.command == "init":
            output = state.init(folder, args.request)
        else:
            output = handlers.get(args.command, inspect)(folder, args)
        if isinstance(output, str):
            print(output, end="")
        else:
            print(state.dump(output))
    except (state.UserError, OSError, ValueError, LookupError, csv.Error, InvalidOperation,
            sqlite3.Error, http.client.HTTPException, RecursionError) as error:
        print(state.dump({"error": str(error)}), file=sys.stderr)
        return 2
    return 0
