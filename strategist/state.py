"""Transactional case checkpoints and their dependency history."""

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

STAGES = ("brief", "research", "options", "strategy", "decision")


class UserError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def load_json(path):
    def reject_constant(value):
        raise ValueError(f"Не допускается числовое значение {value}")

    return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=reject_constant)


def case_dir(root, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise UserError("Имя кейса: 1–64 символа, латиница, цифры, _ или -.")
    return Path(root) / name


def init(folder, request):
    if not request.strip():
        raise UserError("В запросе должен быть текст.")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "materials").mkdir(exist_ok=True)
    db = folder / "state.sqlite3"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        state = {
            "schema": 1,
            "case": folder.name,
            "request": request,
            "revision": 0,
            "paused": False,
            "allowed_urls": [],
            "sources": {},
            "checkpoints": {},
            "history": [],
            "impact": None,
            "tracking": {},
            "journal": [],
        }
        event(state, "init")
        try:
            conn.execute("INSERT INTO snapshot VALUES (1, ?)", (dump(state),))
        except sqlite3.IntegrityError:
            raise UserError(f"Кейс {folder.name} уже существует.") from None
    return summary(state)


@contextmanager
def case(folder, *, write=False, expect=None, while_paused=False):
    db = folder / "state.sqlite3"
    if not db.is_file():
        raise UserError(f"Кейс {folder.name} не найден. Сначала выполните init.")
    conn = sqlite3.connect(db, timeout=10)
    try:
        with conn:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()
            if row is None:
                raise UserError("Кейс не инициализирован.")
            state = json.loads(row[0])
            if state["schema"] != 1:
                raise UserError("Неподдерживаемая версия состояния.")
            revision = state["revision"]
            if expect is not None and expect != revision:
                raise UserError(f"Ревизия изменилась: ожидается {expect}, текущая {revision}. Прочитайте status.")
            if write and state["paused"] and not while_paused:
                raise UserError("Кейс приостановлен. Для изменений выполните resume.")
            yield state
            if write and state["revision"] != revision:
                conn.execute("UPDATE snapshot SET payload=? WHERE id=1", (dump(state),))
    finally:
        conn.close()


def event(state, action, **details):
    state["revision"] += 1
    state["journal"].append({"revision": state["revision"], "at": utc_now(), "action": action, **details})


def next_stage(state):
    return next((stage for stage in STAGES if stage not in state["checkpoints"]), "monitor")


def summary(state):
    return {
        "case": state["case"],
        "revision": state["revision"],
        "paused": state["paused"],
        "next": next_stage(state),
        "checkpoints": list(state["checkpoints"]),
        "active_sources": [key for key, source in state["sources"].items() if source["status"] == "active"],
        "impact": state["impact"],
    }


def archive(state, stage, reason):
    start = STAGES.index(stage)
    previous = {key: state["checkpoints"][key] for key in STAGES[start:] if key in state["checkpoints"]}
    if previous:
        state["history"].append({
            "revision": state["revision"], "at": utc_now(), "reason": reason,
            "checkpoints": previous, "tracking": state["tracking"],
        })
        for key in previous:
            del state["checkpoints"][key]
        state["tracking"] = {}


def invalidate_for_source(state, source_id, replaced=None):
    saved = state["checkpoints"]
    impact = state["impact"]
    if "research" in saved:
        impact = {
            "baseline_revision": state["revision"],
            "source_changes": [],
            "previous_decision": saved.get("decision", {}).get("option"),
            "review_required": True,
        }
        state["impact"] = impact
        archive(state, "research", f"Изменение источников: {source_id or replaced}")
    elif impact is None:
        return
    baseline = next(item["checkpoints"] for item in reversed(state["history"])
                    if item["revision"] == impact["baseline_revision"])
    impact["source_changes"].append({"source": source_id, "replaced": replaced})
    retired = {change["replaced"] for change in impact["source_changes"]}
    affected = [
        claim["id"] for claim in baseline["research"]["claims"]
        if any(ref["source"] in retired for ref in claim["refs"])
    ]
    options = baseline.get("options", {}).get("options", [])
    strategy = baseline.get("strategy", {})
    impacted = set(affected)
    impact["affected_claims"] = affected
    impact["affected_options"] = [item["id"] for item in options if set(item["claims"]) & impacted]
    impact["affected_strategy_sections"] = [
        key for key, field in (("product", "claims"), ("communication", "proof_claims"), ("test", "claims"))
        if set(strategy.get(key, {}).get(field, [])) & impacted
    ]


def followup(state, as_of):
    decision = state["checkpoints"].get("decision")
    if decision is None:
        return {"as_of": as_of, "commitments": [], "next": next_stage(state)}
    as_of = date.fromisoformat(as_of)
    commitments = []
    for item in decision["commitments"]:
        check = state["tracking"].get(item["id"], {})
        due = date.fromisoformat(item["due"])
        status = check.get("status", "open")
        commitments.append({
            **item,
            "status": status,
            "due_state": ("overdue" if due < as_of else "due" if due == as_of else "upcoming")
            if status == "open" else "closed",
            "last_check": check or None,
        })
    return {"as_of": as_of.isoformat(), "commitments": commitments}
