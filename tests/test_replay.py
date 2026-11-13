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
