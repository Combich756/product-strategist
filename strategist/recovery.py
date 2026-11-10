"""Local draft validation and crash recovery; no model or network calls."""

import csv
import re
import shlex
from pathlib import Path

from . import checkpoints, state


def check_draft(data, stage, artifact):
    artifact = checkpoints.object_(artifact, stage)
    prerequisites = state.STAGES[:state.STAGES.index(stage)]
    state.need(all(key in data["checkpoints"] for key in prerequisites),
               "Не сохранены предыдущие этапы; сначала прочитайте context.")
    checkpoints.VALIDATORS[stage](data, artifact)
    assumptions = artifact.get("assumptions", [])
    state.need(isinstance(assumptions, list) and all(isinstance(x, str) and x.strip() for x in assumptions),
               "assumptions: ожидается список непустых строк.")
    warnings = []
    if stage in {"options", "strategy"} and not assumptions:
        warnings.append("Не указаны assumptions: проверьте основания оценок труда, денежных эффектов и порогов.")
    return {
        "valid": True, "stage": stage, "revision": data["revision"], "paused": data["paused"],
        "can_save": not data["paused"] and state.next_stage(data) == stage and stage != "decision",
        "warnings": warnings,
        "validated": ["structure", "source_ranges", "declared_calculations"],
        "manual_review_required": "Смысл ссылок, причинность, экономика и достаточность доказательств требуют проверки.",
        "human_choice_required": stage == "decision",
    }


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
        except (state.UserError, OSError, ValueError, KeyError, RecursionError, csv.Error) as error:
            item.update(valid=False, error=str(error))
        drafts.append(item)
    return {
        **state.summary(data), "drafts": drafts,
        "guidance": "Сверьте черновик и основания. Сохранение этапа не утверждает решение. "
                    "При паузе сначала resume; после любой записи обновите ревизию. "
                    "decide выполняется только после явного выбора человека.",
    }
