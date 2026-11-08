"""从文字里读意思：这一轮是不是在等你回答、撞没撞额度、话里点了哪个模型和深度、是不是一句顺口的回复。
都是纯函数，app（分派、开对话）和 web（列表标记、新开对话）共用；放这里免得 web 反过来 import app。"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from . import engines, report
from .engines import EFFORTS, MODELS


_REPLY = re.compile(r"(好的?|好吧|行|可以|对|是的?|嗯+|ok|OK|继续|接着|就这样|选?[A-Da-d1-9]|再.{0,6}一点|为什么|然后呢|还有呢)[。！!？?…，,]*")
_CHOICE = re.compile(r"(就|那就)?(按|选|用|要)?\s*(方案)?\s*[A-Da-d]\s*(方案)?\s*(来做|来|吧|就行|好了|可以)?[。！!…，,]*")
_OPTIONS = re.compile(r"^\s*(?:\|\s*)?(?:[-*]\s*)?(?:\*\*)?(?:方案|选项)?\s*[A-D]\s*(?:\*\*)?\s*[.．、:：)）]", re.M)


def asks_user(text: str) -> str:
    """这一轮最后是不是在等你回答（要你定、列了 A/B/C、或以问句收尾）；是就返回那句问话，给分派用。"""
    body = text.strip()
    if not body:
        return ""
    last = [ln.strip(" *#") for ln in body.splitlines() if ln.strip()][-1]
    if report.needs_decision(body) or len(_OPTIONS.findall(body)) >= 2 or last.endswith(("？", "?")):
        return report.brief(last if last.endswith(("？", "?")) else body, 40)
    return ""


_OFFER = re.compile(r"(要不要|需不需要|是否需要|要我|需要我|我可以|我也可以|可以帮你|我再|顺便|还要我|想让我|如果你愿意|如果需要)")
_APPROVE = "批准本会话编辑："


def awaiting_kind(text: str) -> str:
    """等你的轻重：decide = 真要你拍板（要你定、列了方案、要你批准编辑、直接问你）；
    suggest = 活做完了，最后顺口问一句“要不要我再…？”——不算卡住，列表里不标琥珀色。"""
    body = text.strip()
    if not body:
        return ""
    if report.needs_decision(body) or len(_OPTIONS.findall(body)) >= 2 or _APPROVE in body:
        return "decide"
    lines = [ln.strip(" *#") for ln in body.splitlines() if ln.strip()]
    if not lines[-1].endswith(("？", "?")):
        return ""
    return "suggest" if _OFFER.search(lines[-1]) else "decide"


def limit_reset(text: str, now: float | None = None) -> float | None:
    """“You've hit your session limit · resets 5:40pm (Asia/Taipei)” → 几点恢复（本机时间戳）；不是撞额度返回 None。"""
    if not re.search(r"hit your .{0,20}limit|usage limit|limit reached", text or "", re.I):
        return None
    m = re.search(r"resets\s+(?:(\w{3})\w*\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    now = now or time.time()
    if not m:
        return now + 5 * 3600
    mon, day, hh, mm, ap = m.groups()
    h = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    base = datetime.fromtimestamp(now)
    months = {x: i for i, x in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
    if mon and mon.lower()[:3] in months:
        at = base.replace(month=months[mon.lower()[:3]], day=int(day), hour=h, minute=int(mm or 0), second=0, microsecond=0)
        if at.timestamp() < now - 86400:
            at = at.replace(year=at.year + 1)
        return at.timestamp()
    at = base.replace(hour=h, minute=int(mm or 0), second=0, microsecond=0)
    return at.timestamp() + (86400 if at.timestamp() <= now else 0)


def inline_overrides(text: str) -> tuple[str | None, str | None]:
    """话里明说了模型或深度（“用 opus”“深度调到高”），就以你说的为准。"""
    model = effort = None
    if m := re.search(r"(?:用|换成|改用|切到|模型)\s*(opus|sonnet|haiku|fable)", text, re.I):
        model = MODELS[m.group(1).lower()]
    elif re.search(r"(?:用|交给|让)\s*(codex|gpt|chatgpt)", text, re.I):
        model = "gpt"
    elif re.search(r"(?:用|交给|让)\s*deepseek", text, re.I):
        model = "deepseek"
    elif (m := re.search(r"(?:用|换成|改用|切到)\s*(kimi-\S+)", text, re.I)):   # ACP 的具体型号
        model = m.group(1)
    elif re.search(r"(?:用|交给|让)\s*kimi", text, re.I):   # ACP 引擎：模型那一栏就写引擎名
        model = "kimi"
    if m := re.search(r"(?:深度|思考)\s*(?:调到|调成|改成|设为|用|开)?\s*(很高|最高|低|中|高)", text):
        effort = EFFORTS[m.group(1)]
    return model, effort


KIND_CN = {"design": "定方案", "dev": "开发", "simple": "简单任务"}


def said_engine_model(engine: str, text: str) -> str | None:
    """话里点了别家的型号：“用 gpt luna”“codex 用 terra”→ Codex 能用的同名型号里最新的那个；“用 deepseek pro”→ deepseek-v4-pro。"""
    models, _ = engines.get(engine).models()
    if engine == engines.CODEX.name:
        m = re.search(r"(?:gpt|codex)\S{0,3}\s*(?:用\s*)?(sol|terra|luna|astra)", text, re.I)
        return next((x for x in models if m and m.group(1).lower() in x.lower()), None)
    if engine == engines.DEEPSEEK.name and re.search(r"deepseek\S{0,3}\s*(?:用\s*)?(?:v4[- ]?)?pro", text, re.I):
        return next((x for x in models if "pro" in x), None)
    eng = engines.get(engine)
    if eng.pick_model == "own":   # 其他自报型号的引擎（ACP/Kimi 等）：话里提到哪个型号就用哪个
        return next((x for x in models if re.search(re.escape(x), text, re.I)), None)
    return None


def _looks_like_project(cwd: Path) -> bool:
    """手动新开时按文件夹猜：在代码仓库或项目里（有 .git、pyproject.toml、package.json）算开发。"""
    for d in [cwd, *list(cwd.parents)[:3]]:
        if any((d / m).exists() for m in (".git", "pyproject.toml", "package.json")):
            return True
    return False


def _is_reply(text: str) -> bool:
    """一看就是对上一条的回复，不必请远窗判断，直接交给当前对话（省一次调用）。"""
    t = text.strip()
    return bool(_REPLY.fullmatch(t) or _CHOICE.fullmatch(t)) or len(t) <= 3
