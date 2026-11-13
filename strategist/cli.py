"""Command-line tools invoked by the product strategist inside an IDE."""

import argparse
import csv
import hashlib
import http.client
import os
import re
import sqlite3
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from time import perf_counter
from urllib.parse import unquote

from . import analysis, budget, checkpoints, privacy, qwen, recovery, sources, state, timing
from .report import claim_evidence, report


def source_or_error(data, key):
    source = data["sources"].get(key)
    checkpoints.need(source is not None, f"Источник {key} отсутствует.")
    return source


def allow_web(folder, args):
    privacy.require_clean(unquote(args.url), "Адрес источника")
    url = sources.normalize_url(args.url)
    with state.case(folder, write=True) as data:
        if url not in data["allowed_urls"]:
            data["allowed_urls"].append(url)
            state.event(data, "allow-web", url=url)
        return {"allowed_url": url}


def ingest(folder, args):
    started = perf_counter()
    checkpoints.need(args.sheet is None or args.file is not None, "--sheet допустим только для локального XLSX.")
    source_dates = timing.source_timing(args.period, args.data_as_of, args.valid_from, args.valid_through,
                                        args.timezone)
    for value in (args.url, args.origin, args.file, args.sheet):
        if value:
            privacy.require_clean(unquote(value), "Имя или адрес источника")
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
        source = sources.read_local(folder, args.file, args.kind, origin, sheet=args.sheet)

    for field in ("origin", "file", "sheet"):
        if source.get(field):
            privacy.require_clean(source[field], "Имя или адрес источника")
    source["text"], masked = privacy.review(source["text"], "снимок источника",
                                             interactive=not os.environ.get(qwen.RUN_ENV))
    if masked:
        source["text_sha256"] = hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
        source["masked_contacts"] = masked

    with state.case(folder, write=True, expect=revision) as data:
        replaced = args.replaces
        for key, old in data["sources"].items():
            if (old["status"] == "active" and old["origin"] == source["origin"]
                    and old.get("sheet") == source.get("sheet")):
                content_fields = ("text_sha256", "kind", "acquisition", "format", "page_starts", "pages_without_text")
                if all(old.get(field) == source.get(field) for field in content_fields):
                    old_dates = old.get("timing", {})
                    source_dates = {**old_dates, **source_dates}
                    if source_dates == old_dates:
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
        if source_dates:
            source["timing"] = source_dates
        data["sources"][key] = source
        state.invalidate_for_source(data, key, replaced)
        details = {"masked_contacts": masked} if masked else {}
        state.event(data, "ingest", source=key, replaced=replaced, **details,
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
    run_id = os.environ.get(qwen.RUN_ENV)
    if run_id:
        checkpoints.need(folder.name == os.environ.get(qwen.CASE_ENV) and stage == os.environ.get(qwen.STAGE_ENV),
                         "Этот запуск Qwen сохраняет только запланированный этап своего кейса.")
    return recovery.save_draft(folder, stage, state.load_json(args.input), expect=args.expect, run_id=run_id)


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


def cost(data, *, summary_only=False):
    entries = [item for item in data["journal"] if item["action"] == "log"]
    amounts = {}
    totals = {"model": 0, "tool": 0, "human": 0}
    unknown = 0
    tokens = {"input": None, "output": None}
    missing_tokens = {"input": 0, "output": 0}
    for item in entries:
        totals[item["kind"]] += item["seconds"] or 0
        if item["amount"] is None:
            unknown += 1
        else:
            currency = item["currency"]
            amounts[currency] = amounts.get(currency, Decimal(0)) + Decimal(item["amount"])
        if item["kind"] == "model":
            for side in tokens:
                value = item.get(f"{side}_tokens")
                if value is None:
                    missing_tokens[side] += 1
                else:
                    tokens[side] = (tokens[side] or 0) + value
    result = {
        "measurement_status": "logged_only" if entries else "unmeasured",
        "known_logged_cost": {currency: str(amount) for currency, amount in amounts.items()},
        "logged_seconds": totals, "log_entries_without_cost": unknown,
        "logged_tokens": tokens, "model_entries_without_token_counts": missing_tokens,
        "successful_ingest_ms": sum(item.get("duration_ms", 0) for item in data["journal"]),
        "note": "Поля logged_* учитывают записи log. Фактический расход qwen-run показан отдельно в qwen_usage. "
                "Ноль записей не означает нулевую стоимость. Интервалы между сохранениями не измеряют время модели.",
    }
    if not summary_only:
        result["entries"] = entries
    return result


def inspect(folder, args):
    with state.case(folder) as data:
        match args.command:
            case "status":
                return state.summary(data)
            case "context":
                result = {"request": data["request"], **state.summary(data),
                          "analysis": state.analysis_context(data), "allowed_urls": data["allowed_urls"]}
                if args.stage:
                    checkpoints.need(args.stage in data["checkpoints"], f"Этап {args.stage} ещё не сохранён.")
                    result["saved"] = {args.stage: data["checkpoints"][args.stage]}
                elif not args.compact:
                    result["saved"] = data["checkpoints"]
                return result
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
                        "lines": [{"line": line, "text": lines[line - 1], **sources.line_location(source, line)}
                                  for line in range(args.start, last + 1)]}
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
                result = cost(data, summary_only=args.summary)
                measured = budget.usage(folder, summary_only=args.summary)
                if measured["by_stage"]:
                    result["qwen_usage"] = measured
                    result["measurement_status"] = (
                        "qwen_only" if result["measurement_status"] == "unmeasured" else "logged_and_qwen"
                    )
                return result
            case "budget":
                return budget.plan(folder, data, limit=args.limit, reserve=args.reserve,
                                   estimate=args.estimate, model=args.model)
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

    sub = command("init", "Создать кейс")
    sub.add_argument("request")
    sub.add_argument("--analysis-at", help="Момент анализа ISO 8601 со смещением. По умолчанию время создания.")
    sub.add_argument("--timezone", help="Часовой пояс анализа, например Europe/Moscow или +03:00.")
    command("allow-web", "Разрешить конкретный HTTPS URL").add_argument("url")
    sub = command("ingest", "Импортировать снимок")
    target = sub.add_mutually_exclusive_group(required=True)
    target.add_argument("--file", help="Путь внутри materials/ кейса")
    target.add_argument("--url", help="Явно разрешённый HTTPS URL")
    sub.add_argument("--sheet", help="Название листа локального XLSX. Обязательно для книги с несколькими листами.")
    sub.add_argument("--kind", choices=("own", "synthetic", "public"))
    sub.add_argument("--origin", help="Разрешённый URL для локальной копии или выписки")
    sub.add_argument("--replaces", metavar="SOURCE")
    sub.add_argument("--period", nargs=2, metavar=("START", "END"), help="Даты периода данных включительно.")
    sub.add_argument("--data-as-of", metavar="DATE_OR_TIMESTAMP", help="Дата среза или момент ISO 8601 со смещением.")
    sub.add_argument("--valid-from", metavar="DATE", help="Дата начала действия условия включительно.")
    sub.add_argument("--valid-through", metavar="DATE", help="Последняя дата действия условия включительно.")
    sub.add_argument("--timezone", help="Часовой пояс календарных дат источника, если известен.")
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
    sub = command("rates", "Пересчитать доли по CSV, TSV или листу XLSX")
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
    sub = command("context", "Показать сохранённый контекст")
    scope = sub.add_mutually_exclusive_group()
    scope.add_argument("--compact", action="store_true", help="Показать состояние без содержимого этапов")
    scope.add_argument("--stage", choices=state.STAGES, help="Добавить содержимое одного сохранённого этапа")
    command("cost", "Показать учтённые затраты").add_argument(
        "--summary", action="store_true", help="Показать суммы без списка записей")
    for name, help_ in (
        ("budget", "Оценить токены до следующего чекпоинта"),
        ("qwen-run", "Запустить Qwen с проверкой бюджета перед этапом и запросом"),
    ):
        sub = command(name, help_)
        sub.add_argument("--limit", type=int, required=True, help="Суммарный токенный бюджет кейса для qwen-run")
        sub.add_argument("--estimate", type=int, help="Оценка расхода одного этапа, если вы задаёте её вручную")
        sub.add_argument("--reserve", type=int, default=10_000, help="Резерв к оценке этапа, по умолчанию 10000")
        sub.add_argument("--model", default=budget.DEFAULT_MODEL)
        if name == "qwen-run":
            sub.add_argument("--compact", action="store_true",
                             help="Собрать options и strategy из чекпоинтов одним запросом на этап")
            sub.add_argument("--account", required=True, help="Cloudflare Account ID")
            sub.add_argument("--max-output-tokens", type=int, default=8192, help="Предел вывода одного запроса")
            sub.add_argument("--request-timeout", type=int, default=90, help="Тайм-аут ответа Cloudflare в секундах")
            sub.add_argument("--wall-time", type=int, default=600, help="Предел времени сеанса Qwen Code в секундах")
    for name, help_ in (
        ("status", "Показать этап и ревизию"),
        ("sources", "Показать реестр источников"), ("history", "Показать архив решений"),
        ("journal", "Показать журнал изменений"),
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
        "qwen-run": qwen.run,
    }
    try:
        folder = state.case_dir(args.root, args.case)
        if args.command == "init":
            clock = None
            if args.analysis_at is not None or args.timezone is not None:
                clock = timing.analysis_clock(args.analysis_at, args.timezone)
            request, _ = privacy.review(args.request, "запрос кейса", interactive=not os.environ.get(qwen.RUN_ENV))
            output = state.init(folder, request, analysis=clock)
        else:
            output = handlers.get(args.command, inspect)(folder, args)
        if isinstance(output, str):
            print(output, end="")
        else:
            print(state.dump(output))
    except (state.UserError, OSError, ValueError, OverflowError, LookupError, csv.Error, InvalidOperation,
            sqlite3.Error, http.client.HTTPException, RecursionError) as error:
        print(state.dump({"error": str(error)}), file=sys.stderr)
        return 2
    return 0
