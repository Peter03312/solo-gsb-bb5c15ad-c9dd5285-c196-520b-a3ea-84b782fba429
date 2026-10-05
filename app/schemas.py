"""请求/响应 Pydantic 模型与输入校验。

明确报错的非法输入：
  * 允许时差为负数；
  * 时间缺时区信息（naive datetime）；
  * 本台呼号与对方呼号相同；
  * 呼号/记录号/波段/模式为空或全空白。
未知演练、封存写入等状态错误在路由/服务层报错。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MICROS_PER_SECOND = 1_000_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _clean_code(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串")
    cleaned = value.strip().upper()
    if not cleaned:
        raise ValueError(f"{field_name} 不能为空")
    return cleaned


def parse_allowed_diff(value: float) -> float:
    if value < 0:
        raise ValueError("允许时差必须为非负数（秒）")
    return float(value)


def aware_time_to_micros(value: datetime) -> int:
    """要求带时区时间，归一化为 UTC 微秒时间戳。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须携带时区信息，例如 2026-10-04T08:00:00+08:00")
    return int(value.astimezone(timezone.utc).timestamp() * MICROS_PER_SECOND)


_DRILL_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class CreateDrillRequest(StrictModel):
    drill_id: str = Field(..., description="演练号（唯一）")
    allowed_time_diff_seconds: float = Field(
        ..., description="允许时差（秒），非负"
    )

    @field_validator("drill_id")
    @classmethod
    def _drill_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("演练号不能为空")
        if not _DRILL_ID_RE.match(v):
            raise ValueError("演练号只能包含字母、数字、下划线、连字符与点")
        return v

    @field_validator("allowed_time_diff_seconds")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        # 显式校验以返回明确的中文报错（不使用 ge 的内置英文信息）。
        return parse_allowed_diff(v)


class LogEntry(StrictModel):
    record_no: str = Field(..., description="唯一记录号（演练内唯一）")
    station: str = Field(..., description="本台呼号")
    counterpart: str = Field(..., description="对方呼号")
    time: datetime = Field(..., description="带时区的通联时间（ISO 8601）")
    band: str = Field(..., description="波段，如 40m")
    mode: str = Field(..., description="模式，如 SSB")

    @field_validator("record_no")
    @classmethod
    def _record_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("记录号不能为空")
        return v

    @field_validator("station")
    @classmethod
    def _station(cls, v: str) -> str:
        return _clean_code(v, "本台呼号")

    @field_validator("counterpart")
    @classmethod
    def _counterpart(cls, v: str) -> str:
        return _clean_code(v, "对方呼号")

    @field_validator("band", "mode")
    @classmethod
    def _band_mode(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("波段与模式不能为空")
        return v

    @field_validator("time")
    @classmethod
    def _time_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError(
                "时间必须携带时区信息，例如 2026-10-04T08:00:00+08:00"
            )
        return v

    @model_validator(mode="after")
    def _stations_differ(self) -> "LogEntry":
        if self.station == self.counterpart:
            raise ValueError("本台呼号与对方呼号不能相同")
        return self

    def to_service(self) -> dict:
        return {
            "record_no": self.record_no,
            "station": self.station,
            "counterpart": self.counterpart,
            "ts_micros": aware_time_to_micros(self.time),
            "band": self.band.upper(),
            "mode": self.mode.upper(),
        }


class BatchLogsRequest(StrictModel):
    logs: list[LogEntry] = Field(..., min_length=1)


class PairOut(BaseModel):
    record_a: str
    record_b: str
    time_diff_seconds: float


class ResultResponse(StrictModel):
    drill_id: str
    version: int
    sealed: bool
    pairs: list[PairOut]
    unpaired: list[str]
    total_time_diff_seconds: float
    recomputed_at: str | None = None


class LockRequest(StrictModel):
    """锁定/解除锁定请求体：一对记录号（顺序任意，服务端内部升序）。"""

    record_a: str = Field(..., description="第一条记录号（演练内现存）")
    record_b: str = Field(..., description="第二条记录号（演练内现存）")

    @field_validator("record_a", "record_b")
    @classmethod
    def _record_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("记录号不能为空")
        return v


class LockItem(BaseModel):
    record_a: str
    record_b: str
    time_diff_seconds: float | None = None
    locked_at: str


class LockActionResponse(StrictModel):
    """锁定/解除操作响应：当前操作锁定、锁定清单与最新互证结果。"""

    lock: LockItem | None = None
    locks: list[LockItem]
    result: ResultResponse


class LockListResponse(StrictModel):
    """锁定清单查询响应。"""

    drill_id: str
    locks: list[LockItem]
    result: ResultResponse


# --------------------------------------------------------------------------
# 版本快照（复核员按版本回看 / 比较）
# --------------------------------------------------------------------------
class SnapshotResponse(StrictModel):
    """指定版本的留存快照：该版配对、未配对、总时差与锁定清单。"""

    drill_id: str
    version: int
    sealed: bool
    pairs: list[PairOut]
    unpaired: list[str]
    total_time_diff_seconds: float
    locks: list[LockItem]
    snapshot_at: str


class SnapshotListResponse(StrictModel):
    """已留存快照的版本号清单。"""

    drill_id: str
    versions: list[int]


class PairDiff(StrictModel):
    """同一记录对在两个版本间的时差变化。"""

    record_a: str
    record_b: str
    from_time_diff_seconds: float
    to_time_diff_seconds: float


class PairDiffSection(StrictModel):
    added: list[PairOut]
    removed: list[PairOut]
    time_diff_changed: list[PairDiff]


class UnpairedDiffSection(StrictModel):
    added: list[str]
    removed: list[str]


class LockDiffSection(StrictModel):
    added: list[LockItem]
    removed: list[LockItem]


class TotalDiffSection(StrictModel):
    from_: float = Field(..., alias="from")
    to: float
    delta: float

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class SnapshotDiffResponse(StrictModel):
    """两个已留存版本的差异（相同版本各节均为空，delta 为 0）。"""

    drill_id: str
    from_version: int
    to_version: int
    pairs: PairDiffSection
    unpaired: UnpairedDiffSection
    locks: LockDiffSection
    total_time_diff_seconds: TotalDiffSection


class DrillResponse(StrictModel):
    drill_id: str
    allowed_time_diff_seconds: float
    version: int
    sealed: bool
    created_at: str
    sealed_at: str | None = None
