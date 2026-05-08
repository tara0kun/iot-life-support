"""家族UI向けに、生センサーイベントを「行動サマリ」へ集約する。

設計方針:
  - 同じソースのペア（open/close, power_on/power_off）は1行に集約
  - 連続するモーションは時間ウィンドウでクラスタリング
  - 湿度の通常レコード（reading）は除外、特筆すべきイベントだけ表示
  - 複数センサ組み合わせで「行動」を予測（例: 風呂ドア閉+モーション+湿度上昇 → 入浴）

戻り値: 表示用の dict のリスト。各要素:
  {
    "icon": "🚽",
    "label": "トイレ使用",
    "detail": "20秒",
    "started_at": "17:30",
    "ended_at": "17:30",
    "person_name": "祖母",
    "sources": ["toilet_door"],
    "raw_event_ids": [123, 124],
  }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

PAIR_SOURCES = {
    "toilet_door": ("🚽", "トイレ"),
    "fridge": ("🧊", "冷蔵庫"),
    "rice_cooker_lid": ("🍚", "炊飯器の蓋"),
}

# 集約から除外するソース・タイプ
SKIP_SOURCES = {"camera"}  # 顔認識は家族UI上は別表示にしている
SKIP_EVENT_TYPES_PREFIX = ("reading",)  # bathroom_meter/reading は除外（個別湿度値）

# 「人物確定」とみなせるソース。これ以外のソースの person_id は active_person からの推定なので、
# サマリには名前を表示しない（誤帰属を避けるため）。
CONFIRMED_PERSON_SOURCES = {
    "camera",            # 顔認識による直接識別
    "family_report",     # 家族が手動記録
    "family_override",   # 家族が修正
    "tablet_report",     # 祖母自身がタブレットで押した
    "family_prompt",     # 家族からの伝言
}

# モーションクラスタの時間ギャップ（このギャップ以下なら同一クラスタ）
MOTION_GAP_MINUTES = 10


@dataclass
class _Activity:
    icon: str
    label: str
    started_at: datetime
    ended_at: datetime
    person_id: int | None = None
    person_name: str | None = None
    sources: list[str] = field(default_factory=list)
    raw_event_ids: list[int] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        d = self.ended_at - self.started_at
        return {
            "icon": self.icon,
            "label": self.label,
            "detail": self.detail,
            "started_at": self.started_at.strftime("%H:%M"),
            "ended_at": self.ended_at.strftime("%H:%M"),
            "duration_seconds": int(d.total_seconds()),
            "person_id": self.person_id,
            "person_name": self.person_name,
            "sources": list(set(self.sources)),
            "raw_event_ids": self.raw_event_ids,
        }


def _to_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("T", " "))
        except ValueError:
            return None
    return None


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    m = seconds // 60
    if m < 60:
        return f"{m}分"
    h = m // 60
    return f"{h}時間{m % 60}分"


def summarize_events(events: list[dict], persons_by_id: dict[int, str] | None = None) -> list[dict]:
    """生イベントの list を「行動」サマリに集約する。

    events: dict のリスト。各要素は { id, source, event_type, started_at, person_id, value }。
    persons_by_id: { person_id: name } の辞書（任意）。
    """
    persons_by_id = persons_by_id or {}
    # 古い順に並べ替え
    sorted_events = sorted(
        [e for e in events if e.get("source") not in SKIP_SOURCES],
        key=lambda e: _to_dt(e.get("started_at")) or datetime.min,
    )

    activities: list[_Activity] = []
    open_pairs: dict[str, dict] = {}   # source -> 待機中の open イベント
    motion_clusters: dict[str, _Activity] = {}  # source -> 進行中クラスタ

    for ev in sorted_events:
        source = ev.get("source", "")
        et = ev.get("event_type", "")
        ts = _to_dt(ev.get("started_at"))
        if ts is None:
            continue
        if any(et.startswith(p) for p in SKIP_EVENT_TYPES_PREFIX) and source == "bathroom_meter":
            continue
        ev_id = ev.get("id")
        person_id = ev.get("person_id")
        # 確定ソース由来の person_id のみ名前を引く。推定（active_person 由来）は表示しない
        if source in CONFIRMED_PERSON_SOURCES and person_id:
            person_name = persons_by_id.get(person_id)
        else:
            person_name = None
            person_id = None
        value = ev.get("value")

        # ペア処理: open/close, power_on/power_off
        if source in PAIR_SOURCES and et == "open":
            open_pairs[source] = ev
            continue
        if source in PAIR_SOURCES and et == "close":
            opener = open_pairs.pop(source, None)
            if opener:
                start_ts = _to_dt(opener.get("started_at")) or ts
                duration_sec = int((ts - start_ts).total_seconds())
                icon, base_label = PAIR_SOURCES[source]
                a = _Activity(
                    icon=icon,
                    label=f"{base_label}使用",
                    started_at=start_ts,
                    ended_at=ts,
                    person_id=person_id or opener.get("person_id"),
                    person_name=person_name or persons_by_id.get(opener.get("person_id")),
                    sources=[source],
                    raw_event_ids=[opener.get("id"), ev_id],
                    detail=_format_duration(duration_sec),
                )
                activities.append(a)
            else:
                # close単体: 不完全
                icon, base_label = PAIR_SOURCES[source]
                activities.append(_Activity(
                    icon=icon, label=f"{base_label}が閉まった",
                    started_at=ts, ended_at=ts,
                    person_id=person_id, person_name=person_name,
                    sources=[source], raw_event_ids=[ev_id], detail="",
                ))
            continue

        # 炊飯器: power_on / power_off
        if source == "rice_cooker" and et == "power_on":
            open_pairs.setdefault("__rice", ev)
            continue
        if source == "rice_cooker" and et == "power_off":
            starter = open_pairs.pop("__rice", None)
            start_ts = _to_dt(starter.get("started_at")) if starter else ts
            duration_sec = int((ts - start_ts).total_seconds()) if starter else 0
            power = starter.get("value") if starter else value
            label = "炊飯器稼働"
            if power and power >= 600:
                label = "炊飯器（炊飯）"
            elif power and power < 100:
                label = "炊飯器（保温）"
            activities.append(_Activity(
                icon="🍚", label=label,
                started_at=start_ts, ended_at=ts,
                person_id=person_id, person_name=person_name,
                sources=["rice_cooker"],
                raw_event_ids=[starter.get("id") if starter else None, ev_id],
                detail=f"{int(power) if power else '?'}W / {_format_duration(duration_sec)}",
            ))
            continue

        # 脱衣所モーション: クラスタ集約
        if source == "bath_motion" and et == "motion":
            cluster = motion_clusters.get(source)
            if cluster and (ts - cluster.ended_at).total_seconds() <= MOTION_GAP_MINUTES * 60:
                cluster.ended_at = ts
                cluster.raw_event_ids.append(ev_id)
                count = len(cluster.raw_event_ids)
                cluster.detail = f"{count}回 / {_format_duration(int((cluster.ended_at - cluster.started_at).total_seconds()))}"
            else:
                cluster = _Activity(
                    icon="🚶", label="脱衣所で動き",
                    started_at=ts, ended_at=ts,
                    person_id=person_id, person_name=person_name,
                    sources=["bath_motion"], raw_event_ids=[ev_id],
                    detail="1回",
                )
                motion_clusters[source] = cluster
                activities.append(cluster)
            continue

        # bath_door 入浴開始/終了は専用ラベル
        if source == "bath_door" and et == "bath_start":
            activities.append(_Activity(
                icon="🛁", label="浴室に入った",
                started_at=ts, ended_at=ts,
                person_id=person_id, person_name=person_name,
                sources=["bath_door"], raw_event_ids=[ev_id], detail="ドア閉",
            ))
            continue
        if source == "bath_door" and et == "bath_end":
            duration = value if isinstance(value, (int, float)) else None
            activities.append(_Activity(
                icon="🛁", label="浴室から出た",
                started_at=ts, ended_at=ts,
                person_id=person_id, person_name=person_name,
                sources=["bath_door"], raw_event_ids=[ev_id],
                detail=f"{int(duration)}分" if duration else "",
            ))
            continue
        # bath_door の単純open/closeは省略（bath_start/bath_endに包含）
        if source == "bath_door" and et in ("open", "close"):
            continue

        # bathroom_meter の特筆イベント
        if source == "bathroom_meter":
            if et == "shower_start":
                activities.append(_Activity(
                    icon="🚿", label="シャワー使用検知",
                    started_at=ts, ended_at=ts,
                    person_id=person_id, person_name=person_name,
                    sources=["bathroom_meter"], raw_event_ids=[ev_id],
                    detail=f"湿度{int(value) if value else '?'}%",
                ))
            elif et == "abnormal_temp":
                activities.append(_Activity(
                    icon="🚨", label="浴室の温度異常",
                    started_at=ts, ended_at=ts,
                    person_id=person_id, person_name=person_name,
                    sources=["bathroom_meter"], raw_event_ids=[ev_id],
                    detail="ヒートショック注意",
                ))
            continue

        # その他のソースは行動サマリでは省略（詳細イベント表で見られる）
        # 例: family_report, tablet_report, family_override, contact_sensor の不明系は冗長なので非表示
        if source in {"family_report", "tablet_report", "family_override"}:
            label_map = {
                "family_report": "家族が記録",
                "tablet_report": "祖母タブレット記録",
                "family_override": "家族が修正",
            }
            activities.append(_Activity(
                icon="📝", label=f"{label_map[source]}: {et}",
                started_at=ts, ended_at=ts,
                person_id=person_id, person_name=person_name,
                sources=[source], raw_event_ids=[ev_id],
                detail="",
            ))
            continue
        # 認識できないソースはサマリ非表示

    # 残った open（close未到来）も「進行中」として追加
    for source, ev in open_pairs.items():
        if source.startswith("__"):
            continue
        ts_open = _to_dt(ev.get("started_at"))
        if ts_open is None:
            continue
        icon, base_label = PAIR_SOURCES.get(source, ("🔹", source))
        activities.append(_Activity(
            icon=icon, label=f"{base_label}が開いている",
            started_at=ts_open, ended_at=ts_open,
            person_id=ev.get("person_id"),
            person_name=persons_by_id.get(ev.get("person_id")),
            sources=[source], raw_event_ids=[ev.get("id")],
            detail="未閉",
        ))

    # 新しい順に並べ替え
    activities.sort(key=lambda a: a.started_at, reverse=True)
    return [a.to_dict() for a in activities]
