# @exports: LintTest(unittest.TestCase)   # test_clean_passes, test_leaky_fails, test_allow_guard_reports_hits, test_leak_entry_shape
# @imports: factory/bin/mandate_lint.py:main (invoked as a subprocess via CLI)
# @env: none
# @schema: fixtures at factory/selftest/fixtures/lint/{task.md, allow.txt, clean/planner.md, leaky/planner.md}
# @schema: run: python3 factory/selftest/test_lint.py   # exit 0 iff all pass
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINT = ROOT / "factory" / "bin" / "mandate_lint.py"
FIX = ROOT / "factory" / "selftest" / "fixtures" / "lint"


class LintTest(unittest.TestCase):
    def run_lint(self, mandates: Path):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "lint.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(LINT),
                    "--mandates",
                    str(mandates),
                    "--task",
                    str(FIX / "task.md"),
                    "--allow",
                    str(FIX / "allow.txt"),
                    "--out",
                    str(out),
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
            self.assertTrue(out.is_file(), f"lint wrote no output. stderr: {proc.stderr}")
            return proc, json.loads(out.read_text(encoding="utf-8"))

    def test_clean_passes(self):
        proc, res = self.run_lint(FIX / "clean")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(res["leaks"], [])
        self.assertGreater(res["tokens_checked"], 0)

    def test_leaky_fails(self):
        proc, res = self.run_lint(FIX / "leaky")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        leaked = {lk["token"] for lk in res["leaks"]}
        for expected in ("restock", "pantry", "shelf", "409"):
            self.assertIn(expected, leaked)

    def test_allow_guard_reports_hits(self):
        proc, res = self.run_lint(FIX / "clean")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("service", res["allow_hits"])

    def test_leak_entry_shape(self):
        _proc, res = self.run_lint(FIX / "leaky")
        self.assertTrue(res["leaks"])
        for lk in res["leaks"]:
            self.assertEqual(set(lk), {"file", "line", "token", "source"})
            self.assertTrue(lk["source"].endswith("task.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
