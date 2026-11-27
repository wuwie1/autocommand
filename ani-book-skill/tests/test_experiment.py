"""Exercise the whole loop with real Git and two local CLI doubles. No model service is used."""

import argparse
import contextlib
import csv
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assessment as a
import providers
import run
import worktrees as wt


FAKE_CLI = r'''#!/usr/bin/env python3
import hashlib, json, os, sys
from pathlib import Path
args = sys.argv[1:]
def flag(name):
    return args[args.index(name) + 1]
def event(kind):
    with Path(os.environ["ANI_TEST_EVENTS"]).open("a") as f:
        f.write(kind + "\n")
def seen(kind):
    path = Path(os.environ["ANI_TEST_EVENTS"])
    return path.read_text().splitlines().count(kind) if path.exists() else 0
mode = os.environ.get("ANI_TEST_MODE", "pass")
if Path(sys.argv[0]).name == "agent":
    if "--list-models" in args:
        event("models")
        print("claude-opus-5-5-medium - Claude Opus 5.5")
        sys.exit(0)
    assert flag("--mode") == "ask" and flag("--model") == "claude-opus-5-5-medium"
    assert flag("--sandbox") == "enabled" and flag("--output-format") == "json" and "--trust" in args
    folder = Path(flag("--workspace"))
    assert {p.name for p in folder.iterdir()} == {"评判材料.md"}
    contents = (folder / "评判材料.md").read_text()
    assert "round-" not in contents and "上一轮" not in contents and "skill-fixture" not in contents
    assert "本版规划引用的拆书原文" in contents and "Codex" not in contents
    earlier = seen("judge")
    event("judge")
    keys = os.environ["ANI_TEST_IDS"].split(",")
    score = 80 if mode == "pass" or (mode in {"improve","new-section"} and "已传入新规" in contents) else 79
    if mode == "waver":
        score = 80 if earlier % 2 == 0 else 79
    if mode == "bad-model":
        model = "some-other-model"
    else:
        model = flag("--model")
    evidence = [{"file":"开篇章纲.md", "quote": "伪造引文" if mode == "bad-quote" else "留一盏灯"},
                {"file":"分卷.md", "quote":"欠下的人情"}]
    result = {"ratings": [{"id": k, "score": score if k == keys[0] else 100, "evidence": evidence,
              "reason":"第1章有可定位的选择。", "next_fix":"回查分卷时怎样交付代价。"} for k in keys]}
    body = json.dumps(result, ensure_ascii=False)
    body = "好的，评分如下。\n" + body if mode == "chatty" else "```json\n" + body + "\n```"
    print(json.dumps({"type":"result","subtype":"success","is_error":False,"model":model,"request_id":"req","result":body}))
elif args == ["--version"]:
    print("codex-cli fixture")
else:
    assert args[0] == "exec" and flag("--sandbox") == "read-only"
    schema = json.loads(Path(flag("--output-schema")).read_text())["properties"]
    prompt = sys.stdin.read()
    tree = Path(flag("-C"))
    if "edits" in schema:
        event("optimize")
        path = "references/story-and-volume-planning.md"
        result = {"diagnosis":path+" 中未说明付出后的变化。","hypothesis":"把代价交给下一段。",
                  "edits":[{"path":path,"before":"初始流程","after":"初始流程\n明确代价。"}]}
        if mode == "new-section":
            result["edits"] = [{"path":"analyses/遮天/sections/局限.md","before":"","after":"古史残缺时的证据限度。\n"},
                               {"path":"analyses/遮天/sections/worldbuilding.md","before":"用选择和后果来验证。",
                                "after":"用选择和后果来验证。\n证据不足时见 [局限](局限.md)。"}]
        if mode == "orphan":
            result["edits"] = [{"path":"references/孤立说明.md","before":"","after":"没有任何文件指向这里。\n"}]
    else:
        event("generate")
        with Path(os.environ["ANI_TEST_PROMPTS"]).open("a") as f:
            f.write(hashlib.sha256(prompt.encode()).hexdigest() + "\n")
        seed = json.loads(Path(os.environ["ANI_TEST_CASE"]).read_text())
        # Follow a link the way the planner would, by opening the file in its own tree.
        linked = tree / "analyses/遮天/sections/局限.md"
        followed = "局限.md" in prompt and linked.is_file() and "证据限度" in linked.read_text()
        tag = "已传入新规" if "明确代价。" in prompt or followed else "初稿"
        result = {"title":"留灯", "synopsis":"渡口的灯保住了。"+tag,
                  "volumes":[{"title":"第一渡","wan":100,"plan":"省下灯油。"},
                             {"title":"最后一渡","wan":100,"plan":"欠下的人情要还。"}],
                  "opening":[{"title":"留灯","plan":"她选择留一盏灯。"} for _ in range(seed["opening_chapters"])],
                  "transfers":[{"book":r["book"],"file":r["files"][0],"anchor":r["anchor"],"use":"交给原创人物。"}
                               for r in seed["references"]]}
        if mode == "improve" and tag == "已传入新规":
            result["title"] = "归渡"
        if followed:
            result["transfers"][-1].update(file="sections/局限.md", anchor="证据限度")
    Path(flag("-o")).write_text(json.dumps(result,ensure_ascii=False))
'''


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "skill-fixture"
        self.repo.mkdir()
        self.case_path = run.HERE / "cases/三书玄幻.json"
        self.case = run.load(self.case_path)
        self.rubric = run.load(run.HERE / "rubric.json")
        # Isolate the 79/80 boundary exercised by the CLI doubles from the author's live thresholds.
        for dimension in self.rubric["dimensions"]:
            dimension["minimum"] = 80
        self.rubric_path = self.root / "rubric.json"
        run.save(self.rubric_path, self.rubric)
        self.events = self.root / "events"
        self.prompts = self.root / "prompts"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for cli in ("codex", "agent"):
            exe = bin_dir / cli
            exe.write_text(FAKE_CLI, encoding="utf-8")
            exe.chmod(0o755)
        env = dict(PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], ANI_TEST_EVENTS=str(self.events),
                   ANI_TEST_PROMPTS=str(self.prompts), ANI_TEST_CASE=str(self.case_path), ANI_TEST_MODE="pass",
                   CODEX_HOME=str(self.root / "codex"), ANI_TEST_IDS=",".join(d["id"] for d in self.rubric["dimensions"]))
        for mock in (patch.dict(os.environ, env), patch.object(run, "RESULTS", self.root / "results"),
                     patch.object(run, "TREES", self.root / "worktrees")):
            mock.start()
            self.addCleanup(mock.stop)
        wt.git(self.repo, "init", "-b", "main")
        for file in run.FLOW:
            run.write(self.repo / file, "初始流程\n")
        for ref in self.case["references"]:
            folder = self.repo / "analyses" / ref["book"]
            run.write(folder / "analysis-state.yaml", "source:\n  coverage: full\n")
            run.write(folder / "coverage-map.md", "已核对全书。\n")
            run.write(folder / "notes/segment-001.md", "原始阅读笔记。\n")
            for source in ref["files"]:
                run.write(folder / source, "## " + ref["anchor"] + "\n用选择和后果来验证。\n")
        self.commit("base")
        self.base = wt.git(self.repo, "rev-parse", "HEAD").strip()
        self.exp = run.run_path("fixture")

    def commit(self, message):
        wt.git(self.repo, "add", ".")
        wt.git(self.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", message)

    def execute(self, mode, maximum, identifier="fixture", base="HEAD", judgments=2):
        os.environ["ANI_TEST_MODE"] = mode
        args = argparse.Namespace(id=identifier, project=self.repo, base=base, case=self.case_path,
                                  rubric=self.rubric_path, max_optimizations=maximum,
                                  judgments=judgments, generator_model=None, timeout=30)
        with contextlib.redirect_stdout(io.StringIO()):
            run.start(args)

    def resume(self, mode, **options):
        os.environ["ANI_TEST_MODE"] = mode
        with contextlib.redirect_stdout(io.StringIO()):
            run.resume(self.exp, **options)

    def ledger(self):
        with (run.RESULTS / "results.tsv").open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f, delimiter="\t"))

    def test_baseline_pass_stops_before_first_optimization(self):
        self.execute("pass", 3)
        manifest = run.load(self.exp / "run.json")
        self.assertEqual(manifest["status"], "qualified")
        self.assertEqual(manifest["generator"], dict(cli="codex-cli fixture", model=None, effort=None))
        self.assertEqual(self.events.read_text().splitlines(), ["models", "generate", "judge", "judge"])
        self.assertEqual(len(self.ledger()), len(self.rubric["dimensions"]))

    def test_missing_cli_stops_before_an_experiment_is_created(self):
        with patch.object(run.shutil, "which", side_effect=lambda cmd: None if cmd == "codex" else "/bin/" + cmd):
            with self.assertRaisesRegex(ValueError, "codex"):
                self.execute("pass", 1)
        self.assertFalse(self.exp.exists())

    def test_regeneration_reads_changed_skill_and_judge_remains_independent(self):
        self.execute("improve", 4)
        rows = run.trials(self.exp)
        self.assertEqual([r["status"] for r in rows], ["baseline", "qualified"])
        scores = [r for r in self.ledger() if r["dimension"] == "promise"]
        self.assertEqual([(r["score"], r["minimum"], r["mean"], r["scores"], r["spread"]) for r in scores],
                         [("79", "80", "97.375", "79/79", "0"), ("80", "80", "97.5", "80/80", "0")])
        table = run.read(self.exp / "候选对照.md")
        self.assertIn("均分（满分 100）", table)
        self.assertIn("| 1 | 第 0 轮 |", table)
        self.assertIn("最接近达标的是第 1 轮", table)
        self.assertEqual(rows[1]["parent"], self.base)
        self.assertNotEqual(rows[1]["commit"], self.base)
        self.assertEqual(rows[1]["control"], run.control_digest())
        self.assertIn("已传入新规", run.read(self.exp / "round-001/总纲.md"))
        self.assertNotIn("明确代价", run.read(self.repo / "references/story-and-volume-planning.md"))
        self.assertEqual(wt.git(self.repo, "branch", "--show-current").strip(), "main")
        self.assertEqual(wt.git(self.repo, "status", "--porcelain").strip(), "?? novels_version/")
        for rec in rows:
            version = self.repo / rec["novel_version"]
            directory = run.rdir(self.exp, rec["round"])
            self.assertEqual(version, self.repo / "novels_version" / "留灯" / (directory.name + "-fixture"))
            for name in (*a.PRODUCTS, run.CITED, "plan.json"):
                self.assertEqual((version / name).read_bytes(), (directory / name).read_bytes())
        self.assertEqual(run.load(self.repo / rows[1]["novel_version"] / "plan.json")["title"], "归渡")
        self.assertIn("[书稿目录]", table)

    def test_budget_preserves_all_worktrees_and_human_can_select_any_scored_version(self):
        self.execute("flat", 2)
        rows = run.trials(self.exp)
        self.assertEqual([r["status"] for r in rows], ["baseline", "retained", "retained"])
        self.assertEqual([r["parent"] for r in rows], [self.base] * 3)
        self.assertEqual(len({r["worktree"] for r in rows}), 3)
        with contextlib.redirect_stdout(io.StringIO()):
            run.choose(self.exp, 1, "人工喜欢这一版")
        manifest = run.load(self.exp / "run.json")
        self.assertEqual(manifest["status"], "limit_reached")
        self.assertEqual(manifest["selections"][-1]["commit"], rows[1]["commit"])
        self.assertEqual(wt.git(self.repo, "rev-parse", "HEAD").strip(), self.base)
        self.assertEqual(len(self.ledger()), len(self.rubric["dimensions"]) * 3)

    def test_bad_judgment_is_not_a_score_and_resume_reuses_generated_outline(self):
        with self.assertRaisesRegex(ValueError, "引文"):
            self.execute("bad-quote", 0)
        self.assertEqual(self.ledger()[0]["score"], "")
        self.assertFalse((self.exp / "round-000/score.json").exists())
        self.assertTrue((self.repo / "novels_version/留灯/round-000-fixture/总纲.md").is_file())
        # Both rejected answers stay on disk for the author to read.
        self.assertEqual(len(list((self.exp / "round-000/judge-raw").iterdir())), 2)
        self.resume("flat", timeout=77)
        self.resume("flat")
        self.assertEqual(self.events.read_text().splitlines().count("generate"), 1)
        manifest = run.load(self.exp / "run.json")
        self.assertEqual((manifest["status"], manifest["timeout"]), ("limit_reached", 77))
        self.assertEqual(len(self.ledger()), len(self.rubric["dimensions"]))

    def test_a_dimension_passes_only_when_every_judgment_passes_it(self):
        self.execute("waver", 0)
        self.assertEqual(run.load(self.exp / "run.json")["status"], "limit_reached")
        rating = run.load(self.exp / "round-000/score.json")["ratings"]["promise"]
        self.assertEqual((rating["score"], rating["scores"], rating["spread"]), (79, [80, 79], 1))
        self.assertIn("| 1（卖点与开篇兑现） |", run.read(self.exp / "候选对照.md"))
        self.assertTrue((self.exp / "round-000/judgment-2.json").is_file())

    def test_answer_wrapped_in_a_sentence_is_still_read(self):
        self.execute("chatty", 0, judgments=1)
        self.assertEqual(run.trials(self.exp)[0]["status"], "baseline")
        for body in ('{"a": 1}', '```json\n{"a": 1}\n```', '说明如下：{"a": 1}。'):
            self.assertEqual(providers.parse(body), {"a": 1})
        with self.assertRaisesRegex(ValueError, "JSON"):
            providers.parse("没有对象")

    def test_frozen_inputs_and_cross_model_substitution(self):
        with self.assertRaisesRegex(ValueError, "模型"):
            self.execute("bad-model", 2)
        run.write(self.exp / "inputs/rubric.json", "{}")
        with self.assertRaisesRegex(ValueError, "冻结"):
            run.resume(self.exp)

    def test_unscored_invalid_candidate_counts_against_limit(self):
        with patch.object(wt, "validate_candidate", side_effect=wt.CandidateRejected("校验失败")):
            self.execute("flat", 1)
        self.assertEqual(run.load(self.exp / "run.json")["status"], "limit_reached")
        self.assertEqual(run.trials(self.exp)[1]["status"], "invalid")
        self.assertEqual(self.events.read_text().splitlines().count("judge"), 2)
        with self.assertRaisesRegex(ValueError, "已完成评分"):
            run.choose(self.exp, 1, "")

    def test_validation_that_litters_the_tree_rejects_the_candidate(self):
        run.write(self.repo / "tests/test_litter.py",
                  "import pathlib, unittest\nclass T(unittest.TestCase):\n"
                  "    def test_it(self):\n        pathlib.Path('leftover.tmp').write_text('x')\n")
        self.commit("tests")
        self.execute("flat", 1)
        row = run.trials(self.exp)[1]
        self.assertEqual((row["status"], row["failed_stage"]), ("invalid", "validate"))
        self.assertEqual(run.load(self.exp / "run.json")["status"], "limit_reached")

    def test_linked_analysis_reaches_generation_and_the_judge_sees_where_it_came_from(self):
        self.execute("new-section", 1)
        self.assertEqual(run.trials(self.exp)[1]["status"], "qualified")
        self.assertIn("sections/局限.md", run.read(self.exp / "round-001/总纲.md"))
        self.assertNotIn("证据限度", run.read(self.exp / "inputs/来源依据.md"))
        self.assertFalse((self.repo / "analyses/遮天/sections/局限.md").exists())
        self.assertEqual(run.read(self.exp / "round-000/引用依据.md").count(" · 冻结材料已有"), 3)
        cited = run.read(self.exp / "round-001/引用依据.md")
        self.assertIn("analyses/遮天/sections/局限.md · 冻结材料没有", cited)
        self.assertIn("古史残缺时的证据限度。", cited)
        self.assertEqual(cited.count(" · 冻结材料已有"), 2)

    def test_a_commit_is_given_the_same_prompt_as_candidate_and_as_baseline(self):
        self.execute("new-section", 1)
        candidate = run.trials(self.exp)[1]["commit"]
        self.execute("flat", 0, identifier="replay", base=candidate)
        baseline, in_loop, replayed = self.prompts.read_text().splitlines()
        self.assertNotEqual(baseline, in_loop)
        self.assertEqual(in_loop, replayed)
        self.assertIn("已传入新规", run.read(run.run_path("replay") / "round-000/总纲.md"))
        book = self.repo / "novels_version/留灯"
        self.assertIn("初稿", run.read(book / "round-000-fixture/总纲.md"))
        self.assertIn("已传入新规", run.read(book / "round-000-replay/总纲.md"))

    def test_archive_backfills_old_runs_and_preserves_reader_edits(self):
        self.execute("pass", 0)
        rec = run.trials(self.exp)[0]
        directory = self.exp / "round-000"
        version = self.repo / rec.pop("novel_version")
        shutil.rmtree(version)
        run.save(directory / "round.json", rec)
        calls = self.events.read_text()
        original = (directory / "总纲.md").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            run.archive(self.exp)
            self.assertEqual((version / "总纲.md").read_bytes(), original)
            run.write(version / "总纲.md", "作者的改稿。")
            (version / "分卷.md").unlink()
            run.archive(self.exp)
        self.assertEqual(run.read(version / "总纲.md"), "作者的改稿。")
        self.assertEqual((version / "分卷.md").read_bytes(), (directory / "分卷.md").read_bytes())
        self.assertEqual((directory / "总纲.md").read_bytes(), original)
        self.assertTrue(run.verified_score(directory, rec)["passed"])
        self.assertEqual(self.events.read_text(), calls)

    def test_archive_stays_under_project_for_unusual_titles_and_symlinks(self):
        for title in ("../别处/书", ".git", "山路" * 100):
            with self.subTest(title=title):
                name = run.book_folder(title)
                self.assertEqual(Path(name).parts, (name,))
                self.assertFalse(name.startswith("."))
                self.assertLessEqual(len(name.encode()), 255)
        (self.repo / "novels_version").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "归档路径"):
            self.execute("pass", 0)
        self.assertFalse((self.root / "留灯").exists())

    def test_unlinked_new_note_is_refused(self):
        with self.assertRaisesRegex(ValueError, "链接"):
            self.execute("orphan", 1)
        self.assertEqual(run.trials(self.exp)[1]["failed_stage"], "optimize")
        self.assertEqual(self.events.read_text().splitlines().count("optimize"), 2)

    def test_removed_worktree_folder_is_checked_out_again(self):
        with self.assertRaisesRegex(ValueError, "引文"):
            self.execute("bad-quote", 1)
        shutil.rmtree(run.TREES / "fixture")
        self.resume("improve")
        self.assertEqual([r["status"] for r in run.trials(self.exp)], ["baseline", "qualified"])

    def test_select_detects_changes_to_the_scored_document(self):
        self.execute("pass", 0)
        run.write(self.exp / "round-000/总纲.md", "后来手改的一版。")
        with self.assertRaisesRegex(ValueError, "被评分"):
            run.choose(self.exp, 0, "")

    def test_edit_scope_and_safe_resumption_after_commit(self):
        tree = self.root / "candidate"
        wt.open_tree(self.repo, tree, "codex/test", self.base)
        books = {"遮天"}
        def proposal(*edits):
            return dict(diagnosis="来源证据不足", hypothesis="回查范围",
                        edits=[dict(path=p, before=b, after=x) for p, b, x in edits])
        refused = [("../rubric.json", "", "data"),
                   ("analyses/遮天/notes/segment-001.md", "原始阅读笔记。", "改写的笔记。"),
                   ("analyses/遮天/analysis-state.yaml", "coverage: full", "coverage: partial"),
                   ("analyses/张三丰异界游/pattern-cards.md", "用选择和后果来验证。", "任务之外的书。"),
                   ("references/简报 新约定.md", "", "没有文件链接到它")]
        for edit in refused:
            with self.subTest(path=edit[0]), self.assertRaises(ValueError):
                wt.commit_proposal(tree, self.base, "proposal", proposal(edit), books)
        self.assertEqual(wt.git(tree, "status", "--porcelain"), "")
        good = proposal(("references/简报 新约定.md", "", "中文新文件，无末尾换行"),
                        ("SKILL.md", "初始流程", "初始流程\n见 [新约定](references/简报 新约定.md)。"),
                        ("analyses/遮天/pattern-cards.md", "用选择和后果来验证。", "用选择、后果和反例来验证。"),
                        ("tests/test_new.py", "", "import unittest\n"))
        rev = wt.commit_proposal(tree, self.base, "proposal", good, books)
        self.assertEqual(wt.commit_proposal(tree, self.base, "proposal", good, books), rev)
        with self.assertRaisesRegex(ValueError, "测试"):
            wt.patch_for(tree, rev, proposal(("tests/test_new.py", "import unittest", "pass")), books)

    def test_score_anchors_and_individual_thresholds(self):
        docs = {"总纲.md": "角色有选择\n后来承担了代价"}
        def cite(*quotes):
            return [dict(file="总纲.md", quote=q) for q in quotes]
        item = dict(id="x", score=79, evidence=cite("有选择"), reason="卷末没有后果", next_fix="补后果")
        rubric = dict(scale=[0, 100], dimensions=[dict(id="x", minimum=80)])
        for score in (0, 39, 40, 59, 60, 79, 80, 86, 89, 90, 100):
            with self.subTest(score=score):
                result = a.assess({"ratings": [dict(item, score=score)]}, rubric, docs)
                self.assertEqual(result["passed"], score >= 80)
                self.assertEqual(result["mean"], score)
        for score in (-1, 101, 80.5, True, "80"):
            with self.subTest(score=score), self.assertRaisesRegex(ValueError, "0–100"):
                a.assess({"ratings": [dict(item, score=score)]}, rubric, docs)
        for bad in ([item, item], [dict(item, next_fix="")], [], [dict(item, evidence=cite("伪证"))],
                    [dict(item, evidence=[])], [dict(item, file="总纲.md", quote="有选择", evidence=None)]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                a.assess({"ratings": bad}, rubric, docs)
        paired = dict(scale=[0, 100], dimensions=[dict(id="x", minimum=80, evidence=2)])
        for quotes in (("有选择",), ("角色", "有选择")):
            with self.subTest(quotes=quotes), self.assertRaisesRegex(ValueError, "不同行"):
                a.assess({"ratings": [dict(item, evidence=cite(*quotes))]}, paired, docs)
        rating = a.assess({"ratings": [dict(item, evidence=cite("有选择", "承担了代价"))]}, paired, docs)["ratings"]["x"]
        self.assertEqual([e["line"] for e in rating["evidence"]], [1, 2])

    def test_lowest_reading_decides_and_only_closeness_to_thresholds_ranks(self):
        def reading(*scores):
            return a.summary({f"d{i}": dict(score=s, minimum=80, evidence=[], reason=str(s), next_fix="")
                              for i, s in enumerate(scores)})
        merged = a.combine([reading(84, 79, 100), reading(78, 85, 100)])
        self.assertEqual([r["score"] for r in merged["ratings"].values()], [78, 79, 100])
        self.assertEqual(merged["ratings"]["d0"]["reason"], "78")
        self.assertEqual([r["spread"] for r in merged["ratings"].values()], [6, 6, 0])
        self.assertFalse(merged["passed"])
        # One far miss is not closer to passing than two near misses.
        self.assertGreater(reading(79, 79, 90)["rank"], reading(40, 80, 90)["rank"])
        # Points above the thresholds are not an improvement.
        self.assertEqual(reading(79, 100, 100)["rank"], reading(79, 81, 80)["rank"])
        self.assertGreater(reading(80, 80, 80)["rank"], reading(79, 100, 100)["rank"])

    def test_rubric_validates_percentage_thresholds_and_anchor_floors(self):
        a.validate_inputs(self.case, run.load(run.HERE / "rubric.json"))
        self.assertEqual([d["id"] for d in self.rubric["dimensions"] if d.get("evidence") == 2],
                         ["engine", "payoff", "volumes"])
        dimension = self.rubric["dimensions"][0]
        for minimum in (0, 87, 100):
            rubric = dict(self.rubric, dimensions=[dict(dimension, minimum=minimum)])
            a.validate_inputs(self.case, rubric)
        for minimum in (-1, 101, False, 80.5):
            rubric = dict(self.rubric, dimensions=[dict(dimension, minimum=minimum)])
            with self.subTest(minimum=minimum), self.assertRaisesRegex(ValueError, "达标线"):
                a.validate_inputs(self.case, rubric)
        for count in (0, "2", 1.5):
            rubric = dict(self.rubric, dimensions=[dict(dimension, evidence=count)])
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "引文处数"):
                a.validate_inputs(self.case, rubric)
        for floors in ([1, 40, 60, 80, 90], [0, 40, 80, 80, 90], [0, 40, 60, 80, 101]):
            with self.subTest(floors=floors), self.assertRaisesRegex(ValueError, "五档起分"):
                a.validate_inputs(self.case, dict(self.rubric, anchor_floors=floors))

    def test_frozen_rubric_keeps_its_original_scale(self):
        legacy = dict(scale=[1, 2, 3, 4, 5], dimensions=[dict(id="x", minimum=4)])
        docs = {"分卷.md": "取得通行权，欠下人情。"}
        item = dict(id="x", score=4, evidence=[dict(file="分卷.md", quote="欠下人情")], reason="卷末留下新的责任")
        self.assertTrue(a.assess({"ratings": [item]}, legacy, docs)["passed"])
        with self.assertRaisesRegex(ValueError, "1–5"):
            a.assess({"ratings": [dict(item, score=80)]}, legacy, docs)


if __name__ == "__main__":
    unittest.main()
