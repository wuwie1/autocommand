"""Keep every planning pipeline candidate in its own local Git worktree."""

import difflib
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from assessment import relative, require_text


class CandidateRejected(ValueError):
    """The candidate was committed but failed the target project's checks."""


def git(repo, *args, data=None):
    proc = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          text=True, encoding="utf-8", check=False)
    if proc.returncode:
        raise RuntimeError(f"git {args[0]}：{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def open_tree(repo, tree, branch, parent):
    if not tree.exists():
        tree.parent.mkdir(parents=True, exist_ok=True)
        try:
            git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)
        except RuntimeError:
            git(repo, "worktree", "add", "-b", branch, str(tree), parent)
        else:
            # The folder was removed after the branch was made. Check the same branch out again.
            git(repo, "worktree", "prune")
            git(repo, "worktree", "add", str(tree), branch)
    if (not (tree / ".git").is_file() or git(tree, "branch", "--show-current").strip() != branch
            or git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
            != git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()):
        raise ValueError(f"worktree 与实验分支不符：{tree}")


def citable(path, books):
    """Analysis material of a task book that a plan may quote. Raw sources and indexes are excluded."""
    parts = PurePosixPath(relative(path)).parts
    return (len(parts) >= 3 and parts[0] == "analyses" and parts[1] in books
            and parts[2] not in {"source", "graph", "retrieval", "production", "历史抽样"}
            and Path(path).suffix in {".md", ".yaml", ".yml", ".json"})


def editable(path, books):
    """Conclusions and method may change. Reading notes, coverage and state stay as the evidence."""
    parts = PurePosixPath(relative(path)).parts
    if path == "SKILL.md":
        return True
    if parts[0] in {"references", "scripts", "tests", "agents", "assets"} and len(parts) > 1:
        return Path(path).suffix in {".md", ".py", ".yaml", ".yml", ".json", ".txt"}
    return citable(path, books) and (parts[2] == "sections" or parts[2:] in {("pattern-cards.md",), ("pattern-cards.yaml",)})


def patch_for(tree, parent, proposal, books):
    require_text(proposal["diagnosis"], "问题诊断")
    require_text(proposal["hypothesis"], "修订假设")
    original, revised = {}, {}
    for edit in proposal["edits"]:
        path, before, after = edit["path"], edit["before"], edit["after"]
        if not editable(path, books):
            raise ValueError(f"超出流程或拆书材料范围：{path}")
        target = tree / path
        if target.resolve() != target or not isinstance(after, str) or not isinstance(before, str):
            raise ValueError(f"无效文本编辑：{path}")
        if path not in original:
            record = git(tree, "ls-tree", "-z", parent, "--", path)
            if record and not record.startswith(("100644 blob ", "100755 blob ")):
                raise ValueError(f"需要普通文本文件：{path}")
            if record and path.startswith("tests/"):
                raise ValueError(f"现有测试保持原样，可新增测试文件：{path}")
            original[path] = git(tree, "show", f"{parent}:{path}") if record else None
            revised[path] = original[path]
        current = revised[path]
        if before:
            if current is None or current.count(before) != 1:
                raise ValueError(f"替换片段在 {path} 未唯一命中")
            revised[path] = current.replace(before, after, 1)
        elif current is None and after.strip():
            revised[path] = after
        else:
            raise ValueError(f"新增文件已存在或内容为空：{path}")
    patch = []
    for path, new in revised.items():
        old = original[path]
        if old == new:
            raise ValueError(f"没有实际变动：{path}")
        # Generation reads what the Skill routes to. An unlinked new note would never be read in real use.
        name = PurePosixPath(path).name
        if old is None and path.startswith(("references/", "analyses/")) and not any(
                name in text for other, text in revised.items() if other != path and original[other] is not None):
            raise ValueError(f"新建的 {path} 需在同一提议中由已有文件链接，生成时才读得到")
        lines = difflib.unified_diff((old or "").splitlines(True), new.splitlines(True),
                                     fromfile="/dev/null" if old is None else "a/" + path + "\t",
                                     tofile="b/" + path + "\t")
        for line in lines:
            patch.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    if not patch:
        raise ValueError("优化未产生可验证的流程变更")
    return "".join(patch), revised


def clean(tree):
    if git(tree, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError(f"实验 worktree 有未登记修改，保留现场：{tree}")


def commit_proposal(tree, parent, marker, proposal, books):
    patch, revised = patch_for(tree, parent, proposal, books)
    head = git(tree, "rev-parse", "HEAD").strip()
    if head != parent:
        # A previous attempt may have committed just before its receipt was saved.
        if git(tree, "show", "-s", "--format=%P%n%s", head).splitlines() != [parent, marker]:
            raise ValueError("候选分支 HEAD 偏离本轮父版本")
        changed = set(git(tree, "diff", "--name-only", "-z", parent, head).split("\0")) - {""}
        if changed != set(revised) or any(git(tree, "show", f"{head}:{p}") != v for p, v in revised.items()):
            raise ValueError("现有提交与已保存的修改建议不一致")
        clean(tree)
        return head
    if not git(tree, "status", "--porcelain", "--untracked-files=all"):
        git(tree, "apply", "--index", "--whitespace=nowarn", "-", data=patch)
    # Resume a fully staged patch, never pick up unrelated edits from a user.
    staged = set(git(tree, "diff", "--cached", "--name-only", "-z").split("\0")) - {""}
    if (staged != set(revised) or git(tree, "diff", "--name-only", "-z")
            or git(tree, "ls-files", "--others", "--exclude-standard", "-z")
            or any(git(tree, "show", ":" + p) != v for p, v in revised.items())):
        raise ValueError("暂存区或工作区与本轮补丁不一致，已保留现场")
    git(tree, "-c", "user.name=Codex Experiment", "-c", "user.email=experiment@localhost",
        "-c", "commit.gpgsign=false", "commit", "-m", marker)
    return git(tree, "rev-parse", "HEAD").strip()


def validate_candidate(repo, tree, log, timeout):
    python = repo / ".venv/bin/python"
    commands = []
    if (tree / "scripts").is_dir():
        commands.append(["-m", "compileall", "-q", "scripts"])
    if (tree / "tests").is_dir():
        commands.append(["-m", "unittest", "discover", "-s", "tests", "-v"])
    validator = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills/.system/skill-creator/scripts/quick_validate.py"
    if (tree / "SKILL.md").is_file() and validator.is_file():
        commands.append(["-X", "utf8", str(validator), "."])
    executable = str(python) if python.is_file() else sys.executable
    with tempfile.TemporaryDirectory() as cache, log.open("w", encoding="utf-8") as stream:
        env = dict(os.environ, PYTHONPYCACHEPREFIX=cache, PYTHONDONTWRITEBYTECODE="1")
        for args in commands:
            stream.write(" ".join(args) + "\n")
            stream.flush()
            proc = subprocess.run([executable, *args], cwd=tree, stdout=stream, stderr=subprocess.STDOUT,
                                  env=env, timeout=timeout, check=False)
            if proc.returncode:
                raise CandidateRejected(f"候选项目验证失败，见 {log}")
    if git(tree, "status", "--porcelain", "--untracked-files=all"):
        raise CandidateRejected(f"候选的验证在工作树留下了文件，保留现场 {tree}")
