
import hashlib
import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = Path(os.environ.get("KEYREMAP_DB", str(DATA / "app.db")))
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 重映落成口径：全局只走“甲路径 —— 同事务”。
# 删入站行、插正式行、写重映凭证在同一个 SQLite 事务里提交，任何一步失败两侧一起回滚。
# 本仓不实现 remap_draft 影子草稿路径，避免两种正确性口径混用。
REMAP_MODE = "甲-同事务"
DB_TIMEOUT = 15.0


class RemapError(Exception):
    """重映失败；失败原因已在各自事务里落库（冲突队列 / 争用登记）。"""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------- schema / seed

SCHEMA = """
CREATE TABLE IF NOT EXISTS map_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL,
  effective_from TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mappings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT NOT NULL,
  old_key TEXT NOT NULL,
  new_key TEXT NOT NULL,
  kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS formal_entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_key TEXT NOT NULL,
  mapped_from TEXT NOT NULL,
  version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conflicts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  old_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingress_entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  old_key TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remap_proof(
  proof_id TEXT PRIMARY KEY,
  old_key TEXT NOT NULL,
  new_key TEXT NOT NULL,
  map_version TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remap_contention(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  old_key TEXT NOT NULL,
  map_version TEXT NOT NULL,
  at TEXT NOT NULL,
  detail TEXT NOT NULL,
  winner_proof_id TEXT
);
CREATE TABLE IF NOT EXISTS app_meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_proof_old_key ON remap_proof(old_key);
CREATE INDEX IF NOT EXISTS ix_proof_version ON remap_proof(map_version);
CREATE INDEX IF NOT EXISTS ix_contention_old_key ON remap_contention(old_key);
CREATE INDEX IF NOT EXISTS ix_formal_mapped_from ON formal_entities(mapped_from);
"""

# 凭证一旦落库不可修改、不可删除。
PROOF_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_remap_proof_no_update
BEFORE UPDATE ON remap_proof
BEGIN SELECT RAISE(ABORT, '重映凭证不可变'); END;
CREATE TRIGGER IF NOT EXISTS trg_remap_proof_no_delete
BEFORE DELETE ON remap_proof
BEGIN SELECT RAISE(ABORT, '重映凭证不可变'); END;
"""

# 硬约束的库级兜底：正式行写入时，同一旧键不得仍留在入站表。
# 甲路径先删入站再插正式，因此正常重映能通过；任何绕过事务顺序的写法会被拒绝。
INVARIANT_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_formal_requires_ingress_cleared
BEFORE INSERT ON formal_entities
WHEN EXISTS (SELECT 1 FROM ingress_entities WHERE old_key = NEW.mapped_from)
BEGIN SELECT RAISE(ABORT, '正式写入前入站旧键未清除'); END;
CREATE TRIGGER IF NOT EXISTS trg_ingress_blocked_when_formal
BEFORE INSERT ON ingress_entities
WHEN EXISTS (SELECT 1 FROM formal_entities WHERE mapped_from = NEW.old_key)
BEGIN SELECT RAISE(ABORT, '旧键已进正式区，不得再入站'); END;
"""


def _seed_base(conn):
    if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"]:
        return
    conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)",
                 ("m-2026.11", "生效", "2026-11-01"))
    conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
        ("m-2026.11", "OLD-A1", "NEW-100", "一对一"),
        ("m-2026.11", "OLD-B1", "NEW-200", "多对一"),
        ("m-2026.11", "OLD-B2", "NEW-200", "多对一"),
    ])
    conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                 ("NEW-100", "OLD-A1", "m-2026.11", "2026-11-10T09:00:00"))
    conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)",
                 ("OLD-C9", "无映射", "挂起"))


