"""引擎目录：引擎名 → 实现与能力；“选的模型”（网页菜单、话里的“用 gpt”、定时任务、总管的 engine 参数）→ 引擎。
Claude 的型号表 MODELS 和深度表 EFFORTS 也放这里，网页、总管查它们不用反过来 import app。"""
from __future__ import annotations

from .. import acp as acp_conn
from .acp import AcpEngine
from .base import Engine
from .claude import ClaudeEngine
from .codex import CodexEngine
from .deepseek import DeepSeekEngine

MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}
HELPER_MODEL = MODELS["haiku"]   # 远窗自己的小活（起标题、分类、翻译、分派判断）用的模型
EFFORTS = {"低": "low", "中": "medium", "高": "high", "很高": "xhigh", "最高": "max", "默认": None}

CLAUDE = ClaudeEngine()
CODEX = CodexEngine()
DEEPSEEK = DeepSeekEngine()
_acp: dict[str, AcpEngine] = {}


def is_acp(engine: str | None) -> bool:
    return bool(engine) and engine in acp_conn.ENGINES


def get(engine: str | None) -> Engine:
    """对话登记的引擎名 → 实现。不认得的当 Claude（早先的对话没写引擎，都是 Claude）。"""
    if engine == CODEX.name:
        return CODEX
    if engine == DEEPSEEK.name:
        return DEEPSEEK
    if is_acp(engine):
        return _acp.setdefault(engine, AcpEngine(engine))
    return CLAUDE


def known() -> list[Engine]:
    """登记了的全部引擎（装没装上看 available()）。"""
    return [CLAUDE, CODEX, DEEPSEEK, *(get(k) for k in acp_conn.ENGINES)]


def by_key(key: str | None) -> Engine | None:
    return next((e for e in known() if e.key == key), None) if key else None


def engine_of(pick: str | None) -> str | None:
    """选的“模型”→ 要换成的引擎名：gpt → codex，kimi → kimi，kimi-k2 这类具体型号 → kimi；
    Claude 的型号（或没选）返回 None，照常用 Claude。"""
    if e := by_key(pick):
        return e.name if e.pick_model != "menu" else None
    for e in known():
        if e.pick_model == "own" and pick in (e.models()[0] or []):   # ACP/Kimi 探到的具体型号
            return e.name
    return None


def switches(pick: str | None, engine: str) -> bool:
    """选的模型要用另一家引擎（已有对话不能中途换家）。"""
    return (engine_of(pick) or CLAUDE.name) != get(engine).name


def can_switch_model(engine: str, pick: str | None) -> bool:
    """这个对话中途能不能换成这个型号：Claude 按型号表换；ACP/Kimi 换成它自己见过的型号也行；换家（gpt、kimi 这类引擎名）不行。"""
    if not pick:
        return False
    resolved = engine_of(pick)
    if resolved and resolved != get(engine).name:   # 引擎名或别家的型号 = 换家，已有对话不允许
        return False
    eng = get(engine)
    if eng.pick_model == "menu":   # Claude：型号表里的（opus… 短名或 claude-… 全名）
        low = pick.lower()
        return bool(MODELS.get(low)) or low in set(MODELS.values())
    if eng.pick_model == "own":    # Kimi 等：它自己见过的型号（启动时探到的）
        return pick in (eng.models()[0] or [])
    return False
