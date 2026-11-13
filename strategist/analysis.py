"""Search snapshots and compute grouped ratios with source line references."""

import csv
import io
import re
from collections import defaultdict

from .sources import TABLE_FORMATS, line_location, source_lines
from .state import UserError, need


def table_records(source):
    """Read physical snapshot line ranges, including multiline cells."""
    need(source["format"] in TABLE_FORMATS, "Нужен CSV, TSV или лист XLSX.")
    reader = csv.reader(io.StringIO(source["text"], newline=""),
                        delimiter="\t" if source["format"] == "tsv" else ",", strict=True)
    header = next(reader, [])
    need(header and all(header) and len(set(header)) == len(header),
         "В таблице пустые или повторные названия столбцов.")
    records = []
    while True:
        first = reader.line_num + 1
        row = next(reader, None)
        if row is None:
            break
        if not row:
            continue
        need(len(row) == len(header), f"Строка {first}: количество полей не совпадает с заголовком.")
        records.append((first, reader.line_num, dict(zip(header, row))))
    return header, records


def merged_refs(source_id, ranges):
    """Produce exact citations without asking a model to copy line numbers."""
    merged = []
    for first, last in sorted(set(ranges)):
        if merged and first <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(last, merged[-1]["end"])
        else:
            merged.append({"source": source_id, "start": first, "end": last})
    return merged


def cells(source_id, source, rows, columns):
    header, records = table_records(source)
    need(rows and len(rows) <= 100 and all(type(row) is int and row > 1 for row in rows),
         "Выберите до 100 строк данных, начиная со строки 2.")
    need(columns and len(set(columns)) == len(columns) and set(columns) <= set(header),
         "Укажите существующие неповторяющиеся столбцы.")
    need(len(set(rows)) * len(columns) <= 200, "Выберите не больше 200 ячеек за вызов.")
    selected = {first: (last, record) for first, last, record in records if first in rows}
    need(set(selected) == set(rows), "Строка отсутствует или не является началом CSV-записи снимка.")
    checks = []
    for first in sorted(selected):
        _, record = selected[first]
        for column in columns:
            checks.append({"source": source_id, "line": first, "column": column, "expected": record[column]})
    return {"refs": merged_refs(source_id, ((first, last) for first, (last, _) in selected.items())),
            "cell_checks": checks}


def rate_evidence(source_id, result):
    return {
        "refs": merged_refs(source_id, ((ref["start"], ref["end"])
                                        for group in result["groups"] for ref in group["refs"])),
        "rate_checks": [{"source": source_id, "group": group["group"],
                         "numerator": result["numerator_column"], "denominator": result["denominator_column"],
                         "expected": {field: group[field] for field in ("numerator", "denominator")}}
                        for group in result["groups"]],
    }


def format_rate(result):
    counts = f"{result['numerator']}/{result['denominator']}"
    if result["percent"] is None:
        return f"{counts}, доля не определена"
    return f"{counts} = {result['percent']:g}%"


def select_groups(result, conditions):
    selection = {}
    for condition in conditions:
        key, separator, value = condition.partition("=")
        need(separator and key and value and key not in selection,
             "--group: используйте неповторяющиеся пары столбец=значение.")
        selection[key] = value
    if not selection:
        return result
    need(set(selection) <= set(result["groups"][0]["group"]),
         "--group выбирает только столбцы из --by.")
    groups = [group for group in result["groups"]
              if all(group["group"][key] == value for key, value in selection.items())]
    need(groups, "--group: подходящие группы отсутствуют.")
    return {**result, "groups": groups, "rows": sum(len(group["refs"]) for group in groups)}


def find(state, query, limit):
    words = set(re.findall(r"\w+", query.casefold()))
    need(words and 1 <= limit <= 50, "Нужен текстовый запрос и limit от 1 до 50.")
    hits = []
    for key, source in state["sources"].items():
        if source["status"] != "active":
            continue
        for index, line in enumerate(source_lines(source), 1):
            score = len(words.intersection(re.findall(r"\w+", line.casefold())))
            if score:
                hits.append((score, key, index, line[:500]))
    hits.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [{"source": key, "line": line, "text": text, **line_location(state["sources"][key], line)}
            for _, key, line, text in hits[:limit]]


def rates(source, group_by, numerator, denominator):
    need(source["format"] in TABLE_FORMATS, "Для rates нужен CSV, TSV или лист XLSX.")
    columns = [*group_by, numerator, denominator]
    need(len(set(columns)) == len(columns), "Поля группировки, числителя и знаменателя должны различаться.")
    stream = io.StringIO(source["text"], newline="")
    rows = csv.reader(stream, delimiter="\t" if source["format"] == "tsv" else ",", strict=True)
    header = next(rows, [])
    need(len(set(header)) == len(header), "В таблице повторяются названия столбцов.")
    absent = set(columns) - set(header)
    need(not absent, "Нет столбцов: " + ", ".join(sorted(absent)))
    sums = defaultdict(lambda: {"numerator": 0, "denominator": 0, "refs": []})
    total_rows = 0
    seen = set()
    while True:
        first = rows.line_num + 1
        row = next(rows, None)
        if row is None:
            break
        if not row:
            continue
        need(len(row) == len(header), f"Строка {first}: количество полей не совпадает с заголовком.")
        record = dict(zip(header, row))
        need(all(record[col].strip() for col in columns), f"Строка {first}: пропуск в используемых полях.")
        signature = tuple(row)
        need(signature not in seen, f"Строка {first}: найден полный дубликат агрегированной строки.")
        seen.add(signature)
        try:
            top, bottom = (int(record[name]) for name in (numerator, denominator))
        except ValueError:
            raise UserError(f"Строка {first}: показатели должны быть целыми счётчиками.") from None
        need(0 <= top <= bottom, f"Строка {first}: нужно 0 ≤ числитель ≤ знаменатель.")
        group = tuple(record[col] for col in group_by)
        value = sums[group]
        value["numerator"] += top
        value["denominator"] += bottom
        value["refs"].append({"start": first, "end": rows.line_num})
        total_rows += 1
    need(total_rows, "Таблица не содержит строк данных.")
    return {
        "numerator_column": numerator, "denominator_column": denominator, "rows": total_rows,
        "groups": [{
            "group": dict(zip(group_by, group)), **value,
            "percent": round(100 * value["numerator"] / value["denominator"], 2) if value["denominator"] else None,
        } for group, value in sorted(sums.items())],
    }