def _seed_ingress_once(conn):
    """给老库补入站夹具，只执行一次；绝不复活已重映的旧键。"""
    if conn.execute("SELECT 1 FROM app_meta WHERE k='seed_ingress_v2'").fetchone():
        return
    # 历史种子：NEW-100 已在正式区，补一条同口径凭证，保证版级互证闭合。
    if conn.execute("SELECT 1 FROM formal_entities WHERE entity_key=? AND mapped_from=? LIMIT 1",
                    ("NEW-100", "OLD-A1")).fetchone():
        conn.execute(
            "INSERT OR IGNORE INTO remap_proof(proof_id,old_key,new_key,map_version,at) VALUES (?,?,?,?,?)",
            ("rp-seed-0001", "OLD-A1", "NEW-100", "m-2026.11", "2026-11-10T09:00:00"))
    for old_key in ("OLD-B1", "OLD-B2", "OLD-C9"):
        if conn.execute("SELECT 1 FROM remap_proof WHERE old_key=? LIMIT 1", (old_key,)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM formal_entities WHERE mapped_from=? LIMIT 1", (old_key,)).fetchone():
            continue
        conn.execute("INSERT OR IGNORE INTO ingress_entities(old_key,source,received_at) VALUES (?,?,?)",
                     (old_key, "入站通道", "2026-11-18T08:00:00"))
    conn.execute("INSERT OR REPLACE INTO app_meta(k,v) VALUES ('seed_ingress_v2','1')")


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript(SCHEMA)
        # 唯一索引防止同一旧键在同一版本重复进正式区（并发兜底）。
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_formal_version_from "
                         "ON formal_entities(version, mapped_from)")
        except sqlite3.IntegrityError:
            # 老库若已有重复落成，保留数据不静默删除；靠事务锁与凭证核证暴露。
            pass
        conn.executescript(PROOF_TRIGGERS)
        _seed_base(conn)
        _seed_ingress_once(conn)
        conn.executescript(INVARIANT_TRIGGERS)


@app.on_event("startup")
def _s():
    init_db()


# ---------------------------------------------------------------- helpers

