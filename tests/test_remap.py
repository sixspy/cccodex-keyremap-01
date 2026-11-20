# -*- coding: utf-8 -*-
"""单键重映夹具：有效映射、校验失败回滚、并发双点争用、凭证只读核证。

口径：甲路径（同事务）。删入站 + 插正式 + 写凭证同库提交，失败两侧回滚。
"""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import app as appmod
from app import app, connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _client():
    c = TestClient(app)
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return c, {"Authorization": "Bearer " + tok}


def _count(table):
    with connect() as conn:
        return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


def _remap(c, h, key):
    return c.post("/api/remap", headers=h, json={"old_key": key})


# ---------------------------------------------------------------- 1 有效映射落成

def test_valid_remap_moves_key_from_ingress_to_formal():
    c, h = _client()
    assert any(x["old_key"] == "OLD-B1" for x in c.get("/api/ingress", headers=h).json()["items"])
    formal_before = _count("formal_entities")
    ingress_before = _count("ingress_entities")

    r = _remap(c, h, "OLD-B1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entity_key"] == "NEW-200"
    assert body["map_version"] == "m-2026.11"
    assert body["ingress_remaining"] == 0
    assert body["proof_id"].startswith("rp-")

    # 正式区确有新实体键（且挂在生效版本下）
    formal = c.get("/api/formal", headers=h).json()["items"]
    hits = [f for f in formal if f["mapped_from"] == "OLD-B1"]
    assert len(hits) == 1
    assert hits[0]["entity_key"] == "NEW-200"
    assert hits[0]["version"] == "m-2026.11"
    assert hits[0]["id"] == body["formal_id"]

    # 入站已无该旧键
    ingress = c.get("/api/ingress", headers=h).json()["items"]
    assert not any(x["old_key"] == "OLD-B1" for x in ingress)
    assert _count("formal_entities") == formal_before + 1
    assert _count("ingress_entities") == ingress_before - 1

    # 留下可查的重映凭证
    proofs = c.get("/api/remap-proofs", headers=h, params={"old_key": "OLD-B1"}).json()["items"]
    assert len(proofs) == 1
    p = proofs[0]
    assert p["proof_id"] == body["proof_id"]
    assert (p["old_key"], p["new_key"], p["map_version"]) == ("OLD-B1", "NEW-200", "m-2026.11")
    assert p["verified"] is True and p["readonly"] is True
    assert p["formal_ids"] == [body["formal_id"]]

    # 互证页不再报入站残留
    mutual = c.get("/api/map-versions/m-2026.11/mutual-proof", headers=h).json()
    assert mutual["verified"] is True, mutual["checks"]
    assert all(x["checks"]["ingress_absent"] for x in mutual["proofs"])
    assert not any(x["old_key"] == "OLD-B1" for x in mutual["ingress_pending"])


def test_seeded_formal_row_is_backed_by_proof():
    """老库既有正式行必须有同口径凭证，版级互证才闭合。"""
    c, h = _client()
    mutual = c.get("/api/map-versions/m-2026.11/mutual-proof", headers=h).json()
    assert mutual["orphan_formal"] == []
    proof_a1 = [p for p in mutual["proofs"] if p["old_key"] == "OLD-A1"]
    assert proof_a1 and proof_a1[0]["verified"] is True


# ---------------------------------------------------------------- 2 校验失败闭

def test_validation_failure_rolls_back_both_sides(monkeypatch):
    c, h = _client()
    formal_before = _count("formal_entities")
    ingress_before = _count("ingress_entities")
    proofs_before = _count("remap_proof")

    original = appmod.validate_mapping_key

    def boom(conn, mapping, version, formal_id):
        raise appmod.RemapError(422, "映射键校验失败：注入夹具")

    monkeypatch.setattr(appmod, "validate_mapping_key", boom)
    r = _remap(c, h, "OLD-B2")
    assert r.status_code == 422
    assert "映射键校验失败" in r.json()["detail"]

    # 正式零增、入站不动、凭证零增：同事务两侧回滚
    assert _count("formal_entities") == formal_before
    assert _count("ingress_entities") == ingress_before
    assert _count("remap_proof") == proofs_before
    ingress = c.get("/api/ingress", headers=h).json()["items"]
    assert any(x["old_key"] == "OLD-B2" for x in ingress)
    formal = c.get("/api/formal", headers=h).json()["items"]
    assert not any(f["mapped_from"] == "OLD-B2" for f in formal)

    # 放开后同一旧键仍可正常落成（失败没有把数据绞死）
    monkeypatch.setattr(appmod, "validate_mapping_key", original)
    r2 = _remap(c, h, "OLD-B2")
    assert r2.status_code == 200, r2.text


