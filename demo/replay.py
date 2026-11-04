"""Replay the recorded example through the same CLI used for new cases."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from time import perf_counter

DEMO = Path(__file__).resolve().parent
REPO = DEMO.parent
REQUEST = "Просела доля первых подключений туристической eSIM. Срочно переделаем онбординг или причина в другом?"
APPLE = "https://support.apple.com/en-us/118227"
AIRALO = "https://www.airalo.com/help/getting-started-with-airalo/E8DFWD88SHUX/when-does-my-esim-data-package-expire/0HBQDX9CSUPR"
AS_OF = "2026-11-08"


def replay(root, name, until, reports):
    def run(*args, parse=True):
        result = subprocess.run(
            [sys.executable, "-m", "strategist", "--root", str(root), args[0], name, *args[1:]],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode:
            raise RuntimeError(f"{' '.join(args)}\n{result.stderr}")
        return json.loads(result.stdout) if parse else result.stdout

    def file(name_, kind, *args):
        shutil.copyfile(DEMO / "materials" / name_, root / name / "materials" / name_)
        return run("ingest", "--file", name_, "--kind", kind, *args)["source"]

    def save(stage, artifact):
        revision = str(run("status")["revision"])
        command = ("decide",) if stage == "decision" else ("save", stage)
        return run(*command, str(DEMO / "checkpoints" / f"{artifact}.json"), "--expect", revision)

    def numbers(source):
        return {
            "weekly": run("rates", source, "--by", "week", "--numerator", "connected_48h",
                          "--denominator", "eligible_orders"),
            "platforms": run("rates", source, "--by", "week", "platform", "--numerator", "connected_48h",
                             "--denominator", "eligible_orders"),
        }

    def write(filename, value):
        if reports:
            reports.mkdir(parents=True, exist_ok=True)
            (reports / filename).write_text(value, encoding="utf-8")

    started = perf_counter()
    run("init", REQUEST)
    file("pilot.md", "synthetic")
    original = file("funnel_v1.csv", "synthetic")
    for url, filename in ((APPLE, "apple_notes.md"), (AIRALO, "airalo_notes.md")):
        run("allow-web", url)
        file(filename, "public", "--origin", url)
    save("brief", "brief")
    print(f"Кейс {name}: бриф и 4 источника сохранены.")
    if until == "brief":
        return

    first = numbers(original)
    save("research", "research_v1")
    run("pause")
    print(f"Пауза после исследования. Сохранённый следующий этап: {run('status')['next']}.")
    # Each command starts a fresh process. The project resumes from the case database.
    run("resume")
    save("options", "options_v1")
    save("strategy", "strategy_v1")
    save("decision", "decision_v1")
    write("01-before.md", run("report", "--as-of", AS_OF, parse=False))
    print(f"Демо-решение O3. Срок первого обязательства на {AS_OF}: "
          f"{run('followup', '--as-of', AS_OF)['commitments'][0]['due_state']}.")
    if until == "decision":
        return

    corrected = file("funnel_v2.csv", "synthetic", "--replaces", original)
    file("correction.md", "synthetic")
    impact = run("status")["impact"]
    write("02-impact.json", json.dumps(impact, ensure_ascii=False, indent=2) + "\n")
    print("На пересмотр: " + ", ".join(impact["affected_claims"]) + ".")
    second = numbers(corrected)
    save("research", "research_v2")
    save("options", "options_v2")
    save("strategy", "strategy_v2")
    write("03-after.md", run("report", "--as-of", AS_OF, parse=False))
    write("04-calculations.json", json.dumps({"before": first, "after": second}, ensure_ascii=False, indent=2) + "\n")
    print(f"Новая рекомендация O1; следующий этап: {run('status')['next']}.")
    print(f"Время воспроизведения CLI: {perf_counter() - started:.2f} с. Вызовов моделей: 0.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO / "cases")
    parser.add_argument("--case", default="demo")
    parser.add_argument("--until", choices=("brief", "decision", "review"), default="review")
    parser.add_argument("--reports", type=Path)
    args = parser.parse_args()
    try:
        replay(args.root.resolve(), args.case, args.until, args.reports.resolve() if args.reports else None)
    except RuntimeError as error:
        parser.exit(2, f"{error}\n")