def effective_version(conn) -> str | None:
    row = conn.execute(
        "SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
    return row["version"] if row else None


def validate_mapping_key(conn, mapping, version: str, formal_id: int) -> None:
    """映射键校验。故意放在两次写入之后、同一事务之内调用：
    校验失败时正式行与入站删除都必须一起回滚（夹具会注入失败来验证这一点）。"""
    new_key = (mapping["new_key"] or "").strip()
    old_key = (mapping["old_key"] or "").strip()
    if not old_key:
        raise RemapError(422, "映射键校验失败：old_key 为空")
    if not new_key:
        raise RemapError(422, "映射键校验失败：new_key 为空")
    rows = conn.execute("SELECT COUNT(*) c FROM mappings WHERE version=? AND old_key=?",
                        (version, old_key)).fetchone()["c"]
    if rows != 1:
        raise RemapError(422, "映射键校验失败：旧键映射不唯一")
    if formal_id is None:
        raise RemapError(422, "映射键校验失败：正式行未生成")


def _record_contention(conn, old_key: str, version: str, now: str, detail: str, proof_id: str | None):
    conn.execute(
        "INSERT INTO remap_contention(old_key,map_version,at,detail,winner_proof_id) VALUES (?,?,?,?,?)",
        (old_key, version, now, detail, proof_id))


def remap_commit(old_key: str) -> dict:
    """甲路径：删入站 + 插正式 + 写凭证，同一事务同败同成。

    BEGIN IMMEDIATE 先拿写锁，双击/并发重映同一旧键只会有一个进入正式区，
    输家在 remap_contention 留争用登记，不会产生第二行正式数据。
    """
    old_key = (old_key or "").strip()
    if not old_key:
        raise RemapError(422, "old_key 不能为空")
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise RemapError(503, f"数据库忙，重映未执行：{exc}") from exc
        try:
            version = effective_version(conn)
            if not version:
                raise RemapError(404, "无生效映射版本")
            mapping = conn.execute(
                "SELECT * FROM mappings WHERE version=? AND old_key=? ORDER BY id LIMIT 1",
                (version, old_key)).fetchone()
            if not mapping:
                conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)",
                             (old_key, "无映射", "挂起"))
                conn.commit()
                raise RemapError(409, "无映射，已进冲突队列")

            prior = conn.execute(
                "SELECT proof_id FROM remap_proof WHERE map_version=? AND old_key=? "
                "ORDER BY at DESC, rowid DESC LIMIT 1", (version, old_key)).fetchone()
            existing_formal = conn.execute(
                "SELECT id FROM formal_entities WHERE version=? AND mapped_from=? LIMIT 1",
                (version, old_key)).fetchone()
            if existing_formal:
                _record_contention(conn, old_key, version, now,
                                   "重复重映：正式区已有该旧键的落成记录，本次未写入",
                                   prior["proof_id"] if prior else None)
                conn.commit()
                raise RemapError(409, "重映争用：正式区已有该旧键的落成记录，本次未落库")

            ingress = conn.execute("SELECT id FROM ingress_entities WHERE old_key=?", (old_key,)).fetchone()
            if not ingress:
                if prior:
                    _record_contention(conn, old_key, version, now,
                                       "重复重映：旧键已由既有凭证胜出，本次未写入", prior["proof_id"])
                    conn.commit()
                    raise RemapError(409, f"重映争用：旧键已由凭证 {prior['proof_id']} 胜出，本次未落库")
                conn.commit()
                raise RemapError(409, "无入站旧键行，未落库")

            # ---- 同一事务内的两侧写入 ----
            deleted = conn.execute("DELETE FROM ingress_entities WHERE old_key=?", (old_key,))
            if deleted.rowcount != 1:
                raise RemapError(409, "入站旧键行已被其它重映占用")
            cur = conn.execute(
                "INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                (mapping["new_key"], old_key, version, now))
            formal_id = cur.lastrowid
            # 校验在两侧写入之后执行；失败 => 下面统一回滚，正式零增、入站不动。
            validate_mapping_key(conn, mapping, version, formal_id)
            proof_id = "rp-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
            conn.execute(
                "INSERT INTO remap_proof(proof_id,old_key,new_key,map_version,at) VALUES (?,?,?,?,?)",
                (proof_id, old_key, mapping["new_key"], version, now))
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    finally:
        conn.close()

    # 回执从已提交状态现查，避免“回执成功、互证仍报入站残留”。
    with connect() as check:
        proof = check.execute("SELECT * FROM remap_proof WHERE proof_id=?", (proof_id,)).fetchone()
        remaining = check.execute("SELECT COUNT(*) c FROM ingress_entities WHERE old_key=?",
                                  (old_key,)).fetchone()["c"]
        formal = check.execute("SELECT * FROM formal_entities WHERE id=?", (formal_id,)).fetchone()
    return {
        "ok": True,
        "mode": REMAP_MODE,
        "proof_id": proof["proof_id"],
        "old_key": proof["old_key"],
        "entity_key": proof["new_key"],
        "map_version": proof["map_version"],
        "formal_id": formal["id"],
        "committed_at": proof["at"],
        "ingress_remaining": remaining,
    }


def _proof_detail(conn, row) -> dict:
    """只读核证：凭证 + 正式实体 + 入站残留 + 映射版本，现场对账。"""
    formal_rows = conn.execute(
        "SELECT * FROM formal_entities WHERE entity_key=? AND mapped_from=? AND version=? ORDER BY id",
        (row["new_key"], row["old_key"], row["map_version"])).fetchall()
    ingress_rows = conn.execute("SELECT COUNT(*) c FROM ingress_entities WHERE old_key=?",
                                (row["old_key"],)).fetchone()["c"]
    version = conn.execute("SELECT * FROM map_versions WHERE version=?",
                           (row["map_version"],)).fetchone()
    checks = {
        "proof_present": True,
        "formal_present": len(formal_rows) > 0,
        "formal_single": len(formal_rows) == 1,
        "formal_version_match": bool(formal_rows) and all(
            r["version"] == row["map_version"] for r in formal_rows),
        "ingress_absent": ingress_rows == 0,
        "map_version_known": version is not None,
    }
    return {
        "proof_id": row["proof_id"],
        "old_key": row["old_key"],
        "new_key": row["new_key"],
        "map_version": row["map_version"],
        "at": row["at"],
        "formal_ids": [r["id"] for r in formal_rows],
        "ingress_rows": ingress_rows,
        "checks": checks,
        "verified": all(checks.values()),
        "readonly": True,
        "mode": REMAP_MODE,
    }


# ---------------------------------------------------------------- auth / pages

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
        p = c.execute("SELECT COUNT(*) c FROM remap_proof").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n,
            "proofs": p, "remap_mode": REMAP_MODE, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?",
                        (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}


@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}