def test_validation_failure_on_empty_new_key(monkeypatch):
    c, h = _client()
    formal_before = _count("formal_entities")
    ingress_before = _count("ingress_entities")
    with connect() as conn:
        conn.execute("UPDATE mappings SET new_key='' WHERE version='m-2026.11' AND old_key='OLD-B1'")
        conn.commit()
    r = _remap(c, h, "OLD-B1")
    assert r.status_code == 422
    assert _count("formal_entities") == formal_before
    assert _count("ingress_entities") == ingress_before


# ---------------------------------------------------------------- 3 并发同旧键

def test_concurrent_remap_same_old_key_single_winner():
    c, h = _client()
    results = []
    barrier = threading.Barrier(2)

    def worker():
        cli = TestClient(app)
        tokh = {"Authorization": "Bearer " + cli.post(
            "/api/login", json={"username": "admin", "password": "123456"}).json()["token"]}
        barrier.wait()
        results.append(cli.post("/api/remap", headers=tokh, json={"old_key": "OLD-B1"}))

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]
    winner = next(r for r in results if r.status_code == 200).json()

    # 正式区只有一行（无双进）
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM formal_entities WHERE mapped_from='OLD-B1' AND version='m-2026.11'").fetchall()
        assert len(rows) == 1
        assert rows[0]["entity_key"] == "NEW-200"
        assert conn.execute(
            "SELECT COUNT(*) c FROM remap_proof WHERE old_key='OLD-B1'").fetchone()["c"] == 1
        # 争用登记留下且指向胜出凭证
        cont = conn.execute("SELECT * FROM remap_contention WHERE old_key='OLD-B1'").fetchall()
        assert len(cont) == 1
        assert cont[0]["winner_proof_id"] == winner["proof_id"]

    assert _count("ingress_entities") >= 1  # OLD-B2 / OLD-C9 仍在，不误删


def test_contention_recorded_for_repeat_click():
    c, h = _client()
    first = _remap(c, h, "OLD-B2")
    assert first.status_code == 200
    second = _remap(c, h, "OLD-B2")
    assert second.status_code == 409
    with connect() as conn:
        cont = conn.execute("SELECT * FROM remap_contention WHERE old_key='OLD-B2'").fetchall()
    assert len(cont) == 1
    assert cont[0]["winner_proof_id"] == first.json()["proof_id"]
    assert _count("formal_entities") == 2  # 种子 NEW-100 + 本次 NEW-200


# ---------------------------------------------------------------- 4 库级约束 / 只读

def test_ingress_row_cannot_be_inserted_after_formal():
    with connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO ingress_entities(old_key,source,received_at) VALUES (?,?,?)",
                         ("OLD-A1", "回灌", "2026-11-20T00:00:00"))
        conn.rollback()


def test_formal_insert_rejected_while_ingress_row_exists():
    with connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) "
                         "VALUES (?,?,?,?)", ("NEW-999", "OLD-B1", "m-2026.11", "2026-11-20T00:00:00"))
        conn.rollback()


def test_proof_is_immutable_readonly():
    c, h = _client()
    pid = _remap(c, h, "OLD-B1").json()["proof_id"]
    with connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE remap_proof SET new_key='HACK' WHERE proof_id=?", (pid,))
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM remap_proof WHERE proof_id=?", (pid,))
        conn.rollback()
    assert c.get(f"/api/remap-proofs/{pid}", headers=h).json()["new_key"] == "NEW-200"
    assert c.get("/api/remap-proofs/nope", headers=h).status_code == 404


def test_conflict_queue_and_no_ingress_no_write():
    c, h = _client()
    before = _count("formal_entities")
    assert _remap(c, h, "OLD-ZZ").status_code == 409
    conflicts = c.get("/api/conflicts", headers=h).json()["items"]
    assert any(x["old_key"] == "OLD-ZZ" and x["reason"] == "无映射" for x in conflicts)
    assert _count("formal_entities") == before

    # 有映射但无入站行：不落库
    with connect() as conn:
        conn.execute("DELETE FROM ingress_entities WHERE old_key='OLD-B1'")
        conn.commit()
    assert _remap(c, h, "OLD-B1").status_code == 409
    assert _count("formal_entities") == before


# ---------------------------------------------------------------- 5 重启不复活

def test_reinit_does_not_resurrect_remapped_ingress_key():
    c, h = _client()
    assert _remap(c, h, "OLD-B1").status_code == 200
    init_db()
    init_db()
    ingress = c.get("/api/ingress", headers=h).json()["items"]
    assert not any(x["old_key"] == "OLD-B1" for x in ingress)
    assert _count("remap_proof") == 2  # 种子凭证 + 本次
    formal = c.get("/api/formal", headers=h).json()["items"]
    assert len([f for f in formal if f["mapped_from"] == "OLD-B1"]) == 1
    mutual = c.get("/api/map-versions/m-2026.11/mutual-proof", headers=h).json()
    assert mutual["verified"] is True, mutual["checks"]
