"""CLI boundaries for read-only Codex design/diagnosis and a blind Cursor judge."""

import json
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path

from assessment import dump


FAMILY = "claude-opus-5-5"


def invoke(argv, cwd, timeout, data=None):
    with subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          start_new_session=True) as proc:
        try:
            out, err = proc.communicate(data, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        if proc.returncode:
            detail = (err or out).strip()[-1400:]
            raise RuntimeError(f"{argv[0]} 退出码 {proc.returncode}。{detail}")
        return out


def judge_model(timeout):
    listing = invoke(["agent", "--list-models"], None, timeout)
    # Match complete IDs. Account listings can include labels, ANSI colour and a selection marker.
    listing = re.sub(r"\x1b\[[0-9;]*m", "", listing)
    ids = set(re.findall(r"(?<![\w-])claude-opus-5-5[\w-]*(?![\w-])", listing))
    for name in (FAMILY, FAMILY + "-medium"):
        if name in ids:
            return name
    raise ValueError(f"Cursor 当前账户没有列出 {FAMILY} 或该系列的 medium 模型")


def generator(model, timeout):
    """Pin what Codex will run with, so every round and a later reader see the same generator."""
    settings = {}
    config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    if config.is_file():
        try:
            import tomllib
            settings = tomllib.loads(config.read_text(encoding="utf-8"))
        except (ImportError, ValueError):
            pass
    def text(key):
        return settings[key] if isinstance(settings.get(key), str) and settings[key] else None
    return dict(cli=invoke(["codex", "--version"], None, timeout).strip(), model=model or text("model"),
                effort=text("model_reasoning_effort"))


def codex(tree, prompt, schema, settings, timeout):
    with tempfile.TemporaryDirectory(prefix="ani-codex-") as tmp:
        schema_path, output = Path(tmp) / "schema.json", Path(tmp) / "reply.json"
        schema_path.write_text(dump(schema), encoding="utf-8")
        args = ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "--color", "never",
                "-C", str(tree), "--output-schema", str(schema_path), "-o", str(output)]
        if settings.get("model"):
            args.extend(["--model", settings["model"]])
        if settings.get("effort"):
            args.extend(["-c", f'model_reasoning_effort="{settings["effort"]}"'])
        invoke([*args, "-"], tree, timeout, prompt)
        return json.loads(output.read_text(encoding="utf-8"))


def cursor(packet, prompt, model, timeout):
    """Return the judge's raw answer. The caller keeps it on disk before parsing."""
    with tempfile.TemporaryDirectory(prefix="ani-editor-") as tmp:
        Path(tmp, "评判材料.md").write_text(packet, encoding="utf-8")
        # The folder holds only this packet. Headless runs stop at an untrusted workspace without --trust.
        args = ["agent", "--print", "--trust", "--mode", "ask", "--sandbox", "enabled", "--output-format", "json",
                "--model", model, "--workspace", tmp, prompt]
        out = invoke(args, tmp, timeout)
    try:
        envelope = json.loads(out)
    except ValueError as error:
        raise ValueError(f"Cursor 的输出不是 JSON：{out.strip()[:400]}") from error
    if (not isinstance(envelope, dict) or envelope.get("is_error")
            or envelope.get("subtype") not in (None, "success") or "result" not in envelope):
        raise ValueError("Cursor 没有返回成功的 result")
    # Current CLI builds report no model name. The check applies when one is present.
    reported = envelope.get("model")
    if isinstance(reported, str) and reported and not re.search(r"(?:claude[- ]+)?opus[- ]+5[.\-]5\b", reported, re.I):
        raise ValueError(f"Cursor 回报的模型与指定的 Opus 5.5 不符：{reported}")
    return envelope["result"], {"requested": FAMILY, "cli_model": model, "reported_model": reported,
                                "request_id": envelope.get("request_id")}


def parse(body):
    """Accept the JSON object alone, fenced, or with a sentence around it."""
    if not isinstance(body, str):
        return body
    fenced = re.search(r"```(?:json)?\s*\n(.*?)\n```", body, re.DOTALL | re.IGNORECASE)
    braces = body[body.find("{"):body.rfind("}") + 1]
    for text in (body, fenced[1] if fenced else "", braces):
        try:
            return json.loads(text)
        except ValueError:
            continue
    raise ValueError("裁判的回答里没有可解析的 JSON 对象")