@app.get("/api/ingress")
def ingress_list(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        version = effective_version(c)
        rows = c.execute("""
            SELECT i.id, i.old_key, i.source, i.received_at, m.new_key, m.kind
            FROM ingress_entities i
            LEFT JOIN mappings m ON m.old_key=i.old_key AND m.version=?
            ORDER BY i.id""", (version,)).fetchall()
        return {"items": [dict(r) for r in rows], "map_version": version}


class RemapIn(BaseModel):
    old_key: str


@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    try:
        return remap_commit(body.old_key)
    except RemapError as exc:
        raise HTTPException(exc.status, exc.detail)


@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute(
            "SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}


@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}


@app.get("/api/remap-contention")
def contention_list(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute(
            "SELECT * FROM remap_contention ORDER BY id DESC").fetchall()]}


@app.get("/api/remap-proofs")
def proof_list(old_key: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        if old_key:
            rows = c.execute("SELECT * FROM remap_proof WHERE old_key=? ORDER BY at DESC, rowid DESC",
                             (old_key,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM remap_proof ORDER BY at DESC, rowid DESC").fetchall()
        return {"items": [_proof_detail(c, r) for r in rows], "readonly": True, "mode": REMAP_MODE}


@app.get("/api/remap-proofs/{proof_id}")
def proof_detail(proof_id: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM remap_proof WHERE proof_id=?", (proof_id,)).fetchone()
        if not row:
            raise HTTPException(404, "重映凭证不存在")
        return _proof_detail(c, row)


@app.get("/api/map-versions/{version}/mutual-proof")
def version_mutual_proof(version: str, authorization: str | None = Header(default=None)):
    """版级互证：只读汇总映射册、正式实体、入站、冲突、争用与逐条凭证核证。"""
    require_user(authorization)
    with connect() as c:
        ver = c.execute("SELECT * FROM map_versions WHERE version=?", (version,)).fetchone()
        if not ver:
            raise HTTPException(404, "映射版本不存在")
        proofs = c.execute("SELECT * FROM remap_proof WHERE map_version=? ORDER BY at, rowid",
                           (version,)).fetchall()
        proof_items = [_proof_detail(c, p) for p in proofs]
        pairs = {(p["old_key"], p["new_key"]) for p in proofs}
        formal_rows = [dict(r) for r in c.execute(
            "SELECT * FROM formal_entities WHERE version=? ORDER BY id", (version,)).fetchall()]
        orphans = [r for r in formal_rows if (r["mapped_from"], r["entity_key"]) not in pairs]
        ingress_pending = [dict(r) for r in c.execute("""
            SELECT i.id, i.old_key, i.received_at, m.new_key
            FROM ingress_entities i
            JOIN mappings m ON m.old_key=i.old_key AND m.version=?
            WHERE NOT EXISTS (
              SELECT 1 FROM formal_entities f WHERE f.version=? AND f.mapped_from=i.old_key)
            ORDER BY i.id""", (version, version)).fetchall()]
        conflicts = [dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id").fetchall()]
        contentions = [dict(r) for r in c.execute(
            "SELECT * FROM remap_contention WHERE map_version=? ORDER BY id", (version,)).fetchall()]
        mapping_rows = [dict(r) for r in c.execute(
            "SELECT * FROM mappings WHERE version=? ORDER BY id", (version,)).fetchall()]
        checks = [
            {"name": "映射版本存在", "ok": True, "detail": ver["status"]},
            {"name": "每条正式行都有对应凭证", "ok": not orphans,
             "detail": f"孤儿正式行 {len(orphans)}"},
            {"name": "每条凭证核证通过", "ok": all(x["verified"] for x in proof_items),
             "detail": f"凭证 {len(proof_items)} 条"},
            {"name": "凭证旧键无入站残留",
             "ok": all(x["checks"]["ingress_absent"] for x in proof_items),
             "detail": f"核证残留 {sum(x['ingress_rows'] for x in proof_items)} 行"},
        ]
        return {
            "version": version,
            "status": ver["status"],
            "readonly": True,
            "mode": REMAP_MODE,
            "verified": all(x["ok"] for x in checks),
            "checks": checks,
            "proofs": proof_items,
            "mappings": mapping_rows,
            "formal": formal_rows,
            "orphan_formal": orphans,
            "ingress_pending": ingress_pending,
            "conflicts": conflicts,
            "contention": contentions,
            "counts": {
                "mappings": len(mapping_rows),
                "formal": len(formal_rows),
                "proofs": len(proof_items),
                "ingress_pending": len(ingress_pending),
                "conflicts": len(conflicts),
                "contention": len(contentions),
            },
        }


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
