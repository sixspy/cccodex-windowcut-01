# 业务窗关账

> 遵守 `ops/底座生成前必读_效率与技术栈.md`（**≥3× 厚度**）。  
> FastAPI + Vue 3 + SQLite；交付须 `docker compose up -d --build` 一键起。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立。

## 骨架一句话

事件按业务窗裁切，迟到进旁路或拒收，关窗后物化结果并与窗口对账。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 业务窗关账 |
| 目录 | `08-windowcut` |
| 接口 | http://127.0.0.1:11011 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 厚度（相对薄壳 ≥3×）

| 项 | 目标 |
|----|------|
| 业务 LOC | ≥900（目标 1000+） |
| 后端具名模块 | `db` / `auth` / `locks` / `windows_svc` / `ingest_svc` / `late_svc` / `materialize_svc` / `reconcile_svc` / `audit_svc` / `hold_svc` / `reopen_svc` |
| 前端视图 | 业务窗 / 入账 / 迟到桶 / 物化 / 对账 / 审计争用 |
| 空桩 | hold 晋升、迟到补入、分区物化、窗重开 |

## 为何能出多题

开窗关窗、迟到策略、窗重开、部分物化失败回滚、旁路暂存 vs 拒收、争用锁与审计。

## 互斥切口候选

- 迟到拒收 vs 进迟到桶
- 物化失败整窗回滚 vs 保留成功分区
- 窗重开作废重算 vs 增量补丁合并
- 切版旁路暂存 vs 开始瞬间拒收

## 封口分流册（本仓定稿口径）

本仓择定 **甲·旁路暂存**，当天一锤定音，禁止改口：

- 关窗两段式：`开启 → 封口中 → 已关闭`；封口中可续跑收口。
- 封口进行中新到事件只写 `hold_staging`（状态 `待放行`），正式 `events` 零增；
  暂存失败与封口态同事务回滚，不留半成品。
- 封口期不得混用拒收路径；写路径与册定口径不符即拒写并记审计 `封口偏了`。
- 两路抢写同一窗口共用 `window:{id}` 窗锁，后到只记 `contention_log` + 审计。
- 五处同态：`hold` 列表、正式事件、拒收凭证、物化对账、审计；对账接口
  单独披露 `hold_pending` 与 `seal_ledger`，物化只算正式窗内合计。

相关接口：`GET /api/seal-routing`、`POST /api/seal-routing`、
`GET /api/seal-ledger`。表：`seal_routing_ledger`、`seal_ledger`。

回归用例：`tests/test_seal_routing.py`（封口泄漏、双人抢关争用、暂存失败回滚、
五处同态、改口拒写）。

## 启动（一键）

```powershell
cd projects/08-windowcut
docker compose up -d --build
```

浏览器：http://127.0.0.1:11011/  
健康检查：GET http://127.0.0.1:11011/api/health

本机开发：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10605
python -m pytest -q
```
