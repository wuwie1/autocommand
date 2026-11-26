#!/usr/bin/env python3
"""Run bounded, independently judged experiments on the ani-book-skill planning pipeline."""

import argparse
import csv
import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import assessment as a
import providers
import worktrees as wt


HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
TREES = HERE / ".worktrees"
DEFAULT_PROJECT = HERE.parent.parent / "ani-book-skill"
INPUT_FILES = ("case.json", "rubric.json", "generate.md", "optimize.md", "judge.md", "来源依据.md")
FLOW = ("SKILL.md", "references/story-and-volume-planning.md", "references/analysis-to-novel.md", "references/style-engine.md")
COLUMNS = ("run", "round", "parent", "commit", "branch", "rubric", "judge", "dimension", "name",
           "score", "minimum", "mean", "status", "evidence", "reason", "next_fix", "scores", "spread")
SCORED = {"baseline", "improved", "retained", "qualified"}
CITED = "引用依据.md"
LEGEND = ("结果一栏的含义：`baseline` 是原流程。`improved` 比它的起点更接近达标，后续从它出发。"
          "`retained` 没有超过起点，后续仍从起点出发。`qualified` 全部达标。"
          "`invalid` 未通过目标项目验证，没有评分。`error` 运行出错，可用 `resume` 接续。")


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read(path):
    return path.read_text(encoding="utf-8")


def load(path):
    return json.loads(read(path))


def save(path, data):
    write(path, a.dump(data))


@contextmanager
def lock(path, wait=False):
    with path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise ValueError("这一实验已有进程在运行") from error
        yield


def run_path(identifier):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier):
        raise ValueError("实验编号使用 1–64 个英文字母、数字、横线或下划线")
    return RESULTS / identifier


def inputs(args):
    repo = args.project.resolve()
    base = wt.git(repo, "rev-parse", "--verify", "--end-of-options", args.base + "^{commit}").strip()
    case, rubric = load(args.case), load(args.rubric)
    a.validate_inputs(case, rubric)
    def source(path):
        return wt.git(repo, "show", f"{base}:{path}")
    for path in FLOW:
        source(path)
    _, excerpts = a.materials(case, source)
    frozen = {"case.json": a.dump(case), "rubric.json": a.dump(rubric), "来源依据.md": excerpts}
    for name in ("generate", "optimize", "judge"):
        frozen[name + ".md"] = read(HERE / "prompts" / (name + ".md"))
    return repo, base, frozen


def tools():
    found = {cmd: shutil.which(cmd) for cmd in ("git", "codex", "agent")}
    missing = [cmd for cmd, path in found.items() if not path]
    return found, missing


def control_digest():
    return a.digest("".join(read(HERE / name) for name in ("run.py", "assessment.py", "providers.py", "worktrees.py")))


def start(args):
    if args.max_optimizations < 0 or args.timeout < 1 or args.judgments < 1:
        raise ValueError("优化上限不得为负数，调用超时和每版评分次数须为正数")
    root = run_path(args.id)
    if root.exists():
        raise ValueError(f"编号已存在，使用 resume {args.id} 接续")
    missing = tools()[1]
    if missing:
        raise ValueError("以下 CLI 不在 PATH，实验尚未建立：" + "、".join(missing))
    repo, base, frozen = inputs(args)
    generator = providers.generator(args.generator_model, args.timeout)
    RESULTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RESULTS, prefix=".init-") as stage:
        staging = Path(stage) / "new"
        for name, text in frozen.items():
            write(staging / "inputs" / name, text)
        manifest = dict(id=args.id, project=str(repo), base=base, max_optimizations=args.max_optimizations,
                        timeout=args.timeout, judgments=args.judgments, generator=generator, judge_model=None,
                        control=[], status="ready", fingerprints={k: a.digest(v) for k, v in frozen.items()})
        save(staging / "run.json", manifest)
        staging.rename(root)
    return resume(root)


def trials(root):
    return [load(p) for p in sorted(root.glob("round-*/round.json"), key=lambda p: int(p.parent.name[6:]))]


def rdir(root, n):
    return root / f"round-{n:03d}"


def verified_score(directory, rec):
    raw = read(directory / "score.json")
    if a.digest(raw) != rec["score_sha256"]:
        raise ValueError("评分记录自上次校验以来发生了改变")
    score = json.loads(raw)
    if any(a.digest(read(directory / name)) != score["documents_sha256"][name] for name in a.PRODUCTS):
        raise ValueError("候选作品与实际被评分的版本不一致")
    return score


