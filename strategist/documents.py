"""Extract bounded local document snapshots without loading optional parsers at startup."""

import csv
import io
from zipfile import ZipFile

from .state import UserError, need

FORMATS = {"pdf", "docx", "xlsx"}
MAX_BYTES = 10_000_000
MAX_OFFICE_BYTES = 50_000_000


def check_length(length, limit):
    need(length <= limit, f"Текст снимка превышает {limit} символов. Разбейте документ на части.")


def extract_pdf(raw, limit):
    from pypdf import PdfReader
    from pypdf.errors import DependencyError

    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
    except DependencyError:
        raise UserError("PDF защищён. Для импорта нужна копия без шифрования.") from None
    need(not reader.is_encrypted, "PDF защищён. Для импорта нужна копия без шифрования.")
    pages = []
    starts = []
    without_text = 0
    length = 0
    next_line = 1
    for page in reader.pages:
        text = "\n".join(page.extract_text().splitlines()).strip()
        if not text:
            without_text += 1
            text = "[Страница без извлекаемого текста]"
        length += len(text) + bool(pages)
        check_length(length, limit)
        pages.append(text)
        starts.append(next_line)
        next_line += text.count("\n") + 1
    need(without_text < len(pages), "В PDF нет извлекаемого текста. Для скана сначала выполните OCR.")
    return "\n".join(pages), {"page_count": len(pages), "page_starts": starts, "pages_without_text": without_text}


def docx_blocks(container):
    from docx.text.paragraph import Paragraph

    for block in container.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text
            if text.strip():
                yield text
        else:
            for row in block.rows:
                cells = [" / ".join(docx_blocks(cell)) for cell in row.cells]
                if any(cells):
                    yield " | ".join(cells)


def extract_docx(raw, limit):
    from docx import Document

    document = Document(io.BytesIO(raw))
    blocks = []
    length = 0
    for block in docx_blocks(document):
        length += len(block) + bool(blocks)
        check_length(length, limit)
        blocks.append(block)
    need(blocks, "В DOCX нет извлекаемых абзацев или таблиц.")
    return "\n".join(blocks), {}


def cell_text(value):
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

    if value is None:
        return ""
    if isinstance(value, ArrayFormula):
        value = value.text or f"[Формула массива {value.ref}]"
    if isinstance(value, DataTableFormula):
        value = f"[Таблица данных Excel {value.ref}]"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def sheet_snapshot(sheet, limit):
    # Some spreadsheet producers report an incorrect used range in the XML.
    sheet.reset_dimensions()
    rows = []
    length = 0
    for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
        check_length(number, limit)
        width = next((index for index in range(len(row), 0, -1) if row[index - 1] is not None), 0)
        if not width:
            continue
        cells = [cell_text(value) for value in row[:width]]
        length += sum(map(len, cells)) + len(cells)
        check_length(length - 1, limit)
        rows.append((number, cells))
    need(rows, "Выбранный лист XLSX пуст.")

    width = max(len(cells) for _, cells in rows)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    continuations = []
    previous_row = 0
    next_line = 1
    for number, cells in rows:
        blank_rows = number - previous_row - 1
        stream.write("\n" * blank_rows)
        writer.writerow(cells + [""] * (width - len(cells)))
        check_length(stream.tell() - 1, limit)
        next_line += blank_rows
        line_count = 1 + sum(value.count("\n") for value in cells)
        continuations.extend(range(next_line + 1, next_line + line_count))
        next_line += line_count
        previous_row = number
    return stream.getvalue().removesuffix("\n"), {"sheet": sheet.title, "continuation_lines": continuations}


def extract_xlsx(raw, sheet_name, limit):
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    try:
        sheets = {sheet.title: sheet for sheet in book.worksheets}
        need(sheets, "В книге XLSX нет листов с ячейками.")
        if sheet_name is None and len(sheets) == 1:
            sheet_name = next(iter(sheets))
        names = ", ".join(repr(name) for name in sheets)
        need(sheet_name in sheets, f"Выберите лист XLSX через --sheet. Доступны {names}.")
        return sheet_snapshot(sheets[sheet_name], limit)
    finally:
        book.close()


def extract(raw, format_, *, sheet, limit):
    try:
        if format_ == "pdf":
            return extract_pdf(raw, limit)
        with ZipFile(io.BytesIO(raw)) as package:
            expanded = sum(entry.file_size for entry in package.infolist())
            need(expanded <= MAX_OFFICE_BYTES, f"Документ после распаковки превышает {MAX_OFFICE_BYTES} байт.")
        if format_ == "docx":
            return extract_docx(raw, limit)
        return extract_xlsx(raw, sheet, limit)
    except ImportError:
        raise UserError(
            f"Для импорта {format_.upper()} установите зависимости: python3 -m pip install -r requirements.txt"
        ) from None
    except UserError:
        raise
    except Exception as error:
        # The parsers raise different exceptions for malformed or unsupported files.
        raise UserError(
            f"Не удалось прочитать {format_.upper()} ({type(error).__name__}). Проверьте файл в редакторе."
        ) from None
