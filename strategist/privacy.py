"""Local checks for common credential and contact patterns."""

import re
import sys

from .state import UserError


SECRETS = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "credential": re.compile(
        r"(?i)\b(?:gh[pousr]_[a-z0-9]{25,}|github_pat_[a-z0-9_]{25,}|"
        r"gsk_[a-z0-9]{25,}|sk-(?:proj-)?[a-z0-9_-]{24,})\b"
        r"|\bbearer[ \t]+[a-z0-9._~+/-]{20,}"
        r"|\b(?:api[ _-]?(?:key|token)|access[ _-]?token|client[ _-]?secret|password)"
        r"""["']?[ \t]*[:=][ \t]*["']?[a-z0-9._~+/-]{20,}"""
    ),
    "token_url": re.compile(
        r"""(?i)[?&](?:access_token|api[_-]?key|auth[_-]?token|token|x-amz-signature|signature|sig)=[^&\s"'<>]+"""
    ),
}
CONTACTS = {
    "email": re.compile(r"(?<![\w.+%-])[\w.+%-]+@(?:[\w-]+\.)+[\w-]{2,63}(?![\w-])"),
    "phone": re.compile(
        r"(?<![\w+])(?:\+7[ \t-]*(?:\(\d{3}\)|\d{3})|8[ \t-]*\(\d{3}\))"
        r"[ \t-]*\d{3}[ \t-]*\d{2}[ \t-]*\d{2}(?!\w)"
    ),
}
LABELS = {
    "private_key": "закрытый ключ", "credential": "учётные данные",
    "token_url": "ссылка с токеном", "email": "адрес почты", "phone": "телефон",
}


def scan(text):
    counts = {}
    for kind, pattern in (SECRETS | CONTACTS).items():
        count = sum(1 for _ in pattern.finditer(text))
        if count:
            counts[kind] = count
    return counts


def describe(findings):
    return ", ".join(f"{LABELS[kind]} × {count}" for kind, count in findings.items())


def require_clean(text, location):
    findings = scan(text)
    if findings:
        raise UserError(f"{location}. Найдены чувствительные шаблоны ({describe(findings)}). "
                        "Подготовьте версию с закрытыми значениями.")


def review(text, subject, *, interactive=True):
    findings = scan(text)
    if not findings:
        return text, {}
    detail = describe(findings)
    if any(kind in SECRETS for kind in findings):
        raise UserError(f"Сохранение остановлено ({detail}). Удалите ключи и токены из материала.")
    if not interactive or not sys.stdin.isatty():
        raise UserError(f"Нужна ручная проверка ({detail}). Запустите команду в обычном терминале, "
                        "чтобы подтвердить сохранение копии с замаскированными контактами.")
    print(f"Найдены контактные данные ({detail}). Сохранить {subject} с маскировкой? [да/нет] ",
          end="", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip().casefold() not in {"да", "yes"}:
        raise UserError("Сохранение отменено пользователем.")
    for kind in findings:
        text = CONTACTS[kind].sub("[PII]", text)
    return text, findings
