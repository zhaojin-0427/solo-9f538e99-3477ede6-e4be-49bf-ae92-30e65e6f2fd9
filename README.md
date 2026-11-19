# 空运危险品保管链 API

面向断网后乱序补传场景的保管链（chain of custody）服务。数据员可以据此判断：
在某个时刻之前，哪些交接仍然可信、断链发生在哪里、当前可确认的保管方是谁。

技术栈：**FastAPI + SQLite（WAL 模式）+ Uvicorn**，可通过 `docker compose` 一键启动。

## 核心规则

1. **固定起始保管方**：创建运单时登记 `origin_party`，初始可确认保管方即该方。
2. **批量提交**：每笔扫描携带 `scan_id`、正整数 `revision`、UTC 时刻 `ts_utc`、
   交出方 `giver`、接收方 `receiver`。时刻必须带时区偏移（如 `...Z`、`+08:00`），统一归一化为 UTC 存储。
3. **撤回**：只提交 `scan_id` 与更高的 `revision`，`event_type=WITHDRAWAL`，
   不得携带时刻或当事方；撤回只能针对已存在的扫描 ID。
4. **修订裁决**：
   - 同一 `scan_id` 仅采用**最高修订号**的版本；更低修订号提交返回 `409 stale`。
   - 同修订号、异文（时刻/交出方/接收方/类型不同）**整批拒绝**（`409`），已提交数据原样保留。
   - 同修订号、同文重复提交幂等，不计入写入数。
5. **链核对**（按 UTC 时刻升序，乱序补传自动归位）：
   - 交接的 `giver` 必须等于当前保管方（首笔对照起始保管方，其后对照上一笔的 `receiver`），
     不匹配即为 **GAP 断链**。
   - **同一时刻存在多笔交接时不猜测顺序**：该时刻整组标记为 **SIMULTANEOUS** 断链，组内全部未证实。
   - 断链之后的所有扫描一律 `verified=false`，直到补报、更正或撤回补齐缺口；
     重算后断链位置可以前移、后移或消失。
6. **原子性与一致性**：
   - 每个批次在单个 `BEGIN IMMEDIATE` 事务内完成，任何冲突整批回滚，**失败保留旧链**。
   - 查询只读已提交快照（WAL），交错的扫描提交与查询永远看不到半写状态或混合修订。
   - 数据持久化在 SQLite 文件中，进程重启后状态不变。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务： http://localhost:8000
# 文档： http://localhost:8000/docs
# 控制台：http://localhost:8000/
```

数据库保存在命名卷 `chain-data`（容器内 `/data/chain.db`），容器重启不丢数据。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：APP_DB_PATH=/custom/path/chain.db
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/waybills` | 创建运单并固定起始保管方 |
| GET | `/api/waybills` | 列出全部运单 |
| POST | `/api/waybills/{waybill_id}/scans/batch` | 批量提交交接 / 更正 / 撤回 |
| GET | `/api/waybills/{waybill_id}/chain` | 查询可信前缀、断链原因、当前保管方 |
| POST | `/api/bills` | 对已结束的 UTC 日生成日结账单（同日重试返回原单） |
| GET | `/api/bills` | 按出账顺序列出全部账单摘要 |
| GET | `/api/bills/{service_date}` | 按服务日查询已出具的账单 |
| GET | `/docs` | Swagger UI，可直接录入与查询 |

### 交接批次示例

```json
{
  "scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z",
     "giver": "HKG-WAREHOUSE", "receiver": "CX-CARGO"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T10:30:00Z",
     "giver": "CX-CARGO", "receiver": "PVG-WAREHOUSE"}
  ]
}
```

### 撤回示例（修订号必须更高）

```json
{"scans": [{"scan_id": "S2", "revision": 2, "event_type": "WITHDRAWAL"}]}
```

### 查询响应示例（发生断链时）

```json
{
  "waybill_id": "AWB-001",
  "origin_party": "HKG-WAREHOUSE",
  "current_custodian": "CX-CARGO",
  "verified_prefix": [ {"scan_id": "S1", "verified": true, "...": "..."} ],
  "unverified_scans": [ {"scan_id": "S3", "verified": false,
                         "unverified_reason": "GAP"} ],
  "chain_intact": false,
  "break_reason": "GAP",
  "break_detail": "giver 'X' does not hold custody; expected 'CX-CARGO' ...",
  "break_scan_ids": ["S3"]
}
```

`break_reason` 取值：`NONE` / `GAP`（保管方不承接）/ `SIMULTANEOUS`（同时刻多笔，顺序不可判定）。
`current_custodian` 只沿已证实交接推进，因此断链后仍可信地停留在最后一个已证实接收方。

## 日结账单

晚到的交接扫描可能改变已结算日的保管时长，账单因此按“只增不改、差额后补”处理：

- **出账范围**：一次只结算一个已结束的 UTC 日。首日可任选，此后必须逐日续开，跳日返回 `409`。
  对同一天重复请求返回最初冻结的账单原单，内容逐字节不变。
- **计费口径**：只计相邻两笔**可信**交接之间的时长（即链查询中的 `verified_prefix`），
  归前一笔交接的 `receiver`；跨 UTC 午夜时按日切开分别计入两天。断链后的扫描不计时。
- **账单内容**：当日各运单、各保管方的 `custody` 时长；以及 `adjustments`——此前已出账日
  重新汇总后的未入账净差额，逐条注明 `origin_service_date`、`waybill_id`、`custodian`
  和带正负号的 `duration_seconds`。
- **不重复补记**：每次出账把各已出账日按 `(服务日, 运单, 保管方)` 的累计已入账时长更新为
  最新真相；后续账单只补尚未入账的差额，已出具的账单永不改动。
- **并发**：汇总在只读快照中完成，仅在短暂写入阶段加锁并用变更序号做乐观校验；汇总期间
  扫描照常提交，若快照后被扫描改变则本次出账返回 `409` 供重试。
- **失败原子性**：任何冲突或失败都不会留下账单，待入账差额留到下次重试；账单存于 SQLite，
  进程重启后仍可查询。

### 出账示例

```json
POST /api/bills        {"service_date": "2026-01-02"}
```

```json
{
  "service_date": "2026-01-02",
  "sequence": 2,
  "issued_at": "2026-11-19T20:31:27.305407Z",
  "custody": [],
  "adjustments": [
    {"origin_service_date": "2026-01-01", "waybill_id": "AWB-001",
     "custodian": "HKG-WAREHOUSE", "duration_seconds": -7200}
  ],
  "custody_total_seconds": 0,
  "adjustment_total_seconds": -7200,
  "total_seconds": -7200
}
```

## 测试

```bash
pip install pytest httpx
pytest -q
```

共 42 个用例，覆盖：乱序补传归位、时区归一化、最高修订裁决、同修订异文整批回滚、
撤回与补报修复断链、GAP/SIMULTANEOUS 检测、输入校验、真实线程下的写串行化、
交错读不到混合修订，进程重启持久化，以及日结账单的跨午夜切分、同日重试、
差额调整、不重复补记、失败不留单、出账期间扫描并发提交。
