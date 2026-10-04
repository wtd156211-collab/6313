"""quota_sim 的 unittest 测试套件。

运行: python3 -m unittest test_quota_sim -v
"""

import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import quota_sim  # noqa: E402

CLI = [sys.executable, os.path.join(ROOT, "quota_sim.py")]
TREES = os.path.join(ROOT, "samples", "trees")
SCRIPTS = os.path.join(ROOT, "samples", "scripts")
EXPECTED = os.path.join(ROOT, "samples", "expected")


def run_cli(*args):
    return subprocess.run(CLI + list(args), capture_output=True, text=True)


class SampleOutputTests(unittest.TestCase):
    """samples/expected 必须与 stdout 逐字节一致。"""

    def check_sample(self, name):
        proc = run_cli("--trees", TREES,
                       "--script", os.path.join(SCRIPTS, name + ".ops"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(EXPECTED, name + ".out"),
                  encoding="utf-8") as fh:
            self.assertEqual(proc.stdout, fh.read())

    def test_deep(self):
        self.check_sample("deep")

    def test_siblings(self):
        self.check_sample("siblings")

    def test_zero(self):
        self.check_sample("zero")

    def test_deterministic(self):
        args = ("--trees", TREES, "--script", os.path.join(SCRIPTS, "deep.ops"))
        first = run_cli(*args)
        second = run_cli(*args)
        self.assertEqual(first.returncode, second.returncode)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stderr, second.stderr)


class EngineSemanticsTests(unittest.TestCase):
    """引擎语义: 超时、偿还顺序、不变量。"""

    def test_timeout_zero_reclaims_before_next_op(self):
        tree = "node r - 10 0\nnode a r 0 0\n"
        script = ("tree t\ntimeout 0\n"
                  "o1 t=0 APPLY a 5\n"
                  "o2 t=0 APPLY a 5\n")
        lines, _ = self._run(tree, script)
        self.assertIn("EVENT t=0 action=RECLAIM id=b1 borrower=a lender=r "
                      "amount=5 start=0", lines)
        event_idx = lines.index("EVENT t=0 action=RECLAIM id=b1 "
                                "borrower=a lender=r amount=5 start=0")
        self.assertTrue(lines[event_idx + 1].startswith(
            "STEP t=0 id=o2 "), lines[event_idx + 1])
        self.assertEqual(lines[-1], "DEBT t=0 b2=a->r:5/5@0")

    def test_repay_oldest_record_first(self):
        tree = "node r - 100 0\nnode a r 10 0\n"
        script = ("tree t\ntimeout 100\n"
                  "o1 t=0 APPLY a 20\n"
                  "o2 t=1 APPLY a 20\n"
                  "o3 t=2 RELEASE a 5\n")
        lines, _ = self._run(tree, script)
        self.assertIn("DEBT t=2 b1=a->r:5/10@0 b2=a->r:20/20@1", lines)

    def test_reclaim_order_by_start_then_borrower(self):
        tree = ("node r - 100 0\n"
                "node b r 0 0\nnode a r 0 0\n")
        script = ("tree t\ntimeout 5\n"
                  "o1 t=0 APPLY a 3\n"
                  "o2 t=0 APPLY b 3\n"
                  "o3 t=5 APPLY a 1\n")
        lines, _ = self._run(tree, script)
        events = [ln for ln in lines if ln.startswith("EVENT ")]
        self.assertEqual(events, [
            "EVENT t=5 action=RECLAIM id=b1 borrower=a lender=r amount=3 start=0",
            "EVENT t=5 action=RECLAIM id=b2 borrower=b lender=r amount=3 start=0",
        ])

    def test_invariants_random_ops(self):
        rng = random.Random(20261004)
        tree = "node r - 500 0\nnode a r 100 0\nnode b r 100 0\nnode c a 50 0\n"
        ops = []
        for i in range(300):
            node = rng.choice(["a", "b", "c"])
            action = rng.choice(["APPLY", "APPLY", "RELEASE"])
            amount = rng.randint(1, 120)
            ops.append("o%d t=%d %s %s %d" % (i, i, action, node, amount))
        script = "tree t\ntimeout 7\n" + "\n".join(ops) + "\n"
        lines, steps = self._run(tree, script)
        # 不变量在 simulate 内部逐步校验, 见 _run 的 hook
        self.assertEqual(len(steps), 300)

    def _run(self, tree_text, script_text, keep_nodes=False):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "t.tree"), "w", encoding="utf-8") as fh:
                fh.write(tree_text)
            script_path = os.path.join(tmp, "s.ops")
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write(script_text)
            name, _, timeout, ops = quota_sim.load_script(script_path)
            nodes, by_id, _ = quota_sim.load_tree(os.path.join(tmp, name + ".tree"))
            lines, steps = quota_sim.simulate(nodes, by_id, timeout, ops,
                                              keep_steps=True)
            quotas = {n.id: n.quota for n in nodes}
            for step in steps:
                for nid, (used, own, debt, lent) in step["state"].items():
                    self.assertEqual(used, own + debt)
                    self.assertLessEqual(own + lent, quotas[nid])
                    got_debt = sum(d["remain"] for d in step["debts"]
                                   if d["borrower"] == nid)
                    got_lent = sum(d["remain"] for d in step["debts"]
                                   if d["lender"] == nid)
                    self.assertEqual(debt, got_debt, step["t"])
                    self.assertEqual(lent, got_lent, step["t"])
            return lines, steps


