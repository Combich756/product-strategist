import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class ReplayTests(unittest.TestCase):
    def test_reserve_replays_verified_source_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [sys.executable, 'demo/reserve/replay.py', '--root', directory, '--case', 'reserve-test'],
                cwd=REPO, capture_output=True, text=True, check=True, timeout=60,
            )
            export = Path(directory) / 'reserve-test/export'
            audit = json.loads((export / '04-audit.json').read_text(encoding='utf-8'))
            rates = [next(row for row in audit[step]['verified_rates'] if row['group'] == {'week': '2026-W44'})
                     for step in ('before', 'after')]
            self.assertEqual([(item['source'], item['percent']) for item in rates], [('S002', 35), ('S005', 50)])
            self.assertTrue(audit['before']['archived'])
            self.assertIn('## Рекомендация\n\n**O5.**', (export / '03-after.md').read_text(encoding='utf-8'))

    def test_saved_live_subagent_result_matches_the_demo_snapshots(self):
        fixture = REPO / 'demo/subagent-run'
        task = json.loads((fixture / 'task.json').read_text())
        result = json.loads((fixture / 'result.json').read_text())
        self.assertEqual(result['case'], task['case'])
        self.assertEqual(result['task_id'], task['task_id'])
        self.assertEqual(result['input_revision'], task['input_revision'])
        self.assertEqual(result['outcome'], 'partial')
        self.assertLessEqual(len(result['comparison'].split()), 250)
        with tempfile.TemporaryDirectory() as directory:
            seed = subprocess.run(
                [sys.executable, 'demo/replay.py', '--root', directory, '--case', task['case'], '--until', 'brief'],
                cwd=REPO, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(seed.returncode, 0, seed.stderr)
            database = Path(directory) / task['case'] / 'state.sqlite3'
            before = database.read_bytes()
            checked = subprocess.run(
                [sys.executable, '-m', 'strategist', '--root', directory, 'check', task['case'],
                 'research', str(fixture / 'result.json')],
                cwd=REPO, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertTrue(json.loads(checked.stdout)['valid'])
            self.assertEqual(database.read_bytes(), before)
            sources = subprocess.run(
                [sys.executable, '-m', 'strategist', '--root', directory, 'sources', task['case']],
                cwd=REPO, capture_output=True, text=True, check=True,
            )
            current = json.loads(sources.stdout)
            for source in task['sources']:
                self.assertEqual(current[source['source']]['text_sha256'], source['text_sha256'])

    def test_recorded_replay_revises_decision_after_new_data(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, 'demo/replay.py', '--root', directory, '--case', 'recorded'],
                cwd=REPO, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            status = subprocess.run(
                [sys.executable, '-m', 'strategist', '--root', directory, 'status', 'recorded'],
                cwd=REPO, capture_output=True, text=True, check=True,
            )
            state = json.loads(status.stdout)
            self.assertEqual(state['next'], 'decision')
            self.assertEqual(state['impact']['previous_decision'], 'O3')
            self.assertTrue(state['impact']['review_required'])
            self.assertEqual(state['active_decision']['option'], 'O3')
            self.assertTrue(state['active_decision']['under_review'])

    def test_approved_esim_decision_stays_in_force_until_a_new_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "esim"

            def run(command, *args, expected_code=0):
                result = subprocess.run(
                    [sys.executable, "-m", "strategist", "--root", directory, command, folder.name, *args],
                    cwd=REPO, capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, expected_code, result.stderr)
                return result.stdout if command == "report" else json.loads(
                    result.stderr if expected_code else result.stdout)

            subprocess.run(
                [sys.executable, "demo/replay.py", "--root", directory, "--case", folder.name, "--until", "decision"],
                cwd=REPO, capture_output=True, text=True, check=True, timeout=60,
            )
            approved = run("status")["revision"]
            run("track", "K1", "--status", "done", "--note", "Сверка выполнена", "--expect", str(approved))
            before = run("followup", "--as-of", "2026-11-08")
            (folder / "materials/note.md").write_text("Рабочее название проекта.", encoding="utf-8")
            note = run("ingest", "--file", "note.md", "--kind", "synthetic")["source"]
            after_import = run("status")
            self.assertEqual(after_import["next"], "monitor")
            self.assertEqual(after_import["pending_source_reviews"], [note])
            reviewed = run("review-source", note, "--outcome", "irrelevant", "--reason", "Название не меняет выбор",
                           "--expect", str(after_import["revision"]))
            self.assertEqual(reviewed["next"], "monitor")
            self.assertIsNone(reviewed["impact"])
            self.assertEqual(reviewed["pending_source_reviews"], [])
            self.assertEqual(run("history"), [])
            self.assertEqual(run("followup", "--as-of", "2026-11-08"), before)
            shutil.copyfile(REPO / "demo/materials/funnel_v2.csv", folder / "materials/funnel_v2.csv")
            run("ingest", "--file", "funnel_v2.csv", "--kind", "synthetic", "--replaces", "S002")
            reviewing = run("status")
            self.assertEqual(reviewing["next"], "research")
            self.assertIn("C1", reviewing["impact"]["affected_claims"])
            self.assertTrue(reviewing["active_decision"]["under_review"])
            self.assertEqual(run("followup", "--as-of", "2026-11-08")["commitments"], before["commitments"])
            self.assertIn("Действует, идёт пересмотр оснований", run("report"))
            self.assertIn("Ревизия изменилась", run(
                "track", "K2", "--status", "cancelled", "--note", "Подготовка отменена",
                "--expect", str(approved), expected_code=2,
            )["error"])
            # The extra note received S005, so the corrected funnel is S006 in this case.
            for stage in ("research", "options", "strategy"):
                path = REPO / "demo/checkpoints" / f"{stage}_v2.json"
                draft = folder / path.name
                content = path.read_text(encoding="utf-8")
                if stage == "research":
                    shutil.copyfile(REPO / "demo/materials/correction.md", folder / "materials/correction.md")
                    run("ingest", "--file", "correction.md", "--kind", "synthetic")
                    content = content.replace('"S006"', '"S007"').replace('"S005"', '"S006"')
                draft.write_text(content, encoding="utf-8")
                run("save", stage, str(draft), "--expect", str(run("status")["revision"]))

            pending = run("followup", "--as-of", "2026-11-08")
            self.assertEqual(pending["next"], "decision")
            self.assertEqual(pending["active_decision"]["approved_revision"], approved)
            self.assertEqual(pending["active_decision"]["option"], "O3")
            self.assertEqual([item["status"] for item in pending["commitments"]], ["done", "open"])
            replacement = json.loads((REPO / "demo/checkpoints/decision_v1.json").read_text(encoding="utf-8"))
            replacement["option"] = "O1"
            replacement["rationale"] = "После коррекции достаточно контролировать полноту событий."
            replacement["commitments"] = [{
                "id": "K1", "task": "Проверять полноту событий", "owner": "Аналитик",
                "due": "2026-11-20", "metric": "Расхождение с независимым журналом", "trigger": "Новый сбой",
            }]
            decision_file = folder / "decision-v2.json"
            decision_file.write_text(json.dumps(replacement, ensure_ascii=False), encoding="utf-8")
            run("check", "decision", str(decision_file))
            self.assertEqual(run("followup", "--as-of", "2026-11-08"), pending)
            run("track", "K2", "--status", "cancelled", "--note", "Владелец отменил подготовку",
                "--expect", str(run("status")["revision"]))
            decided = run("decide", str(decision_file), "--expect", str(run("status")["revision"]))
            self.assertEqual(decided["next"], "monitor")
            self.assertFalse(decided["impact"]["review_required"])
            self.assertEqual(decided["active_decision"]["option"], "O1")
            self.assertFalse(decided["active_decision"]["under_review"])
            current = run("followup", "--as-of", "2026-11-08")["commitments"]
            self.assertEqual([(item["id"], item["status"]) for item in current], [("K1", "open")])
            archive = run("history")[0]
            self.assertEqual(archive["tracking"]["K1"]["status"], "done")
            self.assertEqual(archive["final_tracking"]["K2"]["status"], "cancelled")
            self.assertEqual(run("journal")[-1]["replaces_decision_revision"], approved)

    def test_pending_strategy_can_be_recovered_without_approving_a_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def run(*args):
                result = subprocess.run(
                    [sys.executable, '-m', 'strategist', '--root', directory, args[0], 'pending', *args[1:]],
                    cwd=REPO, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            seed = subprocess.run(
                [sys.executable, 'demo/replay.py', '--root', directory, '--case', 'pending', '--until', 'brief'],
                cwd=REPO, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(seed.returncode, 0, seed.stderr)
            for stage in ('research', 'options'):
                run('save', stage, str(REPO / 'demo' / 'checkpoints' / f'{stage}_v1.json'),
                    '--expect', str(run('status')['revision']))
            drafts = root / 'pending' / 'drafts'
            drafts.mkdir()
            strategy = drafts / 'strategy.json'
            shutil.copyfile(REPO / 'demo/checkpoints/strategy_v1.json', strategy)
            recovered = run('recover')
            self.assertEqual(recovered['next'], 'strategy')
            self.assertTrue(recovered['drafts'][0]['can_save'])
            before = run('status')['revision']
            run('save', 'strategy', str(strategy), '--expect', str(before))
            after = run('status')['revision']
            run('save', 'strategy', str(strategy), '--expect', str(after))
            self.assertEqual(run('status')['revision'], after)
            self.assertEqual(run('status')['next'], 'decision')
            shutil.copyfile(REPO / 'demo/checkpoints/decision_v1.json', drafts / 'decision.json')
            choice = next(x for x in run('recover')['drafts'] if x['stage'] == 'decision')
            self.assertTrue(choice['human_choice_required'])
            self.assertNotIn('suggested_command', choice)
            self.assertNotIn('decision', run('status')['checkpoints'])


if __name__ == '__main__':
    unittest.main()
