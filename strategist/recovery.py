"""Local draft validation and crash recovery; no model or network calls."""

import csv
import hashlib
import re
import shlex
from pathlib import Path

from . import budget, checkpoints, privacy, state


def review_context(data):
    archive = state.source_review_archive(data)
    state.need(archive is not None and "research" in archive["checkpoints"] and state.next_stage(data) == "research",
               "Для --review нужен открытый пересмотр исследования после изменения материалов.")
    previous = archive["checkpoints"]
    research = previous["research"]
    brief = data["checkpoints"]["brief"]
    same_brief = brief == state.archived_brief(data, archive)
    impact = data["impact"]
    affected = set(impact["affected_claims"])
    active = {key for key, source in data["sources"].items()
              if source["status"] == "active" and source.get("review_status") != "pending"}
    facts = [claim for claim in research["claims"]
             if same_brief and claim["type"] == "fact" and claim["id"] not in affected
             and claim["refs"] and all(ref["source"] in active for ref in claim["refs"])]
    fact_ids = {claim["id"] for claim in facts}
    previous_metrics = research.get("metrics", {}) if brief.get("metrics") else {}
    previous_questions = research.get("question_results", {}) if brief.get("require_question_results") else {}
    affected_metrics = set(impact.get("affected_metrics", []))
    affected_questions = set(impact.get("affected_questions", []))
    draft = {"comparison": "", "gaps": [], "claims": facts}
    if brief.get("metrics"):
        draft["metrics"] = {
            key: binding for key, binding in previous_metrics.items()
            if key not in affected_metrics and binding.get("claim") in fact_ids
        }
    if brief.get("require_question_results"):
        draft["question_results"] = {
            key: answer for key, answer in previous_questions.items()
            if key not in affected_questions and "gap" not in answer
            and answer.get("claims") and set(answer["claims"]) <= fact_ids
        }
    strategy = previous.get("strategy", {})
    return {
        "baseline_revision": archive["revision"],
        "draft": draft,
        "previous": {
            "comparison": research["comparison"],
            "gaps": research["gaps"],
            "claims": [{key: claim[key] for key in ("id", "type", "text", "limits", "refs")}
                       for claim in research["claims"] if claim["id"] not in fact_ids],
            "metrics": {key: binding for key, binding in previous_metrics.items()
                        if key not in draft.get("metrics", {})},
            "question_results": {key: answer for key, answer in previous_questions.items()
                                 if key not in draft.get("question_results", {})},
            "recommendation": {key: strategy[key] for key in ("option", "rationale", "revisit")} if strategy else None,
        },
    }


def check_draft(data, stage, artifact):
    artifact = checkpoints.object_(artifact, stage)
    privacy.require_clean(state.dump(artifact), "Черновик этапа")
    prerequisites = state.STAGES[:state.STAGES.index(stage)]
    state.need(all(key in data["checkpoints"] for key in prerequisites),
               "Не сохранены предыдущие этапы; сначала прочитайте context.")
    metrics = checkpoints.VALIDATORS[stage](data, artifact)
    assumptions = artifact.get("assumptions", [])
    state.need(isinstance(assumptions, list) and all(isinstance(x, str) and x.strip() for x in assumptions),
               "assumptions: ожидается список непустых строк.")
    warnings = []
    if stage in {"options", "strategy"} and not assumptions:
        warnings.append("Не указаны assumptions: проверьте основания оценок труда, денежных эффектов и порогов.")
    validated = ["structure", "source_ranges", "declared_calculations"]
    comparability = {}
    if stage == "research":
        comparability = {metric["id"]: metric["comparability_check"] for metric in metrics
                         if "comparability_check" in metric}
        blocked = [metric["id"] for metric in metrics if "threshold" in metric
                   and metric["id"] in comparability and comparability[metric["id"]]["status"] != "compatible"]
        if blocked:
            warnings.append(f"Порог не оценён для {', '.join(blocked)}. Проверьте условия сопоставимости.")
        if comparability:
            validated.append("declared_metric_comparability")
    if stage == "research" and data["checkpoints"]["brief"].get("require_question_results"):
        validated.append("question_coverage")
    return {
        "valid": True, "stage": stage, "revision": data["revision"], "paused": data["paused"],
        "can_save": not data["paused"] and state.next_stage(data) == stage and stage != "decision",
        "warnings": warnings,
        "validated": validated,
        "manual_review_required": "Смысл ссылок, причинность, экономика и достаточность доказательств требуют проверки.",
        "human_choice_required": stage == "decision",
        **({"metric_comparability": comparability} if comparability else {}),
    }