class ErrorHandlingTests(unittest.TestCase):
    """非法输入: 退出码 2, stderr 为 ERROR <行号> <说明>。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trees = os.path.join(self.tmp.name, "trees")
        os.makedirs(self.trees)
        with open(os.path.join(self.trees, "t.tree"), "w",
                  encoding="utf-8") as fh:
            fh.write("node r - 10 0\nnode a r 5 0\n")

    def write_script(self, text):
        path = os.path.join(self.tmp.name, "s.ops")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def write_tree(self, text):
        with open(os.path.join(self.trees, "t.tree"), "w",
                  encoding="utf-8") as fh:
            fh.write(text)

    def expect_error(self, script=None, tree=None):
        if tree is not None:
            self.write_tree(tree)
        path = self.write_script(script if script is not None
                                 else "tree t\ntimeout 1\no1 t=0 APPLY a 1\n")
        proc = run_cli("--trees", self.trees, "--script", path)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertRegex(proc.stderr, r"^ERROR [0-9]+ .+")
        self.assertEqual(proc.stdout, "")

    def test_duplicate_node_id(self):
        self.expect_error(tree="node r - 10 0\nnode a r 5 0\nnode a r 5 0\n")

    def test_unknown_parent(self):
        self.expect_error(tree="node r - 10 0\nnode a zz 5 0\n")

    def test_used_exceeds_quota(self):
        self.expect_error(tree="node r - 10 0\nnode a r 5 6\n")

    def test_root_not_first(self):
        self.expect_error(tree="node a r 5 0\nnode r - 10 0\n")

    def test_two_roots(self):
        self.expect_error(tree="node r - 10 0\nnode r2 - 10 0\n")

    def test_time_regression(self):
        self.expect_error(script="tree t\ntimeout 1\n"
                                 "o1 t=5 APPLY a 1\no2 t=4 APPLY a 1\n")

    def test_duplicate_op_id(self):
        self.expect_error(script="tree t\ntimeout 1\n"
                                 "o1 t=0 APPLY a 1\no1 t=1 APPLY a 1\n")

    def test_unknown_node_in_op(self):
        self.expect_error(script="tree t\ntimeout 1\no1 t=0 APPLY zz 1\n")

    def test_amount_out_of_range(self):
        self.expect_error(script="tree t\ntimeout 1\no1 t=0 APPLY a 0\n")
        self.expect_error(script="tree t\ntimeout 1\no1 t=0 APPLY a 1000000001\n")

    def test_missing_timeout_header(self):
        self.expect_error(script="tree t\no1 t=0 APPLY a 1\n")

    def test_missing_tree_file(self):
        proc = run_cli("--trees", self.trees,
                       "--script", self.write_script("tree nope\ntimeout 1\n"))
        self.assertEqual(proc.returncode, 2)
        self.assertRegex(proc.stderr, r"^ERROR [0-9]+ .+")

    def test_error_line_numbers_are_physical_lines(self):
        proc = run_cli("--trees", self.trees,
                       "--script", self.write_script(
                           "# 注释占第 1 行\ntree t\n# 第 3 行\ntimeout 1\n"
                           "o1 t=0 APPLY a 1\no2 t=0 APPLY zz 1\n"))
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.startswith("ERROR 6 "), proc.stderr)


class HtmlReportTests(unittest.TestCase):
    def test_html_self_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.html")
            proc = run_cli("--trees", TREES,
                           "--script", os.path.join(SCRIPTS, "deep.ops"),
                           "--html", out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as fh:
                html = fh.read()
        for forbidden in ("<script src", "<link", "http://", "https://"):
            self.assertNotIn(forbidden, html)
        self.assertIn("<svg", html)
        self.assertIn("EVENT", html)
        self.assertIn("RECLAIM", html)
        # 数值与文本输出一致: 第一步 d 节点 used=40 quota=10
        self.assertIn('"d":[40,10,30,0]', html)


class PerformanceTests(unittest.TestCase):
    def test_50_nodes_5000_ops_within_budget(self):
        rng = random.Random(7)
        with tempfile.TemporaryDirectory() as tmp:
            trees = os.path.join(tmp, "trees")
            os.makedirs(trees)
            lines = ["node n0 - 100000 0"]
            for i in range(1, 50):
                parent = "n%d" % rng.randrange(i)
                lines.append("node n%d %s %d 0" % (i, parent, rng.randint(0, 2000)))
            with open(os.path.join(trees, "perf.tree"), "w",
                      encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            ops = ["tree perf", "timeout 25"]
            for i in range(5000):
                node = "n%d" % rng.randrange(50)
                action = "APPLY" if rng.random() < 0.6 else "RELEASE"
                ops.append("o%d t=%d %s %s %d"
                           % (i, i, action, node, rng.randint(1, 3000)))
            script = os.path.join(tmp, "perf.ops")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write("\n".join(ops) + "\n")
            out_path = os.path.join(tmp, "perf.out")
            start = time.monotonic()
            with open(out_path, "w", encoding="utf-8") as out:
                proc = subprocess.run(
                    CLI + ["--trees", trees, "--script", script],
                    stdout=out, stderr=subprocess.PIPE, text=True)
            elapsed = time.monotonic() - start
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertLess(elapsed, 5.0, "超过 5 秒预算: %.2fs" % elapsed)
            with open(out_path, encoding="utf-8") as fh:
                out_lines = fh.read().splitlines()
            self.assertEqual(
                sum(1 for ln in out_lines if ln.startswith("STEP ")), 5000)
            # 确定性: 重放一遍逐字节一致
            with open(os.path.join(tmp, "perf2.out"), "w",
                      encoding="utf-8") as out2:
                subprocess.run(CLI + ["--trees", trees, "--script", script],
                               stdout=out2, check=True)
            with open(out_path, "rb") as f1, \
                    open(os.path.join(tmp, "perf2.out"), "rb") as f2:
                self.assertEqual(f1.read(), f2.read())


if __name__ == "__main__":
    unittest.main()
