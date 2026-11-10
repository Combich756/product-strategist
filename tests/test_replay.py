import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class ReplayTests(unittest.TestCase):
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
