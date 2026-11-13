"""Prepare a clean Reserve case or replay the saved live artifacts locally."""

import argparse
import json
from pathlib import Path
from shutil import copy2
import subprocess
import sys

DEMO = Path(__file__).resolve().parent
REPO = DEMO.parents[1]
PERIOD = ("--period", "2026-10-19", "2026-11-01")
PUBLIC = (
    ("backblaze.md", "https://www.backblaze.com/computer-backup/docs/en/create-a-restore"),
    ("onedrive.md", "https://support.microsoft.com/en-us/onedrive/restore-a-previous-version-of-a-file-stored-in-onedrive"),
)


def replay(root, name, prepare):
    def run(command, *args):
        result = subprocess.run(
            [sys.executable, "-m", "strategist", "--root", str(root), command, name, *args],
            cwd=REPO, capture_output=True, encoding="utf-8",
        )
        if result.returncode:
            raise RuntimeError(f"{command}: {result.stderr.strip()}")
        return result.stdout

    def save(stage, file):
        revision = json.loads(run("status"))["revision"]
        run("save", stage, str(DEMO / "results" / "checkpoints" / file), "--expect", str(revision))

    def table(file, cutoff, *extra):
        result = run("ingest", "--file", file, "--kind", "synthetic", "--sheet", "Trials",
                     *PERIOD, "--data-as-of", cutoff, "--timezone", "+03:00", *extra)
        return json.loads(result)["source"]

    request = (DEMO / "request.md").read_text(encoding="utf-8").split("\n\n", 2)[1]
    run("init", request, "--analysis-at", "2026-11-11T12:00:00+03:00", "--timezone", "Europe/Moscow")
    folder = root / name
    materials = folder / "materials"
    for file in sorted((DEMO / "materials").iterdir()):
        copy2(file, materials)
    copy2(DEMO / "request.md", folder)
    if prepare:
        print(f"Кейс {name} подготовлен. Передайте агенту {folder / 'request.md'} и ID кейса.")
        return

    save("brief", "brief.json")
    run("ingest", "--file", "pilot.md", "--kind", "synthetic")
    initial = table("trial_v1.xlsx", "2026-11-09T12:00:00+03:00")
    for file, url in PUBLIC:
        run("allow-web", url)
        run("ingest", "--file", file, "--kind", "public", "--origin", url)
    save("research", "research.json")
    run("pause")
    print(f"{name}: исследование на паузе. Следующий этап {json.loads(run('status'))['next']}.")
    run("resume")
    save("options", "options.json")
    save("strategy", "strategy.json")
    export = folder / "export"
    export.mkdir()
    (export / "01-before.md").write_text(run("report"), encoding="utf-8")

    for file in sorted((DEMO / "updates").iterdir()):
        copy2(file, materials)
    table("trial_v2.xlsx", "2026-11-11T12:00:00+03:00", "--replaces", initial)
    run("ingest", "--file", "reconciliation.md", "--kind", "synthetic", *PERIOD,
        "--data-as-of", "2026-11-11T12:00:00+03:00")
    impact = json.loads(run("status"))["impact"]
    for stage in ("research", "options", "strategy"):
        save(stage, f"{stage}-v2.json")
    (export / "03-after.md").write_text(run("report"), encoding="utf-8")
    audit = {"before": json.loads(run("claim", "C2", "--revision", str(impact["baseline_revision"]))),
             "after": json.loads(run("claim", "C2"))}
    (export / "04-audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{name}: стратегия пересмотрена, ожидается решение владельца. Отчёты в {export}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="reserve-demo")
    parser.add_argument("--root", type=Path, default=REPO / "cases")
    parser.add_argument("--prepare", action="store_true", help="Подготовить материалы для живого прогона")
    args = parser.parse_args()
    try:
        replay(args.root.resolve(), args.case, args.prepare)
    except (OSError, RuntimeError) as error:
        parser.exit(2, f"{error}\n")
