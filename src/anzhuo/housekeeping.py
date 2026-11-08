"""后台维护：每 10 分钟读本机 Claude Code 的消耗；每天清旧副本、归档旧记录；每月 1 号派一次清理体检。
清理规则本身在 cleanup.py，消耗换算在 localspend.py；这里只管什么时候跑。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import folders
from .dingtalk import Incoming
from .engines import MODELS

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger(__name__)


async def spend_loop(b: Bridge) -> None:
    """每 10 分钟读本机 Claude Code 记录新写的部分（第一次要读几 GB，放后台线程）。"""
    while True:
        try:
            n = await asyncio.to_thread(b.spend.scan)
            await asyncio.to_thread(b.spend.save)
            log.debug("本机消耗：新读 %d 条", n)
        except Exception:  # noqa: BLE001
            log.warning("读本机 Claude Code 消耗失败", exc_info=True)
        await asyncio.sleep(600)


async def cleanup_loop(b: Bridge) -> None:
    """每天清一次旧副本、归档 90 天前的旧对话记录（见 cleanup.py）；登记着的对话不动。"""
    from . import cleanup
    while True:
        await asyncio.sleep(600)
        try:
            keep = {s.claude_id for s in b.registry.sessions.values() if s.claude_id}
            await asyncio.to_thread(cleanup.run, b.data_dir, keep)
            using = {s.cwd for s in b.registry.sessions.values() if s.pinned or (not s.hidden and time.time() - s.updated < 60 * 86400)}
            await asyncio.to_thread(cleanup.tidy_scratch, folders._expand(b.cfg.scratch_root), using)
            await monthly_checkup(b)
        except Exception:  # noqa: BLE001
            log.exception("定期清理失败")
        await asyncio.sleep(86400 - 600)


async def monthly_checkup(b: Bridge) -> None:
    """每月 1 号起派一次 AI 体检：新开一个对话，列出建议清理的东西，最后用“要你定”问你，你点头才挪进废纸篓。"""
    from .cleanup import CHECKUP
    mark = b.data_dir / "checkup.json"
    month = time.strftime("%Y-%m")
    try:
        done = json.loads(mark.read_text()).get("month")
    except (OSError, ValueError):
        done = None
    if done is None:   # 头一次：从下个月 1 号开始
        mark.write_text(json.dumps({"month": month}))
        return
    if done == month:
        return
    mark.write_text(json.dumps({"month": month}))
    num = b.registry.reserve_num()
    inc = Incoming(sender_id=b.user_id, sender_nick="", conversation_type="1", kind="text", text=CHECKUP)
    await b.start_session(num, Path.home(), [], CHECKUP, inc, note="（每月清理体检，自动派的）", kind="simple",
                          model=MODELS["sonnet"], effort="low")
    if s := b.registry.get(num):
        s.name, s.renamed = f"{time.localtime().tm_mon} 月清理体检", True
        b.registry.save()
