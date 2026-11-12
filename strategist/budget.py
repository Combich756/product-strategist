"""Token accounting for guarded Qwen runs, independent of checkpoint revisions."""

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from math import ceil

from . import state

DEFAULT_MODEL = "@cf/qwen/qwen3.8-27b"


@contextmanager
def connection(folder, *, write=False):
    uri = (folder / "state.sqlite3").resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn, conn:
        conn.row_factory = sqlite3.Row
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn


def runs(folder):
    with connection(folder) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qwen_runs'"
        ).fetchone()
        if not exists:
            return []
        return [dict(row) for row in conn.execute("SELECT * FROM qwen_runs ORDER BY rowid")]


def totals(records):
    return {
        field: sum(row[field] for row in records)
        for field in ("input_tokens", "output_tokens", "reserved_tokens", "requests",
                      "measured_requests", "rejected_requests")
    }


def usage(folder, *, summary_only=False):
    records = runs(folder)
    return {
        **totals(records),
        "by_stage": {
            stage: totals([row for row in records if row["stage"] == stage])
            for stage in state.STAGES if any(row["stage"] == stage for row in records)
        },
        **({} if summary_only else {"runs": records}),
    }


def stage_samples(folder, records, stage, model):
    def measured(rows):
        return [
            row["input_tokens"] + row["output_tokens"] for row in rows
            if row["stage"] == stage and row["model"] == model
            and row["outcome"] == "checkpoint_saved"
            and row["measured_requests"] > 0 and row["reserved_tokens"] == 0
        ]

    samples = measured(records)
    for database in folder.parent.glob("*/state.sqlite3"):
        if (database.parent != folder and database.is_file()
                and not database.is_symlink() and not database.parent.is_symlink()):
            samples.extend(measured(runs(database.parent)))
    return sorted(samples)


def plan(folder, data, *, limit, reserve, estimate, model):
    state.need(limit > 0 and reserve >= 0 and (estimate is None or estimate > 0),
               "Нужен положительный лимит, неотрицательный резерв и положительная оценка.")
    records = runs(folder)
    recorded = totals(records)
    spent = recorded["input_tokens"] + recorded["output_tokens"]
    remaining = max(0, limit - spent - recorded["reserved_tokens"])
    stage = state.next_stage(data)
    samples = (stage_samples(folder, records, stage, model)
               if estimate is None and stage in state.STAGES[:-1] else [])
    if estimate is not None:
        estimate_source = "explicit"
    elif samples:
        estimate = samples[ceil(0.9 * len(samples)) - 1]
        estimate_source = "p90"
    else:
        estimate_source = None
    required = estimate + reserve if estimate is not None else None
    if stage == "decision":
        decision = "human_decision"
    elif stage == "monitor":
        decision = "complete"
    elif required is None:
        decision = "needs_estimate"
    elif required > remaining:
        decision = "insufficient"
    else:
        decision = "start"
    return {
        "case": data["case"], "next": stage, "paused": data["paused"], "model": model,
        "scope": "guarded_qwen_runs", "limit_tokens": limit, "known_spent_tokens": spent,
        "unmeasured_reserved_tokens": recorded["reserved_tokens"], "remaining_tokens": remaining,
        "estimate_tokens": estimate, "estimate_source": estimate_source, "sample_count": len(samples),
        "reserve_tokens": reserve, "required_tokens": required, "decision": decision,
        "shortfall_tokens": max(0, required - remaining) if required is not None else None,
    }


def start_run(folder, stage, model):
    run_id = uuid.uuid4().hex
    with connection(folder, write=True) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qwen_runs (
                id TEXT PRIMARY KEY, stage TEXT NOT NULL, model TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL DEFAULT 'running',
                input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                reserved_tokens INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
                measured_requests INTEGER NOT NULL DEFAULT 0, rejected_requests INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("INSERT INTO qwen_runs (id, stage, model, started_at) VALUES (?, ?, ?, ?)",
                     (run_id, stage, model, state.utc_now()))
    return run_id


def reserve_request(folder, run_id, stage, limit, tokens):
    with connection(folder, write=True) as conn:
        data = json.loads(conn.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()[0])
        if state.next_stage(data) != stage:
            return {"reason": "checkpoint_reached"}
        if data["paused"]:
            return {"reason": "case_paused"}
        outcome = conn.execute("SELECT outcome FROM qwen_runs WHERE id=?", (run_id,)).fetchone()[0]
        if outcome != "running":
            return {"reason": "run_finished"}
        charged = conn.execute("""
            SELECT COALESCE(SUM(input_tokens + output_tokens + reserved_tokens), 0) FROM qwen_runs
        """).fetchone()[0]
        if tokens > limit - charged:
            return {"reason": "request_budget", "required_tokens": tokens,
                    "remaining_tokens": max(0, limit - charged)}
        conn.execute("""
            UPDATE qwen_runs SET reserved_tokens=reserved_tokens+?, requests=requests+1 WHERE id=?
        """, (tokens, run_id))
    return None


def settle_request(folder, run_id, reservation, measured=None, *, rejected=False):
    if measured is None and not rejected:
        return
    input_tokens, output_tokens = measured if measured is not None else (0, 0)
    with connection(folder, write=True) as conn:
        conn.execute("""
            UPDATE qwen_runs SET input_tokens=input_tokens+?, output_tokens=output_tokens+?,
                reserved_tokens=reserved_tokens-?, measured_requests=measured_requests+?,
                rejected_requests=rejected_requests+? WHERE id=?
        """, (input_tokens, output_tokens, reservation, int(measured is not None), int(rejected), run_id))


def finish_run(folder, run_id, outcome):
    with connection(folder, write=True) as conn:
        conn.execute("UPDATE qwen_runs SET outcome=?, finished_at=? WHERE id=?",
                     (outcome, state.utc_now(), run_id))
