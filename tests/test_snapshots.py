"""版本快照（复核员按版本回看/比较）集成测试。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient


def _make_client(db_path: str):
    os.environ["QSO_DB_PATH"] = db_path
    from app import database  # 延迟导入：config 在调用时读环境变量
    from app.main import app

    return TestClient(app), database


def _log(no, station, counterpart, t, band="40m", mode="SSB"):
    return {
        "record_no": no,
        "station": station,
        "counterpart": counterpart,
        "time": t,
        "band": band,
        "mode": mode,
    }


def _pair_keys(pairs):
    return sorted((p["record_a"], p["record_b"]) for p in pairs)


def _lock_keys(locks):
    return sorted((l["record_a"], l["record_b"]) for l in locks)


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("QSO_DB_PATH", tmp.name)
    from app.main import app

    with TestClient(app) as c:
        yield c
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


def _seed_four(client, drill="SN", allowed=50):
    """左 L1(t0)/L2(t100)，右 R1(t0)/R2(t50)；自由最优 2 对。批量写入后 v1。"""
    client.post(
        "/drills",
        json={"drill_id": drill, "allowed_time_diff_seconds": allowed},
    )
    client.post(f"/drills/{drill}/logs", json={"logs": [
        _log("L1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("L2", "A", "B", "2026-10-04T00:01:40+00:00"),
        _log("R1", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("R2", "B", "A", "2026-10-04T00:00:50+00:00"),
    ]})


# --------------------------------------------------------------------------
# 留存时机
# --------------------------------------------------------------------------
def test_v0_snapshot_persisted_at_creation(client):
    r = client.post(
        "/drills", json={"drill_id": "D", "allowed_time_diff_seconds": 10}
    )
    assert r.status_code == 201
    snap = client.get("/drills/D/snapshots/0")
    assert snap.status_code == 200, snap.text
    body = snap.json()
    assert body["version"] == 0
    assert body["pairs"] == []
    assert body["unpaired"] == []
    assert body["locks"] == []
    assert body["total_time_diff_seconds"] == 0.0
    assert body["sealed"] is False
    assert "snapshot_at" in body
    # 版本清单仅含 0。
    assert client.get("/drills/D/snapshots").json()["versions"] == [0]


def test_write_and_lock_each_persist_snapshot(client):
    _seed_four(client)
    versions = client.get("/drills/SN/snapshots").json()["versions"]
    assert versions == [0, 1]

    v1 = client.get("/drills/SN/snapshots/1").json()
    assert _pair_keys(v1["pairs"]) == [("L1", "R1"), ("L2", "R2")]
    assert v1["unpaired"] == []
    assert v1["total_time_diff_seconds"] == 50
    assert v1["locks"] == []

    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R2"})
    v2 = client.get("/drills/SN/snapshots/2").json()
    assert _pair_keys(v2["pairs"]) == [("L1", "R2")]
    assert sorted(v2["unpaired"]) == ["L2", "R1"]
    assert _lock_keys(v2["locks"]) == [("L1", "R2")]
    assert v2["locks"][0]["time_diff_seconds"] == 50
    assert client.get("/drills/SN/snapshots").json()["versions"] == [0, 1, 2]


def test_retransmit_does_not_persist_snapshot(client):
    _seed_four(client)
    # 同内容重传（规范化后一致）不推进版本，也不产生快照。
    client.put("/drills/SN/logs/L1", json=_log(
        "L1", " a ", " b ", "2026-10-04T00:00:00+00:00", band="40M"))
    # 重复锁定同一对为重传，同样不产生快照。
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R1"})
    client.put("/drills/SN/locks", json={"record_a": "R1", "record_b": "L1"})
    assert client.get("/drills/SN/snapshots").json()["versions"] == [0, 1, 2]


def test_failed_operations_and_rollback_persist_no_snapshot(client):
    _seed_four(client)
    # 非法锁定（占用/非候选）回滚 -> 无新快照。
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R2"})
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R1"})
    client.put("/drills/SN/locks", json={"record_a": "L2", "record_b": "R1"})
    # 解除不存在的锁定 -> 404，无新快照。
    client.post("/drills/SN/locks/unlock",
                json={"record_a": "L2", "record_b": "R1"})
    # 更正使锁定失效 -> 整次写入回滚，无新快照。
    r = client.put(
        "/drills/SN/logs/R2",
        json=_log("R2", "B", "A", "2026-10-04T00:02:00+00:00"),
    )
    assert r.status_code == 409 and r.json()["error"] == "lock_invalidated"
    assert client.get("/drills/SN/snapshots").json()["versions"] == [0, 1, 2]


def test_seal_persists_no_snapshot_but_snapshot_reads_current_seal(client):
    _seed_four(client)
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R2"})
    assert client.post("/drills/SN/seal").status_code == 200
    # 封存不产生新版本/新快照。
    assert client.get("/drills/SN/snapshots").json()["versions"] == [0, 1, 2]
    # 封存状态仍从现态读取：旧快照的 sealed 随当前状态为 true。
    assert client.get("/drills/SN/snapshots/0").json()["sealed"] is True
    assert client.get("/drills/SN/snapshots/2").json()["sealed"] is True


def test_snapshot_content_is_immutable_after_later_changes(client):
    _seed_four(client)
    before = client.get("/drills/SN/snapshots/1").json()
    # 锁定 + 更正改变现态后，旧快照内容不变。
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R2"})
    client.put(
        "/drills/SN/logs/L2",
        json=_log("L2", "A", "B", "2026-10-04T00:05:00+00:00"),
    )
    after = client.get("/drills/SN/snapshots/1").json()
    assert before == after


# --------------------------------------------------------------------------
# 取回 / 错误
# --------------------------------------------------------------------------
def test_get_snapshot_unknown_drill_and_missing_version(client):
    r = client.get("/drills/NOPE/snapshots/0")
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"

    _seed_four(client)
    r = client.get("/drills/SN/snapshots/99")
    assert r.status_code == 404 and r.json()["error"] == "snapshot_not_found"
    # 负版本号同样明确报错。
    r = client.get("/drills/SN/snapshots/-1")
    assert r.status_code == 404 and r.json()["error"] == "snapshot_not_found"
    # 非整数版本 -> 422。
    assert client.get("/drills/SN/snapshots/abc").status_code == 422


def test_readonly_snapshot_and_compare_do_not_change_state(client):
    _seed_four(client)
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R2"})
    versions_before = client.get("/drills/SN/snapshots").json()
    result_before = client.get("/drills/SN/result").json()
    client.get("/drills/SN/snapshots/1")
    client.get("/drills/SN/snapshots/1/diff/2")
    client.get("/drills/SN/snapshots/2/diff/2")
    assert client.get("/drills/SN/snapshots").json() == versions_before
    assert client.get("/drills/SN/result").json() == result_before


# --------------------------------------------------------------------------
# 版本比较
# --------------------------------------------------------------------------
def test_compare_same_version_is_empty_diff(client):
    _seed_four(client)
    d = client.get("/drills/SN/snapshots/1/diff/1").json()
    assert d["from_version"] == 1 and d["to_version"] == 1
    assert d["pairs"] == {"added": [], "removed": [], "time_diff_changed": []}
    assert d["unpaired"] == {"added": [], "removed": []}
    assert d["locks"] == {"added": [], "removed": []}
    assert d["total_time_diff_seconds"] == {"from": 50.0, "to": 50.0, "delta": 0.0}


def test_compare_pair_unpaired_and_lock_added_removed(client):
    _seed_four(client)  # v1: (L1,R1),(L2,R2)
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R2"})  # v2

    d = client.get("/drills/SN/snapshots/1/diff/2").json()
    assert _pair_keys(d["pairs"]["added"]) == [("L1", "R2")]
    assert _pair_keys(d["pairs"]["removed"]) == [("L1", "R1"), ("L2", "R2")]
    assert d["pairs"]["time_diff_changed"] == []
    assert d["unpaired"]["added"] == ["L2", "R1"]
    assert d["unpaired"]["removed"] == []
    assert _lock_keys(d["locks"]["added"]) == [("L1", "R2")]
    assert d["locks"]["removed"] == []
    assert d["total_time_diff_seconds"] == {"from": 50.0, "to": 50.0, "delta": 0.0}

    # 反方向：新增/移除互换。
    rev = client.get("/drills/SN/snapshots/2/diff/1").json()
    assert _pair_keys(rev["pairs"]["added"]) == [("L1", "R1"), ("L2", "R2")]
    assert _pair_keys(rev["pairs"]["removed"]) == [("L1", "R2")]
    assert rev["unpaired"]["removed"] == ["L2", "R1"]
    assert _lock_keys(rev["locks"]["removed"]) == [("L1", "R2")]
    assert rev["total_time_diff_seconds"]["delta"] == 0.0


def test_compare_lock_removed_on_unlock(client):
    _seed_four(client)
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R2"})  # v2
    client.post("/drills/SN/locks/unlock",
                json={"record_a": "R2", "record_b": "L1"})  # v3
    d = client.get("/drills/SN/snapshots/2/diff/3").json()
    assert _lock_keys(d["locks"]["removed"]) == [("L1", "R2")]
    assert d["locks"]["added"] == []


def test_compare_same_pair_time_diff_change_listed_separately(client):
    _seed_four(client)  # v1: (L1,R1)=0, (L2,R2)=50
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R1"})  # v2
    # 更正 R1 到 t+3 秒（锁定仍合法）：配对不变，时差 0 -> 3，版本 v3。
    r = client.put(
        "/drills/SN/logs/R1",
        json=_log("R1", "B", "A", "2026-10-04T00:00:03+00:00"),
    )
    assert r.status_code == 200, r.text

    d = client.get("/drills/SN/snapshots/2/diff/3").json()
    assert d["pairs"]["added"] == [] and d["pairs"]["removed"] == []
    assert d["pairs"]["time_diff_changed"] == [{
        "record_a": "L1",
        "record_b": "R1",
        "from_time_diff_seconds": 0.0,
        "to_time_diff_seconds": 3.0,
    }]
    # 锁定对身份未变（仅时差变化），不列入新增/移除。
    assert d["locks"] == {"added": [], "removed": []}
    assert d["unpaired"] == {"added": [], "removed": []}
    assert d["total_time_diff_seconds"] == {"from": 50.0, "to": 53.0, "delta": 3.0}


def test_compare_unknown_drill_or_unretained_version_errors(client):
    r = client.get("/drills/NOPE/snapshots/0/diff/1")
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"

    _seed_four(client)
    assert client.get(
        "/drills/SN/snapshots/5/diff/1"
    ).status_code == 404
    assert client.get(
        "/drills/SN/snapshots/1/diff/5"
    ).json()["error"] == "snapshot_not_found"
    # 相同版本但该版本未留存，同样明确报错（不用现态补造）。
    r = client.get("/drills/SN/snapshots/9/diff/9")
    assert r.status_code == 404 and r.json()["error"] == "snapshot_not_found"


# --------------------------------------------------------------------------
# 升级既有库：仅从升级时当前版本开始留存，旧版本不补造
# --------------------------------------------------------------------------
def test_legacy_database_backfills_only_current_version(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("QSO_DB_PATH", tmp.name)

    # 用旧 schema（无 snapshots 表）造一个当前版本 2 的演练，含日志与锁定。
    conn = sqlite3.connect(tmp.name)
    conn.executescript(
        """
        CREATE TABLE drills (
            drill_id TEXT PRIMARY KEY, allowed_diff_secs REAL NOT NULL
                CHECK (allowed_diff_secs >= 0),
            version INTEGER NOT NULL DEFAULT 0, sealed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, sealed_at TEXT);
        CREATE TABLE logs (
            drill_id TEXT NOT NULL, record_no TEXT NOT NULL, station TEXT NOT NULL,
            counterpart TEXT NOT NULL, ts_micros INTEGER NOT NULL,
            ts_iso TEXT NOT NULL, band TEXT NOT NULL, mode TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (drill_id, record_no));
        CREATE TABLE results (
            drill_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
            pairs_json TEXT NOT NULL, unpaired_json TEXT NOT NULL,
            total_time_diff_seconds REAL NOT NULL, recomputed_at TEXT NOT NULL);
        CREATE TABLE locks (
            drill_id TEXT NOT NULL, record_a TEXT NOT NULL, record_b TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (drill_id, record_a, record_b));
        """
    )
    # ts_micros 取 2026-01-01T00:00:00Z / +50s 的微秒值。
    import datetime as dt
    base = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1_000_000)
    conn.execute("INSERT INTO drills VALUES ('OLD', 50, 2, 0, 't0', NULL)")
    conn.executemany(
        "INSERT INTO logs VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("OLD", "L1", "A", "B", base, "2026-01-01T00:00:00+00:00",
             "40M", "SSB", "t"),
            ("OLD", "R2", "B", "A", base + 50_000_000,
             "2026-01-01T00:00:50+00:00", "40M", "SSB", "t"),
        ],
    )
    conn.execute(
        "INSERT INTO results VALUES ('OLD', 2, ?, '[\"X\"]', 50.0, 't')",
        (json.dumps([{"record_a": "L1", "record_b": "R2",
                      "time_diff_seconds": 50.0}]),),
    )
    conn.execute("INSERT INTO locks VALUES ('OLD', 'L1', 'R2', 'tlock')")
    conn.commit()
    conn.close()

    client, database = _make_client(tmp.name)
    # init_db 即升级路径（幂等）。
    database.init_db()
    with client:
        versions = client.get("/drills/OLD/snapshots").json()["versions"]
        assert versions == [2]  # 仅升级时当前版本，0/1 不用现态补造
        assert client.get("/drills/OLD/snapshots/0").status_code == 404
        assert client.get("/drills/OLD/snapshots/1").status_code == 404

        snap = client.get("/drills/OLD/snapshots/2").json()
        assert _pair_keys(snap["pairs"]) == [("L1", "R2")]
        assert snap["unpaired"] == ["X"]
        assert snap["total_time_diff_seconds"] == 50.0
        # 锁定清单取自升级时现态（含实时时差）。
        assert _lock_keys(snap["locks"]) == [("L1", "R2")]
        assert snap["locks"][0]["time_diff_seconds"] == 50.0

        # 升级后版本推进照常留存。
        client.put(
            "/drills/OLD/logs/X",
            json=_log("X", "C", "D", "2026-01-01T00:00:00+00:00"),
        )
        assert client.get("/drills/OLD/snapshots").json()["versions"] == [2, 3]

    # init_db 再次执行幂等：不重复回填、不报错。
    database.init_db()
    conn = sqlite3.connect(tmp.name)
    rows = conn.execute(
        "SELECT version FROM snapshots WHERE drill_id='OLD' ORDER BY version"
    ).fetchall()
    assert [r[0] for r in rows] == [2, 3]
    conn.close()

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass
