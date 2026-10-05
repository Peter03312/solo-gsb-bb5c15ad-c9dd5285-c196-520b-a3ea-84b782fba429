"""SQLite 持久化层。

并发写入由 SQLite 的写事务串行化保证：每次写入都开启
``BEGIN IMMEDIATE`` 立即获取 RESERVED 锁，第二个并发写入会在获取锁时
阻塞/报忙；配合 busy_timeout 重试，所有写入被串行提交。版本号、
日志与配对结果在同一个事务中更新，保证原子性。

启用 WAL 模式以支持“封存/计算期间仍可并发查询最终结果”。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable

from . import config
from .matching import (
    InvalidLockError,
    MatchResult,
    Pair,
    Record,
    match_records,
    pair_eligibility,
)

MICROS_PER_SECOND = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS drills (
    drill_id           TEXT PRIMARY KEY,
    allowed_diff_secs  REAL NOT NULL CHECK (allowed_diff_secs >= 0),
    version            INTEGER NOT NULL DEFAULT 0,
    sealed             INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    sealed_at          TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    drill_id     TEXT NOT NULL,
    record_no    TEXT NOT NULL,
    station      TEXT NOT NULL,
    counterpart  TEXT NOT NULL,
    ts_micros    INTEGER NOT NULL,
    ts_iso       TEXT NOT NULL,
    band         TEXT NOT NULL,
    mode         TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (drill_id, record_no),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

CREATE TABLE IF NOT EXISTS results (
    drill_id                  TEXT PRIMARY KEY,
    version                   INTEGER NOT NULL,
    pairs_json                TEXT NOT NULL,
    unpaired_json             TEXT NOT NULL,
    total_time_diff_seconds   REAL NOT NULL,
    recomputed_at             TEXT NOT NULL,
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

-- 复核员人工锁定（互证）：记录号内部升序，一条记录至多被一个锁定占用。
-- 占用冲突由服务层在写事务内校验（SQLite 不支持跨列互斥约束）。
CREATE TABLE IF NOT EXISTS locks (
    drill_id     TEXT NOT NULL,
    record_a     TEXT NOT NULL,
    record_b     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (drill_id, record_a, record_b),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

-- 版本快照（复核员回看/比较）：版本 0 在创建演练时留存，此后每次版本
-- 真正推进（异内容写入/更正、锁定、解除锁定）在同一事务内留存该版的
-- 配对、未配对记录、总时差与锁定清单。重传、失败回滚、封存不产生快照。
-- 既有演练仅从升级时的当前版本开始留存，更早版本不补造，故历史版本
-- 查不到时按 snapshot_version_not_found 明确报错。
CREATE TABLE IF NOT EXISTS snapshots (
    drill_id                  TEXT NOT NULL,
    version                   INTEGER NOT NULL,
    pairs_json                TEXT NOT NULL,
    unpaired_json             TEXT NOT NULL,
    total_time_diff_seconds   REAL NOT NULL,
    locks_json                TEXT NOT NULL,
    created_at                TEXT NOT NULL,
    PRIMARY KEY (drill_id, version),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);
"""


# --------------------------------------------------------------------------
# 领域错误
# --------------------------------------------------------------------------
class ServiceError(Exception):
    """业务错误基类，附带 HTTP 状态码与机器可读错误码。"""

    status_code = 400
    error_code = "bad_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class DrillNotFoundError(ServiceError):
    status_code = 404
    error_code = "drill_not_found"


class DrillExistsError(ServiceError):
    status_code = 409
    error_code = "drill_exists"


class SealedError(ServiceError):
    status_code = 409
    error_code = "drill_sealed"


class DuplicateRecordInBatchError(ServiceError):
    status_code = 409
    error_code = "duplicate_record_in_batch"


class LockNotFoundError(ServiceError):
    """解除的锁定不存在。"""

    status_code = 404
    error_code = "lock_not_found"


class LockSameRecordError(ServiceError):
    """锁定请求中两个记录号相同。"""

    status_code = 422
    error_code = "lock_same_record"


class LockRecordMissingError(ServiceError):
    """锁定引用了不存在的记录。"""

    status_code = 404
    error_code = "lock_record_missing"


class LockNotCandidateError(ServiceError):
    """两条现存日志不满足互证候选条件（互指/波段模式/时差）。"""

    status_code = 409
    error_code = "lock_not_candidate"


class LockOccupiedError(ServiceError):
    """锁定涉及的记录已被其他锁定占用。"""

    status_code = 409
    error_code = "record_already_locked"


class LockInvalidatedError(ServiceError):
    """写入/更正使既有锁定失效，整次写入被拒绝。"""

    status_code = 409
    error_code = "lock_invalidated"


class SnapshotVersionNotFoundError(ServiceError):
    """请求回看/比较的版本没有留存快照（含升级前的历史版本）。"""

    status_code = 404
    error_code = "snapshot_version_not_found"


# --------------------------------------------------------------------------
# 连接管理
# --------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.database_url(),
        timeout=30.0,
        isolation_level=None,  # 手工控制事务（autocommit 模式）
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """创建数据目录、表结构并开启 WAL（幂等）。"""
    import os

    db_path = config.database_url()
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        _seed_current_snapshots(conn)
    finally:
        conn.close()


def _seed_current_snapshots(conn: sqlite3.Connection) -> None:
    """升级补丁：为没有任何快照的既有演练留存“当前版本”快照。

    只在演练完全没有快照时执行一次（幂等）；升级前的历史版本无法由
    现态复原，明确不补造。以现态 results/locks 填充，与升级后版本
    推进时留存的快照同构。
    """
    rows = conn.execute(
        """
        SELECT d.drill_id AS drill_id, d.version AS version,
               r.pairs_json AS pairs_json, r.unpaired_json AS unpaired_json,
               r.total_time_diff_seconds AS total_time_diff_seconds,
               r.recomputed_at AS recomputed_at
        FROM drills d
        LEFT JOIN results r ON r.drill_id = d.drill_id
        WHERE NOT EXISTS (
            SELECT 1 FROM snapshots s WHERE s.drill_id = d.drill_id
        )
        """
    ).fetchall()
    if not rows:
        return
    now = _now_iso()
    with _write_tx(conn):
        for row in rows:
            lock_rows = conn.execute(
                "SELECT record_a, record_b FROM locks WHERE drill_id = ? "
                "ORDER BY record_a, record_b",
                (row["drill_id"],),
            ).fetchall()
            locks_payload = [
                [r["record_a"], r["record_b"]] for r in lock_rows
            ]
            conn.execute(
                """
                INSERT INTO snapshots (drill_id, version, pairs_json,
                    unpaired_json, total_time_diff_seconds, locks_json,
                    created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["drill_id"],
                    row["version"],
                    row["pairs_json"]
                    if row["pairs_json"] is not None
                    else "[]",
                    row["unpaired_json"]
                    if row["unpaired_json"] is not None
                    else "[]",
                    row["total_time_diff_seconds"]
                    if row["total_time_diff_seconds"] is not None
                    else 0.0,
                    json.dumps(locks_payload, ensure_ascii=False),
                    now,
                ),
            )


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 写事务：立即拿写锁，提交/回滚原子。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# 序列化辅助
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _micros_to_iso(ts_micros: int) -> str:
    return datetime.fromtimestamp(
        ts_micros / MICROS_PER_SECOND, tz=timezone.utc
    ).isoformat()


def _record_from_row(row: sqlite3.Row) -> Record:
    return Record(
        record_no=row["record_no"],
        station=row["station"],
        counterpart=row["counterpart"],
        ts_micros=row["ts_micros"],
        band=row["band"],
        mode=row["mode"],
    )


def _load_records(conn: sqlite3.Connection, drill_id: str) -> list[Record]:
    rows = conn.execute(
        "SELECT * FROM logs WHERE drill_id = ? ORDER BY rowid", (drill_id,)
    ).fetchall()
    return [_record_from_row(r) for r in rows]


def _record_map(records: Iterable[Record]) -> dict[str, Record]:
    return {r.record_no: r for r in records}


def _load_lock_keys(conn: sqlite3.Connection, drill_id: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT record_a, record_b FROM locks WHERE drill_id = ? "
        "ORDER BY record_a, record_b",
        (drill_id,),
    ).fetchall()
    return [(r["record_a"], r["record_b"]) for r in rows]


def _check_lock_candidate(
    record_map: dict[str, Record], allowed_micros: int, a: str, b: str
) -> None:
    """把算法层原因码翻译为对应业务错误抛出；合法时静默返回。"""
    problem = pair_eligibility(record_map, allowed_micros, a, b)
    if problem is None:
        return
    code, message = problem
    if code == "same_record":
        raise LockSameRecordError(message)
    if code == "record_missing":
        raise LockRecordMissingError(message)
    raise LockNotCandidateError(message)


def _pairs_payload(result: MatchResult) -> list[dict]:
    return [
        {
            "record_a": p.record_a,
            "record_b": p.record_b,
            "time_diff_seconds": p.time_diff_seconds,
        }
        for p in result.pairs
    ]


def _persist_result(
    conn: sqlite3.Connection, drill_id: str, version: int, result: MatchResult
) -> None:
    pairs_payload = _pairs_payload(result)
    at = _now_iso()
    conn.execute(
        """
        INSERT INTO results (drill_id, version, pairs_json, unpaired_json,
                             total_time_diff_seconds, recomputed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(drill_id) DO UPDATE SET
            version = excluded.version,
            pairs_json = excluded.pairs_json,
            unpaired_json = excluded.unpaired_json,
            total_time_diff_seconds = excluded.total_time_diff_seconds,
            recomputed_at = excluded.recomputed_at
        """,
        (
            drill_id,
            version,
            json.dumps(pairs_payload, ensure_ascii=False),
            json.dumps(list(result.unpaired), ensure_ascii=False),
            result.total_time_diff_seconds,
            at,
        ),
    )


def _persist_snapshot(
    conn: sqlite3.Connection,
    drill_id: str,
    version: int,
    result: MatchResult,
    lock_keys: list[tuple[str, str]],
    created_at: str | None = None,
) -> None:
    """留存指定版本快照：配对/未配对/总时差取该版结果，锁定清单取该版
    锁定键（记录号内部升序）。与结果、版本、锁定状态在同一事务提交。"""
    pairs_payload = _pairs_payload(result)
    conn.execute(
        """
        INSERT INTO snapshots (drill_id, version, pairs_json, unpaired_json,
                               total_time_diff_seconds, locks_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            drill_id,
            version,
            json.dumps(pairs_payload, ensure_ascii=False),
            json.dumps(list(result.unpaired), ensure_ascii=False),
            result.total_time_diff_seconds,
            json.dumps([list(k) for k in lock_keys], ensure_ascii=False),
            created_at or _now_iso(),
        ),
    )


def _recompute(conn: sqlite3.Connection, drill_id: str) -> None:
    """重算全场配对：版本 +1（调用方负责），结果与版本同事务落库，
    并在同一事务留存该版快照。

    先保留全部锁定对，再对剩余日志按“配对数最多、总时差最小、
    记录号字典序最小”求解；锁定即使降低全场配对数也不替换。
    """
    drill = _get_drill_row(conn, drill_id)
    version = drill["version"] + 1
    records = _load_records(conn, drill_id)
    lock_keys = _load_lock_keys(conn, drill_id)
    try:
        result = match_records(
            records, drill["allowed_diff_secs"], locked_keys=lock_keys
        )
    except InvalidLockError as exc:
        raise LockInvalidatedError(str(exc))
    conn.execute(
        "UPDATE drills SET version = ? WHERE drill_id = ?",
        (version, drill_id),
    )
    _persist_result(conn, drill_id, version, result)
    _persist_snapshot(conn, drill_id, version, result, lock_keys)


def _get_drill_row(
    conn: sqlite3.Connection, drill_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM drills WHERE drill_id = ?", (drill_id,)
    ).fetchone()
    if row is None:
        raise DrillNotFoundError(f"演练号不存在: {drill_id}")
    return row


def _require_unsealed(row: sqlite3.Row) -> None:
    if row["sealed"]:
        raise SealedError(f"演练 {row['drill_id']} 已封存，拒绝写入")


# --------------------------------------------------------------------------
# 对外服务操作
# --------------------------------------------------------------------------
def create_drill(drill_id: str, allowed_diff_secs: float) -> dict:
    conn = connect()
    try:
        with _write_tx(conn):
            exists = conn.execute(
                "SELECT 1 FROM drills WHERE drill_id = ?", (drill_id,)
            ).fetchone()
            if exists is not None:
                raise DrillExistsError(f"演练号已存在: {drill_id}")
            conn.execute(
                "INSERT INTO drills (drill_id, allowed_diff_secs, version, "
                "sealed, created_at) VALUES (?, ?, 0, 0, ?)",
                (drill_id, allowed_diff_secs, _now_iso()),
            )
            # 空场结果：版本 0，无配对；版本 0 可回看，同事务留存快照。
            empty = MatchResult((), (), 0.0)
            _persist_result(conn, drill_id, 0, empty)
            _persist_snapshot(conn, drill_id, 0, empty, [])
        return get_drill(drill_id)
    finally:
        conn.close()


def upsert_log(
    drill_id: str,
    record_no: str,
    station: str,
    counterpart: str,
    ts_micros: int,
    band: str,
    mode: str,
) -> dict:
    """写入/更正单条日志；同内容重传不推进版本，异内容更正后重算全场。"""
    conn = connect()
    try:
        changed = False
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            row = conn.execute(
                "SELECT * FROM logs WHERE drill_id = ? AND record_no = ?",
                (drill_id, record_no),
            ).fetchone()
            ts_iso = _micros_to_iso(ts_micros)
            if row is None:
                conn.execute(
                    "INSERT INTO logs (drill_id, record_no, station, "
                    "counterpart, ts_micros, ts_iso, band, mode, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        drill_id, record_no, station, counterpart,
                        ts_micros, ts_iso, band, mode, _now_iso(),
                    ),
                )
                changed = True
            elif (
                row["station"], row["counterpart"], row["ts_micros"],
                row["band"], row["mode"],
            ) != (station, counterpart, ts_micros, band, mode):
                conn.execute(
                    "UPDATE logs SET station = ?, counterpart = ?, "
                    "ts_micros = ?, ts_iso = ?, band = ?, mode = ?, "
                    "updated_at = ? WHERE drill_id = ? AND record_no = ?",
                    (
                        station, counterpart, ts_micros, ts_iso,
                        band, mode, _now_iso(), drill_id, record_no,
                    ),
                )
                changed = True
            # 同内容重传：什么都不改，版本不推进。
            if changed:
                _recompute(conn, drill_id)
        return get_result(drill_id)
    finally:
        conn.close()


def batch_upsert_logs(
    drill_id: str, items: Iterable[dict]
) -> dict:
    """批量写入/更正：整体一个事务，任一非法则整批拒绝。

    批次内同一记录号出现多次视为非法（无法判定终态）。
    """
    items = list(items)
    seen: set[str] = set()
    for it in items:
        if it["record_no"] in seen:
            raise DuplicateRecordInBatchError(
                f"批次内记录号重复: {it['record_no']}"
            )
        seen.add(it["record_no"])

    conn = connect()
    try:
        changed = False
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            for it in items:
                record_no = it["record_no"]
                ts_micros = it["ts_micros"]
                ts_iso = _micros_to_iso(ts_micros)
                row = conn.execute(
                    "SELECT * FROM logs WHERE drill_id = ? AND record_no = ?",
                    (drill_id, record_no),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO logs (drill_id, record_no, station, "
                        "counterpart, ts_micros, ts_iso, band, mode, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            drill_id, record_no, it["station"],
                            it["counterpart"], ts_micros, ts_iso,
                            it["band"], it["mode"], _now_iso(),
                        ),
                    )
                    changed = True
                elif (
                    row["station"], row["counterpart"], row["ts_micros"],
                    row["band"], row["mode"],
                ) != (
                    it["station"], it["counterpart"], ts_micros,
                    it["band"], it["mode"],
                ):
                    conn.execute(
                        "UPDATE logs SET station = ?, counterpart = ?, "
                        "ts_micros = ?, ts_iso = ?, band = ?, mode = ?, "
                        "updated_at = ? WHERE drill_id = ? AND record_no = ?",
                        (
                            it["station"], it["counterpart"], ts_micros,
                            ts_iso, it["band"], it["mode"], _now_iso(),
                            drill_id, record_no,
                        ),
                    )
                    changed = True
            if changed:
                _recompute(conn, drill_id)
        return get_result(drill_id)
    finally:
        conn.close()


def seal_drill(drill_id: str) -> dict:
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            conn.execute(
                "UPDATE drills SET sealed = 1, sealed_at = ? WHERE drill_id = ?",
                (_now_iso(), drill_id),
            )
        return get_drill(drill_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 锁定（复核员人工互证）
# --------------------------------------------------------------------------
def _normalize_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _lock_items(
    conn: sqlite3.Connection,
    drill_id: str,
    record_map: dict[str, Record] | None = None,
) -> list[dict]:
    """组装锁定清单（含实时计算的时差），按记录对升序。"""
    rows = conn.execute(
        "SELECT record_a, record_b, created_at FROM locks "
        "WHERE drill_id = ? ORDER BY record_a, record_b",
        (drill_id,),
    ).fetchall()
    if record_map is None:
        record_map = _record_map(_load_records(conn, drill_id))
    items = []
    for row in rows:
        a, b = row["record_a"], row["record_b"]
        diff = None
        if a in record_map and b in record_map:
            diff = abs(
                record_map[a].ts_micros - record_map[b].ts_micros
            ) / MICROS_PER_SECOND
        items.append(
            {
                "record_a": a,
                "record_b": b,
                "time_diff_seconds": diff,
                "locked_at": row["created_at"],
            }
        )
    return items


def _result_from_conn(conn: sqlite3.Connection, drill_id: str) -> dict:
    """在指定连接（可能是未提交的写事务内）读取当前互证结果。"""
    drill = _get_drill_row(conn, drill_id)
    row = conn.execute(
        "SELECT * FROM results WHERE drill_id = ?", (drill_id,)
    ).fetchone()
    if row is None:  # 理论上不会发生（建演练即写空结果）
        return {
            "drill_id": drill_id,
            "version": drill["version"],
            "sealed": bool(drill["sealed"]),
            "pairs": [],
            "unpaired": [],
            "total_time_diff_seconds": 0.0,
        }
    return {
        "drill_id": drill_id,
        "version": row["version"],
        "sealed": bool(drill["sealed"]),
        "pairs": json.loads(row["pairs_json"]),
        "unpaired": json.loads(row["unpaired_json"]),
        "total_time_diff_seconds": row["total_time_diff_seconds"],
        "recomputed_at": row["recomputed_at"],
    }


def _lock_action_response(
    conn: sqlite3.Connection,
    drill_id: str,
    record_map: dict[str, Record] | None = None,
) -> dict:
    locks = _lock_items(conn, drill_id, record_map)
    payload = _result_from_conn(conn, drill_id)
    return {
        "lock": locks[-1] if locks else None,
        "locks": locks,
        "result": payload,
    }


def lock_pair(drill_id: str, record_a: str, record_b: str) -> dict:
    """锁定一对互证候选；重复锁定同一对视为重传（幂等、不推进版本）。"""
    a, b = _normalize_pair(record_a, record_b)
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            if a == b:
                raise LockSameRecordError(f"两条记录号不能相同: {a}")

            existing = conn.execute(
                "SELECT 1 FROM locks WHERE drill_id = ? AND record_a = ? "
                "AND record_b = ?",
                (drill_id, a, b),
            ).fetchone()
            if existing is not None:
                # 重复锁定同一对：幂等重传，状态不变、版本不推进。
                return _lock_action_response(conn, drill_id)

            records = _load_records(conn, drill_id)
            rmap = _record_map(records)
            allowed_micros = int(
                round(drill["allowed_diff_secs"] * MICROS_PER_SECOND)
            )
            _check_lock_candidate(rmap, allowed_micros, a, b)

            # 两条记录均不得已被其他锁定占用。
            occupied = conn.execute(
                "SELECT record_a, record_b FROM locks WHERE drill_id = ? "
                "AND (record_a = ? OR record_b = ? OR record_a = ? OR record_b = ?)",
                (drill_id, a, a, b, b),
            ).fetchall()
            if occupied:
                other = occupied[0]
                raise LockOccupiedError(
                    f"记录已被锁定 ({other['record_a']}, {other['record_b']}) "
                    f"占用，不能重复锁定"
                )

            conn.execute(
                "INSERT INTO locks (drill_id, record_a, record_b, created_at) "
                "VALUES (?, ?, ?, ?)",
                (drill_id, a, b, _now_iso()),
            )
            # 锁定状态变化与结果、版本原子提交。
            _recompute(conn, drill_id)
            return _lock_action_response(conn, drill_id, rmap)
    finally:
        conn.close()


def unlock_pair(drill_id: str, record_a: str, record_b: str) -> dict:
    """解除已有锁定；锁定不存在时明确报错（404 lock_not_found）。"""
    a, b = _normalize_pair(record_a, record_b)
    conn = connect()
    try:
        with _write_tx(conn):
            drill = _get_drill_row(conn, drill_id)
            _require_unsealed(drill)
            cur = conn.execute(
                "DELETE FROM locks WHERE drill_id = ? AND record_a = ? "
                "AND record_b = ?",
                (drill_id, a, b),
            )
            if cur.rowcount == 0:
                raise LockNotFoundError(f"锁定不存在: ({a}, {b})")
            # 解除锁定后重算：锁定状态变化与结果、版本原子提交。
            _recompute(conn, drill_id)
            return _lock_action_response(conn, drill_id)
    finally:
        conn.close()


def list_locks(drill_id: str) -> dict:
    """查询锁定清单及最新互证结果。"""
    conn = connect()
    try:
        _get_drill_row(conn, drill_id)  # 未知演练 -> 404
        return {
            "drill_id": drill_id,
            "locks": _lock_items(conn, drill_id),
            "result": get_result(drill_id),
        }
    finally:
        conn.close()


def get_drill(drill_id: str) -> dict:
    conn = connect()
    try:
        row = _get_drill_row(conn, drill_id)
        return {
            "drill_id": row["drill_id"],
            "allowed_time_diff_seconds": row["allowed_diff_secs"],
            "version": row["version"],
            "sealed": bool(row["sealed"]),
            "created_at": row["created_at"],
            "sealed_at": row["sealed_at"],
        }
    finally:
        conn.close()


def list_drills() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT drill_id FROM drills ORDER BY drill_id"
        ).fetchall()
        return [get_drill(r["drill_id"]) for r in rows]
    finally:
        conn.close()