def save_draft(folder, stage, artifact, *, expect=None, run_id=None):
    artifact = checkpoints.object_(artifact, stage)
    with state.case(folder, write=True, expect=expect) as data:
        if run_id:
            state.need(any(row["id"] == run_id and row["outcome"] == "running" for row in budget.runs(folder)),
                       "Запуск Qwen уже завершён. Прочитайте status и recover.")
        if data["checkpoints"].get(stage) == artifact:
            return state.summary(data)
        state.need(state.next_stage(data) == stage,
                   f"Следующий этап — {state.next_stage(data)}. Для исправления используйте reopen.")
        review = check_draft(data, stage, artifact)
        data["checkpoints"][stage] = artifact
        if stage == "brief" and "analysis" in artifact:
            data["analysis"] = artifact["analysis"]
        details = {"qwen_run_id": run_id} if run_id else {}
        if stage == "decision" and data["active_decision"]:
            details["replaces_decision_revision"] = data["active_decision"]["approved_revision"]
        state.event(data, "save", stage=stage, **details,
                    artifact_sha256=hashlib.sha256(state.dump(artifact).encode()).hexdigest())
        if stage == "decision":
            state.activate_decision(data, artifact)
        return {**state.summary(data), "warnings": review["warnings"]}


def recover(folder, data):
    """Inspect drafts without executing anything, writing state or choosing a decision."""
    folder = Path(folder)
    draft_dir = folder / "drafts"
    paths = sorted(draft_dir.glob("*.json")) if draft_dir.is_dir() else []
    state.need(len(paths) <= 100, "В drafts больше 100 JSON-файлов. Укажите нужный файл через check.")
    drafts = []
    for path in paths:
        item = {"file": str(path)}
        match = re.match(r"^(brief|research|options|strategy|decision)(?:[._-]|$)", path.stem)
        if not match:
            item.update(valid=False, error="Этап не распознан по имени. Используйте check с явным этапом.")
            drafts.append(item)
            continue
        stage = match.group(1)
        item["stage"] = stage
        try:
            state.need(not path.is_symlink() and path.resolve().parent == draft_dir.resolve(),
                       "Черновик должен быть обычным файлом внутри drafts.")
            state.need(path.stat().st_size <= 1_000_000, "Черновик больше 1 МБ.")
            artifact = state.load_json(path)
            item["same_as_checkpoint"] = artifact == data["checkpoints"].get(stage)
            item.update(check_draft(data, stage, artifact))
            if item["can_save"]:
                item["suggested_command"] = shlex.join([
                    "python3", "-m", "strategist", "--root", str(folder.parent), "save", data["case"],
                    stage, str(path), "--expect", str(data["revision"]),
                ])
        except (state.UserError, OSError, ValueError, OverflowError, KeyError, RecursionError, csv.Error) as error:
            item.update(valid=False, error=str(error))
        drafts.append(item)
    summary = state.summary(data)
    review = summary.get("strategy_review")
    if review and review["status"] == "in_progress":
        receipt = folder / "reviews" / (review["ticket"] + ".json")
        if receipt.is_file() and not receipt.is_symlink() and not receipt.parent.is_symlink():
            review["result_file"] = str(receipt)
            review["suggested_command"] = shlex.join([
                "python3", "-m", "strategist", "--root", str(folder.parent),
                "review-strategy", data["case"], "--record", str(receipt),
            ])
    return {
        **summary, "drafts": drafts,
        "guidance": "Сверьте черновик и основания. Сохранение этапа не утверждает решение. "
                    "При паузе сначала resume; после любой записи обновите ревизию. "
                    "decide выполняется только после явного выбора человека.",
    }
