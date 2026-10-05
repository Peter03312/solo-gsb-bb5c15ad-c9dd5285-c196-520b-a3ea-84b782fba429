"""版本快照回看 / 比较的 HTTP 集成测试。"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from fastapi.testclient import TestClient


def _log(no, station, counterpart, t, band="40m", mode="SSB"):
    return {
        "record_no": no,
        "station": station,
        "counterpart": counterpart,
        "time": t,
        "band": band,
        "mode": mode,
    }


def _seed(client, drill="SN"):
    """v1: (L1,R1)/(L2,R2) 两对；返回创建后的 client 状态。"""
    client.post(
        "/drills",
        json={"drill_id": drill, "allowed_time_diff_seconds": 50},
    )
    client.post(f"/drills/{drill}/logs", json={"logs": [
        _log("L1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("L2", "A", "B", "2026-10-04T00:01:40+00:00"),
        _log("R1", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("R2", "B", "A", "2026-10-04T00:00:50+00:00"),
    ]})


# --------------------------------------------------------------------------
# 快照留存规则
# --------------------------------------------------------------------------
def test_version_zero_snapshot_created_with_drill(client):
    client.post(
        "/drills", json={"drill_id": "SN", "allowed_time_diff_seconds": 10}
    )
    r = client.get("/drills/SN/snapshots/0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 0
    assert body["pairs"] == []
    assert body["unpaired"] == []
    assert body["locks"] == []
    assert body["total_time_diff_seconds"] == 0
    assert body["sealed"] is False
    assert "snapshot_created_at" in body

    r = client.get("/drills/SN/snapshots")
    assert r.status_code == 200
    assert [v["version"] for v in r.json()["versions"]] == [0]


def test_snapshot_kept_on_each_real_version_advance(client):
    _seed(client)
    # v1：一次批量异内容写入
    body = client.get("/drills/SN/snapshots/1").json()
    assert [(p["record_a"], p["record_b"]) for p in body["pairs"]] == [
        ("L1", "R1"), ("L2", "R2"),
    ]
    assert body["total_time_diff_seconds"] == 50

    # 锁定 (L1,R2)：v2，快照含该锁定对
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R2"})
    body = client.get("/drills/SN/snapshots/2").json()
    assert [(p["record_a"], p["record_b"]) for p in body["pairs"]] == [
        ("L1", "R2")
    ]
    assert sorted(body["unpaired"]) == ["L2", "R1"]
    assert [(x["record_a"], x["record_b"]) for x in body["locks"]] == [
        ("L1", "R2")
    ]
    assert body["locks"][0]["time_diff_seconds"] == 50

    # 解除：v3，锁定清单清空，恢复自由最优
    client.post(
        "/drills/SN/locks/unlock",
        json={"record_a": "R2", "record_b": "L1"},
    )
    body = client.get("/drills/SN/snapshots/3").json()
    assert body["locks"] == []
    assert [(p["record_a"], p["record_b"]) for p in body["pairs"]] == [
        ("L1", "R1"), ("L2", "R2"),
    ]

    versions = [v["version"] for v in client.get("/drills/SN/snapshots").json()["versions"]]
    assert versions == [0, 1, 2, 3]


def test_retransmit_and_failed_write_create_no_snapshot(client):
    _seed(client)
    # 同内容重传不推进版本 -> 无新快照
    client.put(
        "/drills/SN/logs/L1",
        json=_log("L1", "A", "B", "2026-10-04T00:00:00+00:00"),
    )
    versions = [v["version"] for v in client.get("/drills/SN/snapshots").json()["versions"]]
    assert versions == [0, 1]

    # 失败的锁定（非候选）回滚 -> 无新快照
    r = client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "L2"})
    assert r.status_code == 409
    versions = [v["version"] for v in client.get("/drills/SN/snapshots").json()["versions"]]
    assert versions == [0, 1]


def test_snapshot_lock_invalidation_rollback_leaves_no_snapshot(client):
    _seed(client)
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R1"})
    # 更正使锁定失效 -> 整次写入回滚，版本与快照都不新增
    r = client.put(
        "/drills/SN/logs/R1",
        json=_log("R1", "B", "A", "2026-10-04T00:01:00+00:00"),
    )
    assert r.status_code == 409
    versions = [v["version"] for v in client.get("/drills/SN/snapshots").json()["versions"]]
    assert versions == [0, 1, 2]
    assert client.get("/drills/SN/result").json()["version"] == 2


def test_seal_creates_no_snapshot_but_sealed_read_from_current_state(client):
    _seed(client)
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R1"})
    client.post("/drills/SN/seal")

    versions = [v["version"] for v in client.get("/drills/SN/snapshots").json()["versions"]]
    assert versions == [0, 1, 2]  # 封存未产生新快照

    # 封存状态从现态读取：旧快照也显示 sealed=True
    for v in versions:
        assert client.get(f"/drills/SN/snapshots/{v}").json()["sealed"] is True

    # 封存后仍可回看
    assert client.get("/drills/SN/snapshots/0").status_code == 200


# --------------------------------------------------------------------------
# 错误处理
# --------------------------------------------------------------------------
def test_snapshot_errors_for_unknown_drill_and_missing_version(client):
    r = client.get("/drills/NOPE/snapshots/0")
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"
    r = client.get("/drills/NOPE/snapshots")
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"
    r = client.get("/drills/NOPE/snapshots/compare?from_version=0&to_version=1")
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"

    client.post(
        "/drills", json={"drill_id": "SN", "allowed_time_diff_seconds": 10}
    )
    r = client.get("/drills/SN/snapshots/5")
    assert r.status_code == 404
    assert r.json()["error"] == "snapshot_version_not_found"

    # 比较中任一版本未留存 -> 404
    r = client.get("/drills/SN/snapshots/compare?from_version=0&to_version=5")
    assert r.status_code == 404
    assert r.json()["error"] == "snapshot_version_not_found"
    r = client.get("/drills/SN/snapshots/compare?from_version=5&to_version=0")
    assert r.status_code == 404
    assert r.json()["error"] == "snapshot_version_not_found"


# --------------------------------------------------------------------------
# 两版比较
# --------------------------------------------------------------------------
def test_compare_same_version_is_empty_diff(client):
    _seed(client)
    r = client.get("/drills/SN/snapshots/compare?from_version=1&to_version=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pairs"] == {"added": [], "removed": []}
    assert body["unpaired"] == {"added": [], "removed": []}
    assert body["locks"] == {"added": [], "removed": []}
    assert body["time_diff_changes"] == []
    assert body["total_time_diff_seconds_from"] == 50
    assert body["total_time_diff_seconds_to"] == 50


def test_compare_writes_lock_and_unlock(client):
    _seed(client)  # v1: 两对
    # v2：写入 L3/R3 一对
    client.post("/drills/SN/logs", json={"logs": [
        _log("L3", "C", "D", "2026-10-04T00:02:00+00:00"),
        _log("R3", "D", "C", "2026-10-04T00:02:10+00:00"),
    ]})
    # v3：锁定 (L1,R2)，自由匹配只剩 (L2 无候选, R1 无候选) -> 仅 1 对
    client.put("/drills/SN/locks", json={"record_a": "L1", "record_b": "R2"})
    # v4：解除
    client.post(
        "/drills/SN/locks/unlock",
        json={"record_a": "L1", "record_b": "R2"},
    )

    # v1 -> v2：新增配对 (L3,R3)
    body = client.get(
        "/drills/SN/snapshots/compare?from_version=1&to_version=2"
    ).json()
    added = [(p["record_a"], p["record_b"]) for p in body["pairs"]["added"]]
    assert added == [("L3", "R3")]
    assert body["pairs"]["removed"] == []
    assert body["unpaired"] == {"added": [], "removed": []}
    assert body["locks"] == {"added": [], "removed": []}
    assert body["total_time_diff_seconds_from"] == 50
    assert body["total_time_diff_seconds_to"] == 60

    # v2 -> v3：锁定加入；自由对 (L1,R1)/(L2,R2) 移除，未配对新增 L2/R1
    body = client.get(
        "/drills/SN/snapshots/compare?from_version=2&to_version=3"
    ).json()
    assert [(x["record_a"], x["record_b"]) for x in body["locks"]["added"]] == [
        ("L1", "R2")
    ]
    assert body["locks"]["removed"] == []
    added = [(p["record_a"], p["record_b"]) for p in body["pairs"]["added"]]
    removed = [(p["record_a"], p["record_b"]) for p in body["pairs"]["removed"]]
    assert added == [("L1", "R2")]
    assert removed == [("L1", "R1"), ("L2", "R2")]
    assert body["unpaired"]["added"] == ["L2", "R1"]
    assert body["unpaired"]["removed"] == []

    # v3 -> v4：锁定移除；配对恢复
    body = client.get(
        "/drills/SN/snapshots/compare?from_version=3&to_version=4"
    ).json()
    assert [(x["record_a"], x["record_b"]) for x in body["locks"]["removed"]] == [
        ("L1", "R2")
    ]
    added = sorted((p["record_a"], p["record_b"]) for p in body["pairs"]["added"])
    removed = sorted((p["record_a"], p["record_b"]) for p in body["pairs"]["removed"])
    assert added == [("L1", "R1"), ("L2", "R2")]
    assert removed == [("L1", "R2")]
    assert body["unpaired"]["removed"] == ["L2", "R1"]

    # 反向比较 v4 -> v3：增删互换
    back = client.get(
        "/drills/SN/snapshots/compare?from_version=4&to_version=3"
    ).json()
    assert (
        [(p["record_a"], p["record_b"]) for p in back["pairs"]["added"]]
        == [(p["record_a"], p["record_b"]) for p in body["pairs"]["removed"]]
    )
    assert (
        sorted((p["record_a"], p["record_b"]) for p in back["pairs"]["removed"])
        == sorted((p["record_a"], p["record_b"]) for p in body["pairs"]["added"])
    )
    assert [(x["record_a"], x["record_b"]) for x in back["locks"]["added"]] == [
        ("L1", "R2")
    ]


def test_compare_time_diff_change_is_listed_separately(client):
    # 两条互指日志配对；更正其中一条的时间（仍在允许时差内），
    # 同一记录对保持配对但时差变化 -> 单列 time_diff_changes。
    client.post(
        "/drills", json={"drill_id": "TD", "allowed_time_diff_seconds": 50}
    )
    client.put(
        "/drills/TD/logs/1",
        json=_log("1", "A", "B", "2026-10-04T00:00:00+00:00"),
    )
    client.put(
        "/drills/TD/logs/2",
        json=_log("2", "B", "A", "2026-10-04T00:00:05+00:00"),
    )
    client.put(
        "/drills/TD/logs/2",
        json=_log("2", "B", "A", "2026-10-04T00:00:08+00:00"),
    )
    body = client.get(
        "/drills/TD/snapshots/compare?from_version=2&to_version=3"
    ).json()
    assert body["pairs"] == {"added": [], "removed": []}
    assert body["time_diff_changes"] == [
        {
            "record_a": "1",
            "record_b": "2",
            "time_diff_seconds_from": 5,
            "time_diff_seconds_to": 8,
        }
    ]
    assert body["total_time_diff_seconds_from"] == 5
    assert body["total_time_diff_seconds_to"] == 8


def test_review_does_not_change_current_state(client):
    _seed(client)
    before = client.get("/drills/SN/result").json()
    client.get("/drills/SN/snapshots/0")
    client.get("/drills/SN/snapshots/1")
    client.get("/drills/SN/snapshots/compare?from_version=0&to_version=1")
    after = client.get("/drills/SN/result").json()
    assert before == after
    versions = [v["version"] for v in client.get("/drills/SN/snapshots").json()["versions"]]
    assert versions == [0, 1]


# --------------------------------------------------------------------------
# 升级：既有演练从当前版本开始留存，之前版本不补造
# --------------------------------------------------------------------------
def test_upgrade_seeds_only_current_version_for_existing_drill(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("QSO_DB_PATH", tmp.name)

    # 1) 用“旧版”库结构（无 snapshots 表）手工造一个 v3 既有演练。
    conn = sqlite3.connect(tmp.name)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE drills (
            drill_id TEXT PRIMARY KEY, allowed_diff_secs REAL NOT NULL
            CHECK (allowed_diff_secs >= 0), version INTEGER NOT NULL DEFAULT 0,
            sealed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            sealed_at TEXT);
        CREATE TABLE logs (
            drill_id TEXT NOT NULL, record_no TEXT NOT NULL, station TEXT NOT NULL,
            counterpart TEXT NOT NULL, ts_micros INTEGER NOT NULL,
            ts_iso TEXT NOT NULL, band TEXT NOT NULL, mode TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (drill_id, record_no));
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
    conn.execute(
        "INSERT INTO drills VALUES ('OLD', 50, 3, 0, 't0', NULL)"
    )
    conn.execute(
        "INSERT INTO results VALUES ('OLD', 3, "
        "'[{\"record_a\": \"L1\", \"record_b\": \"R1\", "
        "\"time_diff_seconds\": 5.0}]', '[]', 5.0, 't3')"
    )
    conn.execute("INSERT INTO locks VALUES ('OLD', 'L1', 'R1', 't2')")
    conn.commit()
    conn.close()

    # 2) 升级启动：幂等建表 + 仅补留当前版本快照。
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/drills/OLD/snapshots")
        versions = [v["version"] for v in r.json()["versions"]]
        assert versions == [3]  # 0/1/2 不用现态补造

        r = c.get("/drills/OLD/snapshots/3")
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == 3
        assert body["pairs"][0]["record_a"] == "L1"
        assert [(x["record_a"], x["record_b"]) for x in body["locks"]] == [
            ("L1", "R1")
        ]

        assert c.get("/drills/OLD/snapshots/0").status_code == 404
        assert c.get(
            "/drills/OLD/snapshots/compare?from_version=0&to_version=3"
        ).status_code == 404

        # 新演练行为正常，0 版可回看；升级后真正推进版本才有新快照
        c.post(
            "/drills",
            json={"drill_id": "NEW", "allowed_time_diff_seconds": 10},
        )
        assert c.get("/drills/NEW/snapshots/0").status_code == 200
        assert c.get("/drills/NEW/snapshots/1").status_code == 404

        # 比较未留存历史版本与已留存当前版本，明确报错
        r = c.get(
            "/drills/OLD/snapshots/compare?from_version=2&to_version=3"
        )
        assert r.status_code == 404
        assert r.json()["error"] == "snapshot_version_not_found"

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass
