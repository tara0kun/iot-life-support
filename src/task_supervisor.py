"""asyncio タスクの静かな死を防ぐ supervisor。

背景: `asyncio.gather(*tasks, return_exceptions=True)` は個別タスクが例外や return で
終了しても待ち続ける。センサ task が死んでもプロセスは active のまま、実質何もして
いない状態になる (lessons/iot-rpi.md L010)。

対処: 各 sensor task を supervise() で wrap し、例外/正常終了に関わらず自動再起動。

使い方 (src/monitor.py の main() で):

    from src.task_supervisor import supervise

    tasks.append(asyncio.create_task(supervise("bathroom_meter", meter.run)))
    tasks.append(asyncio.create_task(supervise("contact_sensor", contact.run)))
    ...
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger("task_supervisor")


def _notify_dead(name: str, restart_count: int) -> None:
    """再起動上限到達時の LINE 保険通知。circular import 回避のため関数内で import。"""
    try:
        from src.notifier import send_line_message
        send_line_message(
            f"⚠️ iot-monitor 内の '{name}' タスクが再起動 {restart_count} 回に達しました。"
            f"手動での確認が必要です (journalctl -u iot-monitor で詳細)。"
        )
    except Exception as e:
        log.exception("supervisor 保険通知失敗: %s", e)


async def supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    restart_delay: int = 30,
    max_restarts: int | None = None,
) -> None:
    """factory() が返す coroutine を無限ループで実行し、例外/正常終了で自動再起動する。

    Args:
        name: ログ表示用の名前 (例 "bathroom_meter")
        factory: 呼ぶと新しい coroutine を返す関数 (例 `lambda: meter.run()` or `meter.run`)
        restart_delay: 再起動前の待機秒数 (default 30秒)
                       これは以下の理由で必要:
                       - 即再起動するとリソース (BLE アダプタ, socket) が解放されていない可能性
                       - 起動時にネットワーク不整合があると連続失敗、ログ爆発を防ぐ
        max_restarts: 再起動上限。None なら無制限 (default)。数値指定時、上限到達で
                      LINE で家族に通知して task 停止。
    """
    count = 0
    while max_restarts is None or count < max_restarts:
        try:
            log.info("[supervisor] %s 起動 (試行 %d)", name, count + 1)
            await factory()
            log.warning("[supervisor] %s が正常終了 → %d 秒後に再起動", name, restart_delay)
        except asyncio.CancelledError:
            log.info("[supervisor] %s キャンセル (プロセス終了)", name)
            raise
        except Exception as e:
            log.exception("[supervisor] %s 死亡: %s → %d 秒後に再起動", name, e, restart_delay)
        count += 1
        await asyncio.sleep(restart_delay)

    log.error("[supervisor] %s 再起動 %d 回で上限到達、諦め", name, max_restarts)
    _notify_dead(name, count)
