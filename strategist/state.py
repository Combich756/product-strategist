"""Transactional case checkpoints and their dependency history."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

STAGES = ("brief", "research", "options", "strategy", "decision")


class UserError(Exception):
    pass


def need(condition, message):
    if not condition:
        raise UserError(message)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def load_json(path):
    path = Path(path)
    need(path.stat().st_size <= 1_000_000, "JSON-артефакт больше 1 МБ.")
    return parse_json(path.read_text(encoding="utf-8-sig"))


def parse_json(text):
    def reject_constant(value):
        raise ValueError(f"Не допускается числовое значение {value}")

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, f"JSON: повторное поле {key!r}.")
            result[key] = value
        return result

    return json.loads(text, parse_constant=reject_constant,
                      object_pairs_hook=unique_fields)


def case_dir(root, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise UserError("Имя кейса: 1–64 символа, латиница, цифры, _ или -.")
    return Path(root) / name


def init(folder, request, *, analysis=None):
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
            "active_decision": None,
            "tracking": {},
            "journal": [],
        }
        event(state, "init")
        state["analysis"] = analysis if analysis is not None else analysis_context(state)
        try:
            conn.execute("INSERT INTO snapshot VALUES (1, ?)", (dump(state),))
        except sqlite3.IntegrityError:
            raise UserError(f"Кейс {folder.name} уже существует.") from None
    return {**summary(state), "analysis": state["analysis"]}


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
            if "active_decision" not in state:
                restore_approved_decision(state)
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


def analysis_context(state):
    if "analysis" in state:
        return state["analysis"]
    created_at = next((entry["at"] for entry in state["journal"] if entry["action"] == "init"), None)
    return {"as_of": created_at, "timezone": "UTC", "periods": []} if created_at else None


def restore_approved_decision(state):
    """Recover the approval and tracking recorded by older versions of the CLI."""
    decision = state["checkpoints"].get("decision")
    if decision is None:
        for entry in reversed(state["history"]):
            if "decision" in entry["checkpoints"]:
                decision = entry["checkpoints"]["decision"]
                state["tracking"] = entry["tracking"].copy()
                break
    state["active_decision"] = None
    if decision is not None:
        approved_revision = next((entry["revision"] for entry in reversed(state["journal"])
                                  if entry["action"] == "save" and entry.get("stage") == "decision"), None)
        state["active_decision"] = {"approved_revision": approved_revision, "artifact": decision}


def decision_summary(state):
    active = state["active_decision"]
    if active is None:
        return None
    artifact = active["artifact"]
    return {
        "option": artifact["option"], "approved_by": artifact["approved_by"],
        "approved_revision": active["approved_revision"],
        "under_review": "decision" not in state["checkpoints"],
    }


def pending_source_reviews(state):
    return [key for key, source in state["sources"].items()
            if source["status"] == "active" and source.get("review_status") == "pending"]


def strategy_review_status(state):
    saved = state["checkpoints"]
    if "strategy" not in saved:
        return None
    # Snapshots are immutable. Bind a review to its actual inputs across pauses and restarts.
    cited = {ref["source"] for claim in saved["research"]["claims"] for ref in claim["refs"]}
    inputs = [state["case"], analysis_context(state),
              [saved[key] for key in STAGES[:4]],
              {key: {field: value for field, value in state["sources"][key].items()
                     if field in ("text_sha256", "kind", "timing", "sheet", "format", "origin", "status")}
               for key in cited}]
    encoded = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    version = hashlib.sha256(encoded).hexdigest()
    record = state.get("strategy_reviews", {}).get(version, {"status": "pending"})
    return {"version": version, **record}


def summary(state):
    return {
        "case": state["case"],
        "revision": state["revision"],
        "paused": state["paused"],
        "next": next_stage(state),
        "checkpoints": list(state["checkpoints"]),
        "active_sources": [key for key, source in state["sources"].items() if source["status"] == "active"],
        "pending_source_reviews": pending_source_reviews(state),
        "impact": state["impact"],
        "active_decision": decision_summary(state),
        **({"strategy_review": strategy_review_status(state)} if "strategy" in state["checkpoints"] else {}),
    }


def archive(state, stage, reason):
    start = STAGES.index(stage)
    previous = {key: state["checkpoints"][key] for key in STAGES[start:] if key in state["checkpoints"]}
    if previous:
        if "strategy" in previous:
            review = strategy_review_status(state)
            if review["status"] == "in_progress":
                state["strategy_reviews"][review["version"]] = {"status": "pending"}
        archived = {
            "revision": state["revision"], "at": utc_now(), "reason": reason,
            "checkpoints": previous, "tracking": state["tracking"].copy() if "decision" in previous else {},
            "analysis": analysis_context(state),
        }
        if "research" in previous and "brief" not in previous:
            archived["brief"] = state["checkpoints"]["brief"]
        state["history"].append(archived)
        for key in previous:
            del state["checkpoints"][key]


def activate_decision(state, artifact):
    previous = state["active_decision"]
    if previous is not None:
        for entry in reversed(state["history"]):
            if "decision" in entry["checkpoints"]:
                entry["final_tracking"] = state["tracking"].copy()
                break
    state["active_decision"] = {"approved_revision": state["revision"], "artifact": artifact}
    state["tracking"] = {}
    if state["impact"]:
        state["impact"]["review_required"] = False


def source_review_archive(state):
    if state["impact"] is None:
        return None
    revision = state["impact"]["baseline_revision"]
    return next((entry for entry in reversed(state["history"]) if entry["revision"] == revision), None)


def archived_brief(state, archive):
    brief = archive.get("brief", archive["checkpoints"].get("brief"))
    if brief is not None:
        return brief
    # Earlier archives omitted the brief when only later stages were reopened.
    edited = any(entry["revision"] > archive["revision"] and entry["action"] == "save"
                 and entry.get("stage") == "brief" for entry in state["journal"])
    return None if edited else state["checkpoints"].get("brief")


def source_review_baseline(state):
    saved = state["checkpoints"]
    if "research" in saved:
        return saved
    archived = source_review_archive(state)
    if archived is None:
        return saved
    brief = archived.get("brief", saved.get("brief", {}))
    return {"brief": brief, **archived["checkpoints"]}


def register_source_change(state, source_id, replaced=None):
    has_research = "research" in state["checkpoints"]
    if replaced:
        baseline = source_review_baseline(state)
        claims = baseline.get("research", {}).get("claims", [])
        cited = any(ref["source"] == replaced for claim in claims for ref in claim["refs"])
        changes = (state["impact"] or {}).get("source_changes", [])
        part_of_review = not has_research and any(change["source"] == replaced for change in changes)
        if cited or part_of_review:
            invalidate_for_source(state, source_id, replaced)
            return
    if source_id and has_research:
        state["sources"][source_id]["review_status"] = "pending"


def invalidate_for_source(state, source_id, replaced=None, *, claims=(), questions=(), metrics=(), reason=None):
    saved = state["checkpoints"]
    impact = state["impact"]
    if "research" in saved:
        active = state["active_decision"]
        impact = {
            "baseline_revision": state["revision"],
            "source_changes": [],
            "previous_decision": active["artifact"]["option"] if active else None,
            "review_required": True,
        }
        state["impact"] = impact
        archive(state, "research", reason or f"Изменение источников: {source_id or replaced}")
    elif impact is None:
        return
    baseline = source_review_baseline(state)
    change = {"source": source_id, "replaced": replaced}
    if reason is not None:
        change.update(reason=reason, claims=list(claims))
    if questions:
        change["questions"] = list(questions)
    if metrics:
        change["metrics"] = list(metrics)
    impact["source_changes"].append(change)
    retired = {change["replaced"] for change in impact["source_changes"]}
    flagged = {key for change in impact["source_changes"] for key in change.get("claims", [])}
    affected = [
        claim["id"] for claim in baseline["research"]["claims"]
        if claim["id"] in flagged or any(ref["source"] in retired for ref in claim["refs"])
    ]
    options = baseline.get("options", {}).get("options", [])
    strategy = baseline.get("strategy", {})
    impacted = set(affected)
    impact["affected_claims"] = affected
    bindings = baseline["research"].get("metrics", {})
    related_metrics = [
        key for key, binding in bindings.items()
        if isinstance(binding, dict) and binding.get("claim") in impacted
    ] if isinstance(bindings, dict) else []
    metrics = [key for change in impact["source_changes"] for key in change.get("metrics", [])]
    questions = [key for change in impact["source_changes"] for key in change.get("questions", [])]
    impact["affected_metrics"] = list(dict.fromkeys(related_metrics + metrics))
    impact["affected_questions"] = list(dict.fromkeys(questions))
    impact["affected_options"] = [item["id"] for item in options if set(item["claims"]) & impacted]
    impact["affected_strategy_sections"] = [
        key for key, field in (("product", "claims"), ("communication", "proof_claims"), ("test", "claims"))
        if set(strategy.get(key, {}).get(field, [])) & impacted
    ]


def followup(state, as_of):
    active = state["active_decision"]
    result = {"as_of": as_of, "commitments": [], "next": next_stage(state),
              "active_decision": decision_summary(state)}
    if active is None:
        return result
    decision = active["artifact"]
    result["active_decision"]["rationale"] = decision["rationale"]
    as_of = date.fromisoformat(as_of)
    for item in decision["commitments"]:
        check = state["tracking"].get(item["id"], {})
        due = date.fromisoformat(item["due"])
        status = check.get("status", "open")
        result["commitments"].append({
            **item,
            "status": status,
            "due_state": ("overdue" if due < as_of else "due" if due == as_of else "upcoming")
            if status == "open" else "closed",
            "last_check": check or None,
        })
    return result
