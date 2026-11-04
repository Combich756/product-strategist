"""Search snapshots and compute grouped ratios with source line references."""

import csv
import io
import re
from collections import defaultdict

from .checkpoints import need
from .sources import source_lines
from .state import UserError


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
    return [{"source": key, "line": line, "text": text} for _, key, line, text in hits[:limit]]


def rates(source, group_by, numerator, denominator):
    need(source["format"] in {"csv", "tsv"}, "Для rates нужен CSV или TSV.")
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
