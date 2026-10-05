"""应急无线电演练通联日志互证服务 —— FastAPI 入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import database
from .config import http_host, http_port
from .schemas import (
    BatchLogsRequest,
    CreateDrillRequest,
    DrillResponse,
    LockActionResponse,
    LockListResponse,
    LockRequest,
    LogEntry,
    ResultResponse,
    SnapshotDiffResponse,
    SnapshotListResponse,
    SnapshotResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 数据初始化：建库、建表、开启 WAL（幂等）。
    database.init_db()
    yield


app = FastAPI(
    title="应急无线电演练通联日志互证服务",
    version="1.2.0",
    description=(
        "记录各台站通联日志，按相互指认、波段/模式一致、时差不超限的"
        "规则完成全场最优配对（最大配对数 → 最小总时差 → 字典序最小），"
        "支持版本化更正、并发原子写入与演练封存。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(database.ServiceError)
async def _service_error_handler(
    request: Request, exc: database.ServiceError
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error_code, "message": exc.message},
    )


# --------------------------------------------------------------------------
# 演练管理
# --------------------------------------------------------------------------
@app.post(
    "/drills",
    response_model=DrillResponse,
    status_code=201,
    tags=["drills"],
    summary="创建演练（配置非负允许时差）",
)
def create_drill(payload: CreateDrillRequest) -> DrillResponse:
    return database.create_drill(
        payload.drill_id, payload.allowed_time_diff_seconds
    )


@app.get(
    "/drills",
    response_model=list[DrillResponse],
    tags=["drills"],
    summary="列出全部演练",
)
def list_drills() -> list[dict]:
    return database.list_drills()


@app.get(
    "/drills/{drill_id}",
    response_model=DrillResponse,
    tags=["drills"],
    summary="查询演练配置与状态（未知演练 404）",
)
def get_drill(drill_id: str) -> dict:
    return database.get_drill(drill_id)


@app.post(
    "/drills/{drill_id}/seal",
    response_model=DrillResponse,
    tags=["drills"],
    summary="封存演练：之后拒绝写入，仍可查询最终结果",
)
def seal_drill(drill_id: str) -> dict:
    return database.seal_drill(drill_id)


# --------------------------------------------------------------------------
# 日志写入（重传不推进版本；异内容更正并重算全场；并发原子）
# --------------------------------------------------------------------------
@app.put(
    "/drills/{drill_id}/logs/{record_no}",
    response_model=ResultResponse,
    tags=["logs"],
    summary="写入/更正单条日志（同内容重传版本不变）",
)
def upsert_log(drill_id: str, record_no: str, entry: LogEntry) -> dict:
    data = entry.to_service()
    data["record_no"] = record_no  # 路径参数为准
    return database.upsert_log(drill_id, **data)


@app.post(
    "/drills/{drill_id}/logs",
    response_model=ResultResponse,
    tags=["logs"],
    status_code=200,
    summary="批量写入/更正日志（整体事务，任一非法整批拒绝）",
)
def batch_logs(drill_id: str, payload: BatchLogsRequest) -> dict:
    items = []
    for entry in payload.logs:
        data = entry.to_service()
        items.append(data)
    return database.batch_upsert_logs(drill_id, items)


@app.get(
    "/drills/{drill_id}/logs",
    tags=["logs"],
    summary="查看演练下全部日志",
)
def list_logs(drill_id: str) -> list[dict]:
    return database.list_logs(drill_id)


# --------------------------------------------------------------------------
# 互证结果查询
# --------------------------------------------------------------------------
@app.get(
    "/drills/{drill_id}/result",
    response_model=ResultResponse,
    tags=["result"],
    summary="查询互证配对与未配对记录（封存后即最终结果）",
)
def get_result(drill_id: str) -> dict:
    return database.get_result(drill_id)


# --------------------------------------------------------------------------
# 人工锁定（复核员互证）：锁定/解除/清单；封存后拒绝一切锁定操作
# --------------------------------------------------------------------------
@app.put(
    "/drills/{drill_id}/locks",
    response_model=LockActionResponse,
    tags=["locks"],
    summary="锁定一对互证候选（重复锁定同一对视为重传，不推进版本）",
)
def lock_pair(drill_id: str, payload: LockRequest) -> dict:
    return database.lock_pair(
        drill_id, payload.record_a, payload.record_b
    )


@app.post(
    "/drills/{drill_id}/locks/unlock",
    response_model=LockActionResponse,
    tags=["locks"],
    summary="解除已有锁定（锁定不存在返回 404 lock_not_found）",
)
def unlock_pair(drill_id: str, payload: LockRequest) -> dict:
    return database.unlock_pair(
        drill_id, payload.record_a, payload.record_b
    )


@app.get(
    "/drills/{drill_id}/locks",
    response_model=LockListResponse,
    tags=["locks"],
    summary="查询锁定清单及最新互证结果",
)
def list_locks(drill_id: str) -> dict:
    return database.list_locks(drill_id)


# --------------------------------------------------------------------------
# 版本快照（复核员按版本回看/比较）：只读，不改变当前状态
# --------------------------------------------------------------------------
@app.get(
    "/drills/{drill_id}/snapshots",
    response_model=SnapshotListResponse,
    tags=["snapshots"],
    summary="列出已留存快照的版本号（升级前旧版本不可回看）",
)
def list_snapshots(drill_id: str) -> dict:
    return database.list_snapshots(drill_id)


@app.get(
    "/drills/{drill_id}/snapshots/{version}",
    response_model=SnapshotResponse,
    tags=["snapshots"],
    summary="取回指定版本快照（配对/未配对/总时差/锁定清单）",
)
def get_snapshot(drill_id: str, version: int) -> dict:
    return database.get_snapshot(drill_id, version)


@app.get(
    "/drills/{drill_id}/snapshots/{from_version}/diff/{to_version}",
    response_model=SnapshotDiffResponse,
    tags=["snapshots"],
    summary="比较两个已留存版本（相同版本差异为空；只读）",
)
def compare_snapshots(drill_id: str, from_version: int, to_version: int) -> dict:
    return database.compare_snapshots(drill_id, from_version, to_version)


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


def main() -> None:  # pragma: no cover - 容器入口
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=http_host(),
        port=http_port(),
        proxy_headers=True,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