def list_logs(drill_id: str) -> list[dict]:
    conn = connect()
    try:
        _get_drill_row(conn, drill_id)  # 未知演练 -> 404
        rows = conn.execute(
            "SELECT * FROM logs WHERE drill_id = ? ORDER BY rowid",
            (drill_id,),
        ).fetchall()
        return [
            {
                "record_no": r["record_no"],
                "station": r["station"],
                "counterpart": r["counterpart"],
                "time": r["ts_iso"],
                "band": r["band"],
                "mode": r["mode"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_result(drill_id: str) -> dict:
    """查询当前（封存后即为最终）互证结果。"""
    conn = connect()
    try:
        drill = _get_drill_row(conn, drill_id)
        row = conn.execute(
            "SELECT * FROM results WHERE drill_id = ?", (drill_id,)
        ).fetchone()
        if row is None:  # 理论上不会发生（建演练即写空结果）
            return {
                "drill_id": drill_id,
                "version": drill["version"],
                "sealed": bool(drill["sealed"]),
                "pairs": [],
                "unpaired": [],
                "total_time_diff_seconds": 0.0,
            }
        return {
            "drill_id": drill_id,
            "version": row["version"],
            "sealed": bool(drill["sealed"]),
            "pairs": json.loads(row["pairs_json"]),
            "unpaired": json.loads(row["unpaired_json"]),
            "total_time_diff_seconds": row["total_time_diff_seconds"],
            "recomputed_at": row["recomputed_at"],
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 版本快照回看 / 比较（只读，不改变当前状态）
# --------------------------------------------------------------------------
def _snapshot_row(
    conn: sqlite3.Connection, drill_id: str, version: int
) -> sqlite3.Row:
    """取已留存快照行；演练未知 -> drill_not_found；版本未留存 ->
    snapshot_version_not_found（升级前历史版本同样报此错）。"""
    _get_drill_row(conn, drill_id)
    row = conn.execute(
        "SELECT * FROM snapshots WHERE drill_id = ? AND version = ?",
        (drill_id, version),
    ).fetchone()
    if row is None:
        raise SnapshotVersionNotFoundError(
            f"演练 {drill_id} 的版本 {version} 未留存快照，无法回看"
        )
    return row


def _snapshot_lock_items(
    conn: sqlite3.Connection,
    drill_id: str,
    lock_keys: list[list[str]],
    record_map: dict[str, Record] | None = None,
) -> list[dict]:
    """把快照中的锁定键展开为清单；时差按当前现存日志计算（与锁定
    查询接口一致），记录已不存在时为 null。锁定键已按记录号升序存储。"""
    if record_map is None:
        record_map = _record_map(_load_records(conn, drill_id))
    items = []
    for key in lock_keys:
        a, b = key[0], key[1]
        diff = None
        if a in record_map and b in record_map:
            diff = (
                abs(record_map[a].ts_micros - record_map[b].ts_micros)
                / MICROS_PER_SECOND
            )
        items.append(
            {"record_a": a, "record_b": b, "time_diff_seconds": diff}
        )
    return items


def _snapshot_view(
    conn: sqlite3.Connection, drill_id: str, row: sqlite3.Row
) -> dict:
    """组装单个快照的回看响应；封存状态始终从现态读取。"""
    drill = _get_drill_row(conn, drill_id)
    lock_keys = json.loads(row["locks_json"])
    return {
        "drill_id": drill_id,
        "version": row["version"],
        "sealed": bool(drill["sealed"]),
        "pairs": json.loads(row["pairs_json"]),
        "unpaired": json.loads(row["unpaired_json"]),
        "total_time_diff_seconds": row["total_time_diff_seconds"],
        "locks": _snapshot_lock_items(conn, drill_id, lock_keys),
        "snapshot_created_at": row["created_at"],
    }


def get_snapshot(drill_id: str, version: int) -> dict:
    """取回指定演练、指定版本的已留存快照（只读）。"""
    conn = connect()
    try:
        row = _snapshot_row(conn, drill_id, version)
        return _snapshot_view(conn, drill_id, row)
    finally:
        conn.close()


def list_snapshots(drill_id: str) -> dict:
    """列出演练全部已留存版本（含 0 版），按版本升序（只读）。"""
    conn = connect()
    try:
        _get_drill_row(conn, drill_id)  # 未知演练 -> 404
        rows = conn.execute(
            "SELECT version, created_at FROM snapshots "
            "WHERE drill_id = ? ORDER BY version",
            (drill_id,),
        ).fetchall()
        return {
            "drill_id": drill_id,
            "versions": [
                {"version": r["version"], "snapshot_created_at": r["created_at"]}
                for r in rows
            ],
        }
    finally:
        conn.close()


def compare_snapshots(drill_id: str, version_from: int, version_to: int) -> dict:
    """比较两个已留存版本：按记录号稳定排序列出配对、未配对记录和锁定
    对的新增与移除；同一记录对的时差变化单列。相同版本差异为空。

    只读，不改变当前状态；封存状态仍从现态读取。
    """
    conn = connect()
    try:
        row_from = _snapshot_row(conn, drill_id, version_from)
        row_to = _snapshot_row(conn, drill_id, version_to)
        drill = _get_drill_row(conn, drill_id)

        pairs_from = {
            (p["record_a"], p["record_b"]): p["time_diff_seconds"]
            for p in json.loads(row_from["pairs_json"])
        }
        pairs_to = {
            (p["record_a"], p["record_b"]): p["time_diff_seconds"]
            for p in json.loads(row_to["pairs_json"])
        }
        unpaired_from = set(json.loads(row_from["unpaired_json"]))
        unpaired_to = set(json.loads(row_to["unpaired_json"]))
        locks_from = {
            (k[0], k[1]) for k in json.loads(row_from["locks_json"])
        }
        locks_to = {
            (k[0], k[1]) for k in json.loads(row_to["locks_json"])
        }

        pair_added_keys = sorted(pairs_to.keys() - pairs_from.keys())
        pair_removed_keys = sorted(pairs_from.keys() - pairs_to.keys())
        diffs = []
        for key in sorted(pairs_from.keys() & pairs_to.keys()):
            before = pairs_from[key]
            after = pairs_to[key]
            if before != after:
                diffs.append(
                    {
                        "record_a": key[0],
                        "record_b": key[1],
                        "time_diff_seconds_from": before,
                        "time_diff_seconds_to": after,
                    }
                )
        lock_added = sorted(locks_to - locks_from)
        lock_removed = sorted(locks_from - locks_to)

        def pair_payload(keys: list[tuple[str, str]], table: dict) -> list[dict]:
            return [
                {
                    "record_a": a,
                    "record_b": b,
                    "time_diff_seconds": table[(a, b)],
                }
                for a, b in keys
            ]

        def lock_payload(keys: list[tuple[str, str]]) -> list[dict]:
            return [{"record_a": a, "record_b": b} for a, b in keys]

        return {
            "drill_id": drill_id,
            "version_from": version_from,
            "version_to": version_to,
            "sealed": bool(drill["sealed"]),
            "pairs": {
                "added": pair_payload(pair_added_keys, pairs_to),
                "removed": pair_payload(pair_removed_keys, pairs_from),
            },
            "unpaired": {
                "added": sorted(unpaired_to - unpaired_from),
                "removed": sorted(unpaired_from - unpaired_to),
            },
            "locks": {
                "added": lock_payload(lock_added),
                "removed": lock_payload(lock_removed),
            },
            "time_diff_changes": diffs,
            "total_time_diff_seconds_from": row_from[
                "total_time_diff_seconds"
            ],
            "total_time_diff_seconds_to": row_to[
                "total_time_diff_seconds"
            ],
        }
    finally:
        conn.close()
