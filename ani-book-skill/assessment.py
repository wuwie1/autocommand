"""Experiment input contracts, planning artifacts, and independent score validation."""

import hashlib
import json
import re
from pathlib import PurePosixPath


def obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def array(items):
    return {"type": "array", "items": items}


TEXT = {"type": "string"}
PLAN = obj(title=TEXT, synopsis=TEXT, volumes=array(obj(title=TEXT, wan={"type": "integer"}, plan=TEXT)),
           opening=array(obj(title=TEXT, plan=TEXT)), transfers=array(obj(book=TEXT, file=TEXT, anchor=TEXT, use=TEXT)))
PROPOSAL = obj(diagnosis=TEXT, hypothesis=TEXT, edits=array(obj(path=TEXT, before=TEXT, after=TEXT)))
PRODUCTS = ("总纲.md", "分卷.md", "开篇章纲.md")


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\0\r\n\t\\"):
        raise ValueError(f"无效相对路径 {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(p in {".", "..", ".git"} for p in value.split("/")):
        raise ValueError(f"无效相对路径 {value!r}")
    return value


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 应为非空文本")
    return value.strip()


def validate_inputs(case, rubric):
    for key in ("name", "reader", "task", "seed"):
        require_text(case.get(key), key)
    for key in ("target_wan", "tolerance_wan", "opening_chapters"):
        if type(case.get(key)) is not int or case[key] < (0 if key == "tolerance_wan" else 1):
            raise ValueError(f"无效的 {key}")
    if case["tolerance_wan"] >= case["target_wan"]:
        raise ValueError("篇幅浮动应小于目标篇幅")
    books = set()
    for ref in case["references"]:
        book = relative(ref["book"])
        if "/" in book or book in books or not ref["files"]:
            raise ValueError(f"参考书重复或缺材料 {book}")
        for name in ref["files"]:
            relative(name)
        for key in ("role", "anchor", "lens"):
            require_text(ref.get(key), key)
        books.add(book)
    if not books:
        raise ValueError("缺少参考书")
    require_text(rubric.get("version"), "量表版本")
    scale = rubric.get("scale")
    if scale != [0, 100] or any(type(n) is not int for n in scale):
        raise ValueError("新实验的评分范围应为 [0, 100]")
    low, high = scale
    floors = rubric.get("anchor_floors")
    if (not isinstance(floors, list) or len(floors) != 5 or any(type(n) is not int for n in floors)
            or floors != sorted(set(floors)) or floors[0] != low or floors[-1] > high):
        raise ValueError("五档起分应从 0 开始，按整数递增，最高不超过 100")
    ids = set()
    for item in rubric["dimensions"]:
        key = item["id"]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in ids:
            raise ValueError(f"维度 ID 错误或重复 {key}")
        anchors = item.get("anchors")
        if not isinstance(anchors, list) or len(anchors) != len(floors):
            raise ValueError(f"{key} 的每个分段都需要一条评判标准")
        if type(item.get("minimum")) is not int or not low <= item["minimum"] <= high:
            raise ValueError(f"{key} 的达标线应为 {low}–{high} 整数")
        for text in [item["name"], item["question"], *anchors]:
            require_text(text, key)
        if type(item.get("evidence", 1)) is not int or item.get("evidence", 1) < 1:
            raise ValueError(f"{key} 的引文处数应为正整数")
        ids.add(key)
    if not ids:
        raise ValueError("量表至少需要一个维度")


def materials(case, read, *, freeze_excerpts=True):
    """Use the supplied Git revision reader for baselines, the candidate tree for generation."""
    readings, excerpts = {}, []
    for ref in case["references"]:
        prefix = f"analyses/{ref['book']}/"
        state = read(prefix + "analysis-state.yaml")
        coverage = read(prefix + "coverage-map.md")
        files = {name: read(prefix + name) for name in ref["files"]}
        readings.update({prefix + name: text for name, text in files.items()})
        if not freeze_excerpts:
            continue
        first = files[ref["files"][0]].splitlines()
        start = next((i for i, line in enumerate(first) if ref["anchor"] in line), None)
        if start is None:
            raise ValueError(f"{prefix}{ref['files'][0]} 缺少任务登记的锚点 {ref['anchor']}")
        stop = next((i for i in range(start + 1, len(first)) if first[i].startswith("## ")), len(first))
        scope = "\n".join(s for s in state.splitlines() if re.match(r"\s*(coverage|read_scope):", s))
        excerpts.append(f"## {ref['book']} · {ref['role']}\n{scope}\n覆盖图 SHA256 {digest(coverage)}\n"
                        f"{ref['files'][0]} / {ref['anchor']}\n" + "\n".join(first[start:stop]).strip())
    return readings, "\n\n".join(excerpts)


def section(text, anchor, limit=2400):
    """The heading block around a cited line, so the judge reads the quote in its own context."""
    lines = text.splitlines()
    at = next(i for i, line in enumerate(lines) if anchor in line)
    start = next((i for i in range(at, -1, -1) if lines[i].startswith("#")), 0)
    stop = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    body = "\n".join(lines[start:stop]).strip()
    if len(body) <= limit:
        return body
    head = lines[start] + "\n（本节较长，只列被引的一行）\n" if start != at else ""
    return head + lines[at][:limit]


def render_plan(plan, case, source):
    """source(path) returns one analysis file of the version being generated, or None."""
    title = require_text(plan["title"], "书名")
    synopsis = require_text(plan["synopsis"], "总纲")
    if not plan["volumes"] or len(plan["opening"]) != case["opening_chapters"]:
        raise ValueError("分卷缺失或开篇章数与任务不符")
    volumes, total = [], 0
    for i, v in enumerate(plan["volumes"], 1):
        if type(v["wan"]) is not int or v["wan"] <= 0:
            raise ValueError(f"第 {i} 卷篇幅无效")
        total += v["wan"]
        volumes.append(f"## 第{i}卷 {require_text(v['title'], '卷名')}（{v['wan']} 万字）\n\n"
                       + require_text(v["plan"], "卷纲"))
    if abs(total - case["target_wan"]) > case["tolerance_wan"]:
        raise ValueError(f"分卷合计 {total} 万字，未达到实验任务的篇幅范围")
    chapters = [f"## 第{i}章 {require_text(ch['title'], '章名')}\n\n{require_text(ch['plan'], '章纲')}"
                for i, ch in enumerate(plan["opening"], 1)]
    refs = {r["book"]: r for r in case["references"]}
    transfers, cites = [], []
    for item in plan["transfers"]:
        book = item["book"]
        ref = refs.pop(book, None)
        if not ref:
            raise ValueError(f"缺少或重复的来源登记 {book}")
        path = f"analyses/{book}/{relative(item['file'])}"
        text = source(path)
        if text is None:
            raise ValueError(f"本版拆书材料中没有可引用的 {path}")
        anchor = require_text(item["anchor"], "机制锚点")
        if not any(anchor in line for line in text.splitlines()):
            raise ValueError(f"{path} 找不到引文 {anchor}")
        transfers.append(f"- 《{book}》{ref['role']}：{require_text(item['use'], '原创落点')}（{path}，{anchor}）")
        cites.append(dict(book=book, role=ref["role"], path=path, anchor=anchor, section=section(text, anchor)))
    if refs:
        raise ValueError("缺少参考转化：" + "、".join(refs))
    return {PRODUCTS[0]: f"# {title}\n\n{synopsis}\n\n## 三书机制转化\n\n" + "\n".join(transfers) + "\n",
            PRODUCTS[1]: f"# {title} · 分卷（合计 {total} 万字）\n\n" + "\n\n".join(volumes) + "\n",
            PRODUCTS[2]: f"# {title} · 开篇章纲\n\n" + "\n\n".join(chapters) + "\n"}, cites


def summary(ratings):
    """Closeness to the thresholds decides the order. Points above a threshold do not."""
    deficits = [max(0, r["minimum"] - r["score"]) for r in ratings.values()]
    total = sum(r["score"] for r in ratings.values())
    return dict(ratings=ratings, passed=not any(deficits), mean=round(total / len(ratings), 3),
                rank=[-sum(deficits), -sum(n > 0 for n in deficits)])


def combine(judgments):
    """A dimension counts as its lowest independent reading, so one lucky judgment cannot pass it."""
    ratings = {}
    for key in judgments[0]["ratings"]:
        readings = [j["ratings"][key] for j in judgments]
        scores = [r["score"] for r in readings]
        ratings[key] = dict(min(readings, key=lambda r: r["score"]), scores=scores, spread=max(scores) - min(scores))
    return summary(ratings)


def assess(reply, rubric, documents):
    if not isinstance(reply, dict) or not isinstance(reply.get("ratings"), list):
        raise ValueError("裁判应返回 ratings 数组")
    low, high = rubric["scale"][0], rubric["scale"][-1]
    dims = {d["id"]: d for d in rubric["dimensions"]}
    ratings = {}
    for item in reply["ratings"]:
        if not isinstance(item, dict):
            raise ValueError("裁判分项应为对象")
        key = item.get("id")
        if not isinstance(key, str) or key not in dims or key in ratings:
            raise ValueError(f"未知或重复维度 {key}")
        score = item.get("score")
        if type(score) is not int or not low <= score <= high:
            raise ValueError(f"{key} 分数应为 {low}–{high} 整数")
        cited = item.get("evidence")
        if not isinstance(cited, list) or not cited:
            raise ValueError(f"{key} 应在 evidence 数组里给出引文")
        evidence = []
        for entry in cited:
            file = entry.get("file") if isinstance(entry, dict) else None
            quote = require_text(entry.get("quote") if isinstance(entry, dict) else None, key + " 引文")
            lines = documents.get(file, "").splitlines() if isinstance(file, str) else []
            line = next((n for n, s in enumerate(lines, 1) if quote in s), None)
            if line is None:
                raise ValueError(f"{key} 的引文未命中作品 {file}：{quote}")
            evidence.append(dict(file=file, line=line, quote=quote))
        need = dims[key].get("evidence", 1)
        if len({(e["file"], e["line"]) for e in evidence}) < need:
            raise ValueError(f"{key} 需要 {need} 处落在不同行的引文，分别指向前后两个卷章节点")
        minimum = dims[key]["minimum"]
        reason = require_text(item.get("reason"), key + " 理由")
        fix = item.get("next_fix", "")
        if not isinstance(fix, str) or (score < minimum and not fix.strip()):
            raise ValueError(f"{key} 低分需提供具体优化方向")
        ratings[key] = dict(score=score, minimum=minimum, evidence=evidence, reason=reason, next_fix=fix.strip())
    if dims.keys() != ratings.keys():
        raise ValueError("裁判缺少维度：" + "、".join(dims.keys() - ratings.keys()))
    return summary({key: ratings[key] for key in dims})