def publish(root, manifest, rubric):
    max_score = rubric["scale"][-1]
    generator = manifest["generator"]
    best = manifest.get("best_round")
    rows, lines = [], [f"# {manifest['id']} · 候选版本", "", f"状态 `{manifest['status']}`。"
                       f"基线 `{manifest['base']}`，最多优化 {manifest['max_optimizations']} 次。"
                       + (f"自动比较中最接近达标的是第 {best} 轮。" if best is not None else ""), "",
                       f"生成 `{generator['model'] or 'Codex 默认模型'}`"
                       + (f"（推理强度 {generator['effort']}）" if generator["effort"] else "")
                       + f"，裁判 `{manifest.get('judge_model') or '未解析'}`，每版独立评分 {manifest['judgments']} 次。"
                       "每项取各次评分的低值，分差是同一项各次评分的最大差距。", "",
                       f"| 轮次 | 起点 | 源码提交 | 结果 | 均分（满分 {max_score}） | 未达标 | 最大分差 | 可读工件 |",
                       "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    names = {d["id"]: d["name"] for d in rubric["dimensions"]}
    for rec in trials(root):
        n = rec["round"]
        directory = rdir(root, n)
        rel = directory.name
        status = rec["status"]
        base = dict(run=manifest["id"], round=n, parent=rec["parent"], commit=rec.get("commit", ""),
                    branch=rec["branch"], rubric=rubric["version"], judge=manifest.get("judge_model") or "", status=status)
        score = verified_score(directory, rec) if status in SCORED else None
        if score:
            for key, item in score["ratings"].items():
                cited = "；".join(f"{e['file']}:{e['line']} {e['quote']}" for e in item["evidence"])
                rows.append(dict(**base, dimension=key, name=names[key], score=item["score"], minimum=item["minimum"],
                                 mean=score["mean"], evidence=cited, reason=item["reason"], next_fix=item["next_fix"],
                                 scores="/".join(map(str, item["scores"])), spread=item["spread"]))
        elif status in {"error", "invalid"}:
            rows.append(dict(**base, reason=rec.get("error", "")))
        docs = [f"[{f}]({rel}/{f})" for f in (*a.PRODUCTS, CITED) if (directory / f).is_file()]
        if score:
            docs.append(f"[逐项评分]({rel}/score.json)")
        if (directory / "proposal.json").is_file():
            docs.append(f"[优化诊断]({rel}/proposal.json)")
        gaps = "、".join(names[k] for k, v in score["ratings"].items() if v["score"] < v["minimum"]) if score else ""
        spread = "—"
        if score:
            key, item = max(score["ratings"].items(), key=lambda pair: pair[1]["spread"])
            spread = f"{item['spread']}（{names[key]}）"
        origin = "—" if rec.get("parent_round") is None else f"第 {rec['parent_round']} 轮"
        lines.append(f"| {n} | {origin} | `{base['commit'][:12]}` | {status} | {score['mean'] if score else '—'} |"
                     f" {gaps or '—'} | {spread} | {' · '.join(docs)} |")
    lines.extend(["", LEGEND])
    if manifest.get("selections"):
        last = manifest["selections"][-1]
        lines.extend(["", f"人工选用第 {last['round']} 轮，提交 `{last['commit']}`。"])
    write(root / "候选对照.md", "\n".join(lines) + "\n")
    ledger = RESULTS / "results.tsv"
    with lock(RESULTS / ".ledger.lock", wait=True):
        existing = list(csv.DictReader(io.StringIO(read(ledger)), delimiter="\t")) if ledger.exists() else []
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(r for r in existing if r["run"] != manifest["id"])
        writer.writerows(rows)
        write(ledger, out.getvalue())


def brief_docs(documents):
    return "\n\n".join(f"# 文件 {file}\n\n{text}" for file, text in documents.items())


def candidate(root, m, fixed, rec, directory, tree):
    n, parent = rec["round"], rec["parent"]
    if not n:
        wt.clean(tree)
        if wt.git(tree, "rev-parse", "HEAD").strip() != parent:
            raise ValueError("基线工作树偏离冻结提交")
        return parent
    books = {r["book"] for r in json.loads(fixed["case.json"])["references"]}
    previous = rdir(root, rec["parent_round"])
    proposal_file = directory / "proposal.json"
    marker = f"experiment {m['id']} round {n}"
    if not proposal_file.exists():
        wt.clean(tree)
        if wt.git(tree, "rev-parse", "HEAD").strip() != parent:
            raise ValueError("本轮起点与登记的父提交不符")
        history = []
        for item in trials(root):
            path = rdir(root, item["round"]) / "proposal.json"
            if item["round"] < n and path.is_file():
                history.append([item["round"], item["status"], load(path)["hypothesis"]])
        prompt = "\n\n".join([fixed["optimize.md"], fixed["case.json"], read(previous / "score.json"),
                              brief_docs({k: read(previous / k) for k in a.PRODUCTS}), "已试改动 " + a.dump(history)])
        for attempt in range(2):
            proposal = providers.codex(tree, prompt, a.PROPOSAL, m["generator"], m["timeout"])
            try:
                wt.patch_for(tree, parent, proposal, books)
                break
            except (KeyError, TypeError, ValueError) as error:
                if attempt:
                    raise
                prompt += "\n上一份提议不能应用，请读该版本原文后修正：" + str(error)
        save(proposal_file, proposal)
    return wt.commit_proposal(tree, parent, marker, load(proposal_file), books)


def cited_sources(tree, base, cites):
    """Show the judge each quoted passage and whether the frozen baseline already contained it."""
    lines = ["# 本版规划引用的拆书原文", "",
             "程序已核对下列引文出自本版规划所用的拆书材料。“冻结材料已有”表示这句话在评判基准的拆书材料中原样存在。"
             "“冻结材料没有”表示它由规划方后来补充，尚未对照原书笔记核实。"]
    for cite in cites:
        try:
            frozen = any(cite["anchor"] in line for line in wt.git(tree, "show", f"{base}:{cite['path']}").splitlines())
        except RuntimeError:
            frozen = False
        lines.extend(["", f"## {cite['book']} · {cite['role']} · {cite['path']} · {'冻结材料已有' if frozen else '冻结材料没有'}",
                      f"引文：{cite['anchor']}", "", cite["section"]])
    return "\n".join(lines) + "\n"


def generate(tree, directory, m, fixed, case):
    # Every round is shown the same fixed list, read from its own tree. Anything else reaches the
    # planner the way it does in real use, through the links in the Skill and the analysis files.
    readings, _ = a.materials(case, lambda name: read(tree / name), freeze_excerpts=False)
    flow = {name: read(tree / name) for name in FLOW}
    books = {ref["book"] for ref in case["references"]}

    def source(name):
        if name in readings:
            return readings[name]
        try:
            return wt.git(tree, "show", "HEAD:" + name) if wt.citable(name, books) else None
        except RuntimeError:
            return None

    def render(plan):
        docs, cites = a.render_plan(plan, case, source)
        return docs, cited_sources(tree, m["base"], cites)

    path = directory / "plan.json"
    if path.exists():
        return render(load(path))
    prompt = "\n\n".join([fixed["generate.md"], fixed["case.json"], "本版创作流程", brief_docs(flow),
                          "本版拆书依据", brief_docs(readings)])
    for attempt in range(2):
        plan = providers.codex(tree, prompt, a.PLAN, m["generator"], m["timeout"])
        try:
            result = render(plan)
            save(path, plan)
            return result
        except (KeyError, TypeError, ValueError) as error:
            if attempt:
                raise
            prompt += "\n修正上次交付的格式或引用：" + str(error)


def judgment(directory, index, m, fixed, rubric, docs, packet):
    """One independent reading. Every raw answer is kept, including those that fail the checks."""
    prompt = fixed["judge.md"]
    raw = directory / "judge-raw"
    for attempt in range(2):
        try:
            body, identity = providers.cursor(packet, prompt, m["judge_model"], m["timeout"])
            taken = len(list(raw.glob(f"{index:02d}-*.txt"))) if raw.is_dir() else 0
            write(raw / f"{index:02d}-{taken + 1:02d}.txt", body if isinstance(body, str) else a.dump(body))
            result = a.assess(providers.parse(body), rubric, docs)
            result.update(judge=identity)
            return result
        except (ValueError, KeyError, TypeError) as error:
            if attempt:
                raise
            prompt += "\n请重新给出完整 JSON，并核对原文。上一份返回未通过校验：" + str(error)


def judge(directory, m, fixed, rubric, docs, citations):
    packet = "\n\n".join([fixed["case.json"], fixed["来源依据.md"], citations, fixed["rubric.json"], brief_docs(docs)])
    path = directory / "score.json"
    fingerprint = a.digest(packet)
    if path.is_file():
        stored = load(path)
        if stored["input_sha256"] != fingerprint or stored["judge"]["cli_model"] != m["judge_model"]:
            raise ValueError("现存评分与材料或裁判版本不符")
        return stored
    readings = []
    for index in range(1, m["judgments"] + 1):
        part = directory / f"judgment-{index}.json"
        if part.is_file():
            item = load(part)
            if item["input_sha256"] != fingerprint or item["judge"]["cli_model"] != m["judge_model"]:
                raise ValueError(f"现存的第 {index} 次评分与材料或裁判版本不符")
        else:
            item = judgment(directory, index, m, fixed, rubric, docs, packet)
            item.update(input_sha256=fingerprint)
            save(part, item)
        readings.append(item)
    result = a.combine(readings)
    result.update(judge={k: readings[0]["judge"][k] for k in ("requested", "cli_model", "reported_model")},
                  rubric=rubric["version"], judgments=len(readings), input_sha256=fingerprint,
                  documents_sha256={name: a.digest(text) for name, text in docs.items()})
    save(path, result)
    return result


def execute(root, m, fixed, rubric):
    repo = Path(m["project"])
    case = json.loads(fixed["case.json"])
    best = None
    for n in range(m["max_optimizations"] + 1):
        directory = rdir(root, n)
        parent = best["commit"] if best else m["base"]
        branch = f"codex/ani-eval/{m['id']}/r{n:03d}"
        tree = TREES / m["id"] / directory.name
        recfile = directory / "round.json"
        rec = load(recfile) if recfile.exists() else dict(round=n, parent=parent, branch=branch, worktree=str(tree),
                                                           parent_round=best["round"] if best else None, status="pending")
        if rec["parent"] != parent or rec["branch"] != branch:
            raise ValueError("本轮起点与先前记录不一致")
        if rec["status"] == "invalid":
            # This failed source version remains available. Retry the next idea from the incumbent.
            if n == m["max_optimizations"]:
                m["status"] = "limit_reached"
                save(root / "run.json", m)
                publish(root, m, rubric)
                return
            continue
        save(recfile, rec)
        stage = "worktree"
        try:
            wt.open_tree(repo, tree, branch, parent)
            stage = "optimize" if n else "baseline"
            rev = candidate(root, m, fixed, rec, directory, tree)
            if rec.get("commit", rev) != rev:
                raise ValueError("候选提交与记录不一致")
            rec["commit"] = rev
            save(recfile, rec)
            print(f"第 {n} 轮 · {rev[:12]} · {tree}", flush=True)
            if n and rec.get("validated") != rev:
                stage = "validate"
                wt.validate_candidate(repo, tree, directory / "validation.log", m["timeout"])
                rec["validated"] = rev
                save(recfile, rec)
            stage = "generate"
            docs, citations = generate(tree, directory, m, fixed, case)
            for name, text in {**docs, CITED: citations}.items():
                target = directory / name
                if target.exists() and read(target) != text:
                    raise ValueError(f"实验产物有人工变动，保留现场 {target}")
                if not target.exists():
                    write(target, text)
            stage = "judge"
            score = judge(directory, m, fixed, rubric, docs, citations)
            score_digest = a.digest(read(directory / "score.json"))
            if rec.get("score_sha256", score_digest) != score_digest:
                raise ValueError("评分记录自上次校验以来发生了改变")
            improved = best is None or score["rank"] > best["rank"]
            if score["passed"]:
                rec["status"] = "qualified"
            elif n == 0:
                rec["status"] = "baseline"
            else:
                rec["status"] = "improved" if improved else "retained"
            rec.update(mean=score["mean"], rank=score["rank"], score_sha256=score_digest)
            rec.setdefault("control", m["control"][-1])
            rec.pop("error", None)
            rec.pop("failed_stage", None)
            save(recfile, rec)
            if improved:
                best = rec
            m["best_round"] = best["round"]
            if score["passed"] or n == m["max_optimizations"]:
                m["status"] = "qualified" if score["passed"] else "limit_reached"
            save(root / "run.json", m)
            publish(root, m, rubric)
            if m["status"] in {"qualified", "limit_reached"}:
                print(f"{m['status']} · {root / '候选对照.md'}")
                return
        except wt.CandidateRejected as error:
            rec.update(status="invalid", failed_stage=stage, error=str(error))
            save(recfile, rec)
            if n == m["max_optimizations"]:
                m["status"] = "limit_reached"
            save(root / "run.json", m)
            publish(root, m, rubric)
            if m["status"] == "limit_reached":
                print(f"limit_reached · {root / '候选对照.md'}")
                return
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            rec.update(status="error", failed_stage=stage, error=str(error))
            save(recfile, rec)
            raise


def resume(root, timeout=None):
    if not (root / "run.json").is_file():
        raise ValueError(f"没有此实验 {root.name}")
    if timeout is not None and timeout < 1:
        raise ValueError("调用超时须为正数")
    with lock(root / ".run.lock"):
        m = load(root / "run.json")
        fixed = {name: read(root / "inputs" / name) for name in INPUT_FILES}
        if m["fingerprints"] != {k: a.digest(v) for k, v in fixed.items()}:
            raise ValueError("冻结任务、量表或角色提示词已经改变，请另开实验")
        rubric = json.loads(fixed["rubric.json"])
        if m["status"] in {"qualified", "limit_reached"}:
            publish(root, m, rubric)
            print(f"{m['status']} · {root / '候选对照.md'}")
            return
        try:
            if timeout:
                m["timeout"] = timeout
            # A repaired control program is allowed between attempts. The record shows which one judged each round.
            if control_digest() not in m["control"][-1:]:
                m["control"].append(control_digest())
            if not m["judge_model"]:
                m["judge_model"] = providers.judge_model(m["timeout"])
            m["status"] = "running"
            m.pop("error", None)
            save(root / "run.json", m)
            execute(root, m, fixed, rubric)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            m.update(status="error", error=str(error))
            save(root / "run.json", m)
            publish(root, m, rubric)
            raise


def choose(root, n, note):
    with lock(root / ".run.lock"):
        m = load(root / "run.json")
        rec = load(rdir(root, n) / "round.json")
        if rec["status"] not in SCORED:
            raise ValueError("只能选择已完成评分的候选")
        verified_score(rdir(root, n), rec)
        selected = dict(round=n, commit=rec["commit"], branch=rec["branch"], note=note,
                        selected_at=datetime.now().astimezone().isoformat())
        m.setdefault("selections", []).append(selected)
        save(root / "run.json", m)
        publish(root, m, load(root / "inputs/rubric.json"))
        print(a.dump(selected), end="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="离线核对来源和工具位置")
    run = sub.add_parser("run", help="启动真实 Codex / Cursor 实验")
    for p in (run, check):
        p.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
        p.add_argument("--base", default="HEAD")
        p.add_argument("--case", type=Path, default=HERE / "cases/三书玄幻.json")
        p.add_argument("--rubric", type=Path, default=HERE / "rubric.json")
    run.add_argument("--id", default=datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    run.add_argument("--max-optimizations", type=int, default=2)
    run.add_argument("--judgments", type=int, default=2, help="每个版本独立评分的次数，各项取低值")
    run.add_argument("--generator-model", help="Codex 模型 ID，默认取本机 Codex 配置并记入实验")
    run.add_argument("--timeout", type=int, default=900, help="单次模型或验证命令超时秒数")
    again = sub.add_parser("resume", help="从已持久化的阶段接续")
    again.add_argument("id")
    again.add_argument("--timeout", type=int, help="改用新的单次调用超时秒数")
    select = sub.add_parser("select", help="记录人工选择的候选版本")
    select.add_argument("id")
    select.add_argument("round", type=int)
    select.add_argument("--note", default="")
    args = parser.parse_args()
    if args.command == "check":
        repo, base, fixed = inputs(args)
        cli, missing = tools()
        print(a.dump(dict(project=str(repo), base=base, case=json.loads(fixed["case.json"])["name"],
                          rubric=json.loads(fixed["rubric.json"])["version"], tools=cli)), end="")
        if missing:
            raise ValueError("以下 CLI 不在 PATH：" + "、".join(missing))
    elif args.command == "run":
        start(args)
    elif args.command == "resume":
        resume(run_path(args.id), args.timeout)
    else:
        choose(run_path(args.id), args.round, args.note)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"实验未完成：{exc}", file=sys.stderr)
        sys.exit(1)
