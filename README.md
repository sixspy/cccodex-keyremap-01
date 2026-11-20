# 实体键桥

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 实体键桥 |
| 目录 | `07-keyremap` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10911 |
| 库 | `data/app.db`（可用环境变量 `KEYREMAP_DB` 覆盖路径） |
| 账号 | admin / 123456 |

## 重映落成口径（甲路径 / 同事务，全局唯一）

**本仓只实现一种正确性口径，不实现 remap_draft 影子草稿路径，两路不混用。**

单键重映 `POST /api/remap {old_key}` 在**同一个 SQLite 事务**（`BEGIN IMMEDIATE`）内完成：

1. `DELETE FROM ingress_entities WHERE old_key=?`（入站旧键行）
2. `INSERT INTO formal_entities(... entity_key=new_key, version=生效版本 ...)`（正式区新实体键）
3. 映射键校验 `validate_mapping_key`（非空 / 唯一 / 正式行已生成）
4. `INSERT INTO remap_proof(proof_id, old_key, new_key, map_version, at)`（重映凭证）
5. `COMMIT`

任一步失败 → 统一 `ROLLBACK`：**正式零增、入站不动、凭证零增**。回执成功即表示两侧已同库提交；回执里的 `ingress_remaining` 与互证页都从**已提交状态现查**，不会出现“回执成功、互证仍报入站残留”。

### 重映凭证

`remap_proof` 表（proof_id + old_key + new_key + map_version + at），行级不可变：库级触发器拒绝 `UPDATE`/`DELETE`。

### 并发与争用

`BEGIN IMMEDIATE` 先取写锁 + `UNIQUE INDEX ux_formal_version_from(version, mapped_from)` + 触发器等三层兜底：
同一旧键并发/重复重映只允许一个胜出（200），其余返回 409 并写入 `remap_contention`
（old_key + map_version + at + detail + winner_proof_id）。正式区不会双进，入站不会被误删。

### 库级硬约束

- `trg_formal_requires_ingress_cleared`：入站仍存在同旧键时，禁止插正式行（禁止“正式已写、入站未删”）。
- `trg_ingress_blocked_when_formal`：旧键已进正式区后禁止再回灌入站。

## 只读核证接口

| 接口 | 说明 |
|------|------|
| `GET /api/remap-proofs[?old_key=]` | 凭证清单，逐条现场核证（正式行、版本、入站残留） |
| `GET /api/remap-proofs/{proof_id}` | 单张凭证只读核证 |
| `GET /api/map-versions/{version}/mutual-proof` | 版级互证：映射册 / 正式实体 / 入站待重映 / 冲突 / 争用 / 孤儿正式行 |

协同页：映射版本与映射册、入站列表、执行重映、正式区、冲突与争用、互证与凭证核证。

## 本地夹具（SQLite）

`python -m pytest -q` 覆盖：

1. 有效映射下重映后：正式出现 `map_version` 下的新实体键，入站无该旧键，凭证可核证且互证闭合。
2. 人为校验失败：正式零增、入站不动、凭证零增（同事务回滚），之后同一旧键仍可正常落成。
3. 并发双点同旧键：仅一胜出（200/409），正式仅一行，留争用登记指向胜出凭证。
4. 库级约束：正式未删入站时禁止再插正式；已落成旧键禁止回灌；凭证不可改删。

## 启动（一键）

`powershell
cd projects/07-keyremap
docker compose up -d --build
`

浏览器：http://127.0.0.1:10911/  
健康检查：GET http://127.0.0.1:10911/api/health（返回 `remap_mode`）

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10604
python -m pytest -q
`
