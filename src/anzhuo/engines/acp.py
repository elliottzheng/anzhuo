"""ACP 引擎（Kimi 等，连接见 anzhuo/acp.py）：开或接着会话 → 说这句话 → 把事件翻成和 Claude 一样的步骤、说明、回复。
第一句话前带上远窗的约定（ACP 没有系统提示）。要批准时：跑命令、删 / 挪文件交给 policy 判，危险的问你，其余放行。
ACP 不能中途插话（排到下一轮），没有回退 / 分叉，查不到额度，也带不上远窗的工具 —— 总管的委派和工人的汇报
走文本命令（【派活】/【转话】/【查一查】/【回报】/【退回】，见 superagent 的 ACP_*_RULES 和 on_turn_text）。"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import acp as acp_conn
from ..report import brief
from . import policy
from .base import Engine, check_folders, codex_instructions, handoff, role

if TYPE_CHECKING:
    from ..report import Turn
    from ..sessions import Job, Runner, Session

ACP_KIND = {"read": "看", "edit": "改", "delete": "删", "move": "挪", "search": "搜：", "execute": "跑命令：", "fetch": "读网页：",
            "think": "想：", "switch_mode": "换模式："}
PATH_KEYS = ("path", "file_path", "paths", "source", "src", "from", "old_path", "destination", "dest", "to", "new_path", "target")


class AcpEngine(Engine):
    pick_model = "own"

    def __init__(self, name: str):
        self.name = self.key = name

    @property
    def label(self) -> str:
        return acp_conn.ENGINES.get(self.name, {}).get("name", self.name)

    def available(self) -> bool:
        return acp_conn.available(self.name)

    def models(self) -> tuple[list[str], str]:
        seen = acp_conn.MODELS_SEEN.get(self.name) or {}
        return seen.get("models", []), seen.get("current") or "它自己配置的模型"

    async def halt(self, conn: Any) -> None:
        await conn.cancel()

    async def run(self, runner: "Runner", s: "Session", job: "Job", turn: "Turn") -> None:
        await check_folders(s)
        name = self.label
        said: list[str] = []   # 这一轮它说过的每一段（工具调用把话隔成几段；最后一段是回复）
        buf = {"say": "", "think": ""}
        calls: dict[str, dict] = {}

        def flush(kind: str) -> None:
            text = buf[kind].strip()
            buf[kind] = ""
            if not text:
                return
            if kind == "say":
                said.append(text)
                turn.partial = ""
            turn.timeline.append({"t": time.time(), "kind": kind, "text": text[:6000 if kind == "say" else 4000]})

        def on_update(u: dict) -> None:
            turn.last_at = time.time()
            k = u.get("sessionUpdate")
            content = u.get("content") if isinstance(u.get("content"), dict) else {}
            if k == "agent_message_chunk":
                if buf["think"]:
                    flush("think")
                buf["say"] += str(content.get("text") or "")
                turn.partial = buf["say"][-6000:]
            elif k == "agent_thought_chunk":
                buf["think"] += str(content.get("text") or "")
            elif k in ("tool_call", "tool_call_update"):
                flush("think"), flush("say")
                cid = str(u.get("toolCallId") or "")
                call = calls.setdefault(cid, {})
                # 只认第一次带来的参数文字：之后的 update 把 content 换成了命令输出，不能拿来找参数
                if "_input" not in call and (got := _acp_input(u)):
                    call["_input"] = got
                call.update({x: v for x, v in u.items() if v is not None and x != "content"})
                call.setdefault("rawInput", call.get("_input"))
                label, detail, target = _acp_step(call)
                if "_at" not in call:   # 第一次：记一步；之后参数补全了（命令、文件名）就原地改这一步，不另起一步
                    call["_at"] = (len(turn.steps), len(turn.timeline))
                    turn.steps.append(label)
                    turn.timeline.append({"t": time.time(), "kind": "do", "text": label, "detail": detail})
                else:
                    i, j = call["_at"]
                    turn.steps[i] = label
                    turn.timeline[j].update(text=label, detail=detail)
                if target and not call.get("_counted"):
                    call["_counted"] = True
                    if call.get("kind") == "execute":
                        turn.commands.append(target[:200])
                    elif call.get("kind") in ("edit", "delete", "move"):
                        turn.changed_files.append(target)
            elif k == "plan":
                st = {"completed": "done", "in_progress": "doing"}
                turn.plan = [{"text": str(x.get("content", ""))[:60], "status": st.get(x.get("status"), "pending")}
                             for x in (u.get("entries") or [])[:20]]
            elif k == "usage_update":
                used, size = u.get("used"), u.get("size")
                if used and size:
                    turn.context_pct = s.context_pct = min(100.0, 100.0 * used / size)
                    s.context_tokens = int(used)

        async def on_permission(prm: dict) -> str | None:
            """要批准时：跑命令、删 / 挪文件交给 policy（危险的问你，不同意就拒；看不出做什么也问），其余放行一次。"""
            call = prm.get("toolCall") or {}
            opts = prm.get("options") or []
            pick = lambda *kinds: next((o.get("optionId") for k in kinds for o in opts if o.get("kind") == k), None)  # noqa: E731
            known = calls.get(str(call.get("toolCallId") or ""), {})
            merged = {**known, **{x: v for x, v in call.items() if v is not None}}
            kind = str(known.get("kind") or call.get("kind") or "")
            target = permission_target(kind, known, merged, s.cwd)
            if kind in policy.KINDS and await policy.approve(runner, s, turn, kind, target, strict=True,
                                                             title=str(call.get("title") or "")) is False:
                return pick("reject_once", "reject_always")
            return pick("allow_once", "allow_always")

        conn = acp_conn.AcpConn(s.engine, s.cwd, on_update, on_permission, add_dirs=s.add_dirs)
        runner.live[s.num] = conn
        turn.model = name
        turn.timeline.append({"t": time.time(), "kind": "you", "text": (job.text or "（见图）")})
        rl = role(runner, s)
        try:
            await conn.start()
            fresh = not s.acp_id
            s.acp_id = await conn.open_session(s.acp_id or None)
            runner.registry.save()
            if s.model:
                await conn.set_option("model", s.model)
            effort = s.effort or runner.default_effort or ""
            if effort and (want := thinking_value(effort, conn.option("thinking"))):
                await conn.set_option("thinking", want)
            acp_conn.remember(s.engine, conn.option("model"))   # 总管派活时知道它有哪些模型
            model = (conn.option("model") or {}).get("currentValue") or ""
            turn.model = f"{name}（{model}）" if model else name
            text = job.text
            if fresh:   # ACP 没有系统提示：远窗的约定放在第一句话前面
                rules = codex_instructions(runner.language, runner.extra_instructions) + rl.get("append", "")
                text = f"（远窗的约定，整个对话都照此执行：{rules}）\n\n{text}"
            stop = await conn.prompt(text, job.images)
            flush("think"), flush("say")
            if stop == "cancelled":
                turn.interrupted = True
            elif stop in ("refusal", "max_tokens", "max_turn_requests"):
                turn.error = {"refusal": f"{name} 拒绝了这个请求", "max_tokens": "回复太长被截断了",
                              "max_turn_requests": "这一轮工具调用次数到上限了"}[stop]
        except acp_conn.AcpError as e:
            turn.is_error = True
            turn.error = str(e)[:300]
        finally:
            turn.accepting = False
            runner.live.pop(s.num, None)
            await conn.close()
        turn.partial = ""
        turn.final_text = said[-1] if said else ""
        turn.final_text, imgs, files = handoff(turn.final_text, s.cwd)
        turn.images += imgs
        turn.files += files
        turn.effort = s.effort or runner.default_effort or ""


def permission_target(kind: str, known: dict, merged: dict, cwd: str) -> str | list[str]:
    """批准请求要判的东西：跑命令 → 命令；删 / 挪 → 涉及的路径（相对路径按工作文件夹算）。
    先用之前记下的参数，再看批准请求自己带的文字（Kimi 只写“Requesting approval to Running: …”）。"""
    if kind == "execute":
        return _acp_step(known)[2] or _acp_step(merged)[2]
    if kind in ("delete", "move"):
        paths = _acp_paths(known) or _acp_paths(merged)
        return [p if Path(p).expanduser().is_absolute() else str(Path(cwd) / p) for p in paths]
    return ""


def _acp_texts(call: dict) -> list[str]:
    """工具调用里附带的文字（各家放的地方不一样：参数 JSON、“Requesting approval to Running: …”）。"""
    out = []
    for c in call.get("content") or []:
        inner = c.get("content") if isinstance(c, dict) else None
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            out.append(inner["text"])
    return out


def _acp_input(call: dict) -> dict:
    """这次调用的参数：有 rawInput 用它；没有就从附带的 JSON 文字里找（Kimi 开头只给标题和一段 JSON）。"""
    raw = call.get("rawInput")
    if isinstance(raw, dict) and raw:
        return raw
    for text in _acp_texts(call):
        if text.lstrip().startswith("{"):
            try:
                d = json.loads(text)
            except ValueError:
                continue
            if isinstance(d, dict):
                return d
    return {}


def _acp_paths(call: dict) -> list[str]:
    """这次调用涉及的所有路径（locations、参数里的路径；都没有就看标题“Deleting …”）。挪文件的来源、去处都算。"""
    raw = _acp_input(call)
    out = [str(x.get("path")) for x in call.get("locations") or [] if isinstance(x, dict) and x.get("path")]
    for k in PATH_KEYS:   # 有的助手一次删好几个：参数是路径列表
        v = raw.get(k)
        out += [x.strip() for x in (v if isinstance(v, list) else [v]) if isinstance(x, str) and x.strip()]
    if not out:
        words = [str(call.get("title") or ""), *_acp_texts(call)]
        out = [m.group(1).strip() for w in words if (m := re.search(r"(?:Deleting|Moving|Removing)\s+(\S.*)$", w))][:1]
    return list(dict.fromkeys(out))


def _acp_step(call: dict) -> tuple[str, str, str]:
    """ACP 的一次工具调用 → (步骤名, 细节, 命令或文件)。命令、路径找不到时第三项为空。"""
    kind, title = str(call.get("kind") or ""), str(call.get("title") or "")
    raw = _acp_input(call)
    locs = [str(x.get("path")) for x in call.get("locations") or [] if isinstance(x, dict) and x.get("path")]
    words = [title, *_acp_texts(call)]
    cmd = ""
    if kind == "execute":
        cmd = str(raw.get("command") or raw.get("cmd") or "")
        if not cmd:   # “Running: ls -la”“Requesting approval to Running: git push”
            cmd = next((m.group(1).strip() for w in words if (m := re.search(r"Running:\s*(.+)", w, re.S))), "")
    path = locs[0] if locs else str(raw.get("path") or raw.get("file_path") or "")
    if not path and kind in ("read", "edit", "delete", "move"):
        path = next((m.group(1).strip() for w in words if (m := re.search(r"(?:Writing|Reading|Editing|Deleting|Moving)\s+(\S.*)$", w))), "")
    if kind == "execute" and not cmd:
        return "跑命令", title, ""
    if kind == "execute":
        return "跑命令：" + brief(cmd, 18), cmd[:3000], cmd
    if kind in ("read", "edit", "delete", "move") and path:
        return f"{ACP_KIND[kind]} {Path(path).name}", path, path
    return (ACP_KIND.get(kind, "") + brief(title, 18)) if title else (kind or "工具"), (json.dumps(raw, ensure_ascii=False)[:500] if raw else title), ""


def thinking_value(effort: str, option: dict | None) -> str:
    """远窗的思考深度（low…max）→ 这个助手“思考”配置项里最接近的取值（各家不一样：off/on、low/high/max……）。"""
    have = [str(x.get("value")) for x in (option or {}).get("options") or []]
    prefs = {"low": ["low", "off", "minimal"], "medium": ["medium", "high", "on"], "high": ["high", "on", "medium"],
             "xhigh": ["xhigh", "max", "high", "on"], "max": ["max", "xhigh", "high", "on"]}.get(effort, [effort])
    return next((v for v in prefs if v in have), "")
