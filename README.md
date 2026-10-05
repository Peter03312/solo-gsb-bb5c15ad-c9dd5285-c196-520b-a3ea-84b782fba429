# 应急无线电演练通联日志互证服务

为应急无线电演练提供台站通联日志（QSO Log）的**互证配对**服务：
各台站上报自己的通联日志，系统把“**相互指认、波段一致、模式一致、
时差不超过演练允许值**”的两条日志配成一对，并按全场最优规则求解，
返回配对清单与未配对记录。

技术栈：**Python 3.11 + FastAPI + SQLite（WAL）**，容器一键启动，
无外部依赖。

---

## 1. 配对规则与优化目标

两条日志可配对，当且仅当：

1. **相互指认**：一条的本台呼号 == 另一条的对方呼号，反之亦然；
2. **波段相同**；
3. **模式相同**；
4. 两条**带时区时间**的绝对时差 `|t1 − t2|` 不超过演练配置的允许时差；
5. 每条日志最多参与一次配对。

全场求解按以下优先级取**唯一确定**的最优解：

1. **最大化配对数量**；
2. 配对数相同则**最小化总时差**（所有配对时差之和）；
3. 仍并列时取**字典序最小**的配对列表：先把每对内部的两个记录号
   按升序排列，再把所有配对按升序排列，按列表字典序比较取最小。

算法为二分图上的**最小费用最大流**（Dijkstra + potential），
见 [`app/matching.py`](app/matching.py)：相互指认关系天然构成二分图，
费用中对时差主项放大 `2^E` 倍并叠加“配对键等级奖励”，使一次求流
同时满足上述三级目标。配套 150 组随机用例与穷举枚举结果逐一对照
（`tests/test_matching.py`）。

### 人工锁定（复核员互证）

演练复核员可在**未封存**演练中，按两个记录号**锁定**一对互证候选；
锁定对会**强制入选**互证结果：

- 锁定仅接受同时满足下列条件的两条日志：**现存**、**相互指认**、
  **波段相同**、**模式相同**、**时差不超过演练允许值**，且两条记录
  **均未被其他锁定占用**；
- 重算时**先保留全部锁定对**（其记录移出自由匹配池），再对剩余日志
  沿用原“配对数最多 → 总时差最小 → 记录号字典序最小”规则；
  **即使锁定使全场配对数下降，也绝不替换锁定对**；
- **重复锁定同一对视为重传**（幂等，记录号顺序任意），不推进版本；
- **解除不存在的锁定明确报错**（`404 lock_not_found`）；
- 单条或批量**更正/写入使任一锁定失效**（记录被改到不互指、波段/模式
  变化、时差超限等）时，**整次写入拒绝、状态不变**（`409
  lock_invalidated`，事务回滚）；
- **锁定状态变化与结果、版本原子提交**；无状态变化不推进版本；
  **封存后拒绝一切锁定操作**（仍可查询锁定清单与最终结果）；
- 无锁定时结果与历史行为**完全一致**，现有写入与结果查询的
  请求/响应保持兼容。

### 版本快照（复核员按版本回看 / 比较）

每个真正推进的版本都在**同一事务**留存一份不可变快照，供复核员回看与
两版比较：

- **创建演练即留存 0 版快照**（空场：无配对、无未配对、无锁定）；
- 每次**日志写入/更正真正推进版本**、**锁定/解除锁定推进版本**时，
  同事务留存该版的**配对（含时差）、未配对记录、总时差与锁定清单**；
- **同内容重传、失败回滚（含整批拒绝、锁定校验失败、解除不存在的锁定）、
  封存均不产生新快照**；封存本身不改版本，快照中的**封存状态仍从现态读取**；
- **升级既有服务时**：旧库在启动初始化阶段为每个演练从**升级时的当前
  版本**开始留存一份快照，**更早的版本不用现态补造**（取回/比较均明确
  报错）；升级后的版本推进照常逐版留存；
- 取回快照与比较版本都是**只读**操作，不改变当前状态、版本与锁定。

### 版本语义

- 每条日志以 `(演练号, 记录号)` 唯一标识；
- **同内容重传不推进版本**（呼号大小写、首尾空白等规范化后相同即视为重传）；
- **异内容视为更正**：更新日志后**重算全场**，`version + 1`，
  配对结果与新版本在同一事务原子落库；
- **锁定/解除成功各 `version + 1`**，锁定状态、配对结果与版本同事务
  原子提交；重复锁定同一对为重传，不推进版本；
- 批量写入整体在一个事务内：任一条非法（含批次内记录号重复）则
  **整批回滚**；若写入/更正会使**任一既有锁定失效**，同样整批拒绝、
  状态不变（`409 lock_invalidated`）；
- 并发写入通过 SQLite `BEGIN IMMEDIATE` 串行化，版本号与配对结果
  **原子更新**，不会出现版本与结果错位；
- **封存**演练后拒绝一切写入（返回 `409`），但仍可查询最终结果；
  封存本身不改变配对结果与版本，也**不产生新版本快照**；
- 每个真正推进的版本（含创建时的 0 版）在同事务留存**版本快照**
  （配对/未配对/总时差/锁定清单）；重传与失败回滚不留存，
  升级既有库仅从升级时当前版本开始留存、旧版本不补造。

---

## 2. 一键启动（Docker Compose，推荐）

```bash
docker compose up -d --build
```

启动后：

- 服务地址：`http://127.0.0.1:8080`
- 健康检查：`GET http://127.0.0.1:8080/health`
- 交互式 API 文档（Swagger UI）：`http://127.0.0.1:8080/docs`
- OpenAPI 描述：`http://127.0.0.1:8080/openapi.json`

容器启动时自动完成**依赖安装与数据初始化**（建库、建表、开启 WAL，
幂等）。SQLite 数据保存在命名卷 `qso-data`（容器内 `/data/qso.db`），
容器重建数据不丢。

停止 / 清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷
```

### 仅用 Docker

```bash
docker build -t qso-log-attestation:1.2.0 .
docker run -d --name qso -p 8080:8080 -v qso-data:/data qso-log-attestation:1.2.0
```

---

## 3. 本地直接运行（无需 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 可选：初始化数据库（不执行也会在服务启动时自动初始化）
python -m scripts.init_db

# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

跑测试：

```bash
pip install pytest httpx
pytest -q
```

一键演示（需要先启动服务，自动创建演练、写入、更正、封存并打印结果）：

```bash
python scripts/demo.py http://127.0.0.1:8080
```

---

## 4. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QSO_HOST` | `0.0.0.0` | 服务监听地址 |
| `QSO_PORT` | `8080` | 服务监听端口（Compose 宿主机映射端口也可用它覆盖） |
| `QSO_DB_PATH` | `/data/qso.db` | SQLite 数据库文件路径（会在同目录产生 `-wal`/`-shm` 文件） |

Compose 改宿主机端口示例：

```bash
QSO_PORT=9000 docker compose up -d --build   # 宿主机 http://127.0.0.1:9000
```

---

## 5. HTTP API

所有请求/响应均为 JSON，时间字段使用 **ISO 8601 且必须携带时区**，
如 `2026-10-04T08:00:00+08:00` 或 `2026-10-04T00:00:00Z`
（内部统一换算为 UTC，微秒精度比较时差）。

可直接导入 [`examples/requests.http`](examples/requests.http) 执行；
也可用 [`scripts/demo.py`](scripts/demo.py) 看完整调用序列。

### 5.1 演练管理

#### `POST /drills` —— 创建演练

```bash
curl -X POST http://127.0.0.1:8080/drills \
  -H 'Content-Type: application/json' \
  -d '{"drill_id": "DRILL-2026", "allowed_time_diff_seconds": 10}'
```

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `drill_id` | string | 演练号，唯一；字母/数字/`_`/`-`/`.` |
| `allowed_time_diff_seconds` | number | 允许时差（秒），**必须非负** |

响应 `201`：

```json
{
  "drill_id": "DRILL-2026",
  "allowed_time_diff_seconds": 10.0,
  "version": 0,
  "sealed": false,
  "created_at": "2026-10-04T12:00:00+00:00",
  "sealed_at": null
}
```

#### `GET /drills` —— 列出演练
#### `GET /drills/{drill_id}` —— 查询演练配置/状态（未知演练 `404`）
#### `POST /drills/{drill_id}/seal` —— 封存演练（幂等封存，重复封存 `409`）

### 5.2 日志写入

#### `PUT /drills/{drill_id}/logs/{record_no}` —— 写入/更正单条日志

路径上的 `record_no` 为准。响应直接返回写入后的最新互证结果。

```bash
curl -X PUT http://127.0.0.1:8080/drills/DRILL-2026/logs/R1 \
  -H 'Content-Type: application/json' \
  -d '{
    "record_no": "R1",
    "station": "BH1AAA",
    "counterpart": "BH2BBB",
    "time": "2026-10-04T08:00:00+08:00",
    "band": "40m",
    "mode": "SSB"
  }'
```

字段：`record_no`（演练内唯一记录号）、`station`（本台呼号）、
`counterpart`（对方呼号）、`time`（**带时区**时间）、`band`、`mode`。
呼号/波段/模式自动 `strip + 大写` 规范化。

#### `POST /drills/{drill_id}/logs` —— 批量写入/更正

```bash
curl -X POST http://127.0.0.1:8080/drills/DRILL-2026/logs \
  -H 'Content-Type: application/json' \
  -d '{"logs": [ { ... }, { ... } ]}'
```

整体一个事务；批次内同记录号重复或任一条非法 => 整批拒绝回滚。

#### `GET /drills/{drill_id}/logs` —— 查看演练全部日志

### 5.3 结果查询

#### `GET /drills/{drill_id}/result` —— 查询互证结果（封存后即最终结果）

```json
{
  "drill_id": "DRILL-2026",
  "version": 3,
  "sealed": false,
  "pairs": [
    {"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0},
    {"record_a": "R3", "record_b": "R6", "time_diff_seconds": 8.0}
  ],
  "unpaired": ["R7"],
  "total_time_diff_seconds": 13.0,
  "recomputed_at": "2026-10-04T12:00:05.123456+00:00"
}
```

- `pairs`：每对记录号已内部升序，列表整体升序；
- `unpaired`：未参与任何配对的记录号（按写入顺序）；
- `version`：当前全场版本，同内容重传不变，异内容更正 +1。

### 5.4 人工锁定（复核员互证）

记录号通过请求体传递（避免 `/`、`.` 等记录号字符进入路径）；
`record_a` / `record_b` 顺序任意，服务端按字典序内部升序存储。

#### `PUT /drills/{drill_id}/locks` —— 锁定一对互证候选

```bash
curl -X PUT http://127.0.0.1:8080/drills/DRILL-2026/locks \
  -H 'Content-Type: application/json' \
  -d '{"record_a": "R1", "record_b": "R2"}'
```

响应 `200`（同时回传当前锁定对、锁定清单与最新互证结果）：

```json
{
  "lock": {
    "record_a": "R1",
    "record_b": "R2",
    "time_diff_seconds": 5.0,
    "locked_at": "2026-10-04T12:00:05.000000+00:00"
  },
  "locks": [
    {"record_a": "R1", "record_b": "R2",
     "time_diff_seconds": 5.0,
     "locked_at": "2026-10-04T12:00:05.000000+00:00"}
  ],
  "result": {
    "drill_id": "DRILL-2026",
    "version": 4,
    "sealed": false,
    "pairs": [{"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0}],
    "unpaired": ["R3"],
    "total_time_diff_seconds": 5.0,
    "recomputed_at": "2026-10-04T12:00:05.000000+00:00"
  }
}
```

锁定成功时**版本 +1**，锁定对强制出现在 `result.pairs` 中（即使全场
配对数因此下降）。重复锁定同一对（含颠倒顺序）视为**重传**：返回
`200`，`version` 不变。

#### `POST /drills/{drill_id}/locks/unlock` —— 解除锁定

```bash
curl -X POST http://127.0.0.1:8080/drills/DRILL-2026/locks/unlock \
  -H 'Content-Type: application/json' \
  -d '{"record_a": "R2", "record_b": "R1"}'
```

解除成功响应结构同上（`lock` 为 `null`、`locks` 为解除后的清单），
全场按无此锁定重算且**版本 +1**。锁定不存在返回
`404 lock_not_found`。

#### `GET /drills/{drill_id}/locks` —— 查询锁定清单及最新结果

```json
{
  "drill_id": "DRILL-2026",
  "locks": [
    {"record_a": "R1", "record_b": "R2",
     "time_diff_seconds": 5.0,
     "locked_at": "2026-10-04T12:00:05.000000+00:00"}
  ],
  "result": { "...": "与 GET /result 完全一致的最新互证结果" }
}
```

锁定操作的校验规则与错误见下节错误表。

### 5.5 版本快照（按版本回看 / 两版比较）

#### `GET /drills/{drill_id}/snapshots` —— 列出已留存快照的版本号

```bash
curl http://127.0.0.1:8080/drills/DRILL-2026/snapshots
```

```json
{ "drill_id": "DRILL-2026", "versions": [0, 1, 2, 3] }
```

0 版在创建演练时即存在；升级前的旧版本不在此清单中（不可回看）。

#### `GET /drills/{drill_id}/snapshots/{version}` —— 取回指定版本快照

```bash
curl http://127.0.0.1:8080/drills/DRILL-2026/snapshots/2
```

```json
{
  "drill_id": "DRILL-2026",
  "version": 2,
  "sealed": false,
  "pairs": [
    {"record_a": "R1", "record_b": "R2", "time_diff_seconds": 5.0}
  ],
  "unpaired": ["R3"],
  "total_time_diff_seconds": 5.0,
  "locks": [
    {"record_a": "R1", "record_b": "R2",
     "time_diff_seconds": 5.0,
     "locked_at": "2026-10-04T12:00:05.000000+00:00"}
  ],
  "snapshot_at": "2026-10-04T12:00:05.234567+00:00"
}
```

- `pairs` / `unpaired` / `locks` 为该版**留存时**的内容，之后不再改变；
- `sealed` 是唯一的现态字段：封存后回看任意版本都显示 `true`；
- 未知演练 `404 drill_not_found`；版本未留存 `404 snapshot_not_found`。

#### `GET /drills/{drill_id}/snapshots/{from}/diff/{to}` —— 比较两个已留存版本

按记录号**稳定排序**列出配对、未配对记录与锁定对的新增/移除，
**同一记录对的时差变化单列**：

```bash
curl http://127.0.0.1:8080/drills/DRILL-2026/snapshots/1/diff/3
```

```json
{
  "drill_id": "DRILL-2026",
  "from_version": 1,
  "to_version": 3,
  "pairs": {
    "added":   [{"record_a": "R3", "record_b": "R6", "time_diff_seconds": 8.0}],
    "removed": [],
    "time_diff_changed": [
      {"record_a": "R1", "record_b": "R2",
       "from_time_diff_seconds": 5.0, "to_time_diff_seconds": 9.0}
    ]
  },
  "unpaired": {
    "added": ["R7"],
    "removed": ["R3", "R6"]
  },
  "locks": {
    "added":   [{"record_a": "R1", "record_b": "R2",
                 "time_diff_seconds": 9.0,
                 "locked_at": "2026-10-04T12:00:05.000000+00:00"}],
    "removed": []
  },
  "total_time_diff_seconds": {"from": 5.0, "to": 17.0, "delta": 12.0}
}
```

- 配对以 `(record_a, record_b)` 身份比较：`added` 取 **to（新）版**内容，
  `removed` 取 **from（旧）版**内容；两版都在但时差不同的对进入
  `pairs.time_diff_changed`，不重复计入新增/移除；
- `unpaired.added/removed` 为记录号集合差异，按记录号升序；
- `locks.added/removed` 按锁定对身份比较（锁定对仅时差变化时不出现，
  其时差变化已在 `pairs.time_diff_changed` 体现），列表按记录对升序；
- `total_time_diff_seconds` 给出两版总时差及 `delta`（to − from）；
- **相同版本**比较合法，各差异节均为空、`delta` 为 0（该版本仍须已留存）；
- 两个版本都必须已留存，否则 `404 snapshot_not_found`；未知演练
  `404 drill_not_found`。比较为只读，不改变任何当前状态。

---

## 6. 错误响应与状态码

业务错误统一形如 `{"error": "<错误码>", "message": "<中文说明>"}`；
请求体校验错误为 FastAPI 标准 `422`（`detail` 中含明确中文原因）。

| 场景 | 状态码 | 错误码 / 说明 |
| --- | --- | --- |
| 允许时差为负数 | `422` | `允许时差必须为非负数（秒）` |
| 时间缺时区信息 | `422` | `时间必须携带时区信息，例如 2026-10-04T08:00:00+08:00` |
| 本台呼号与对方呼号相同 | `422` | `本台呼号与对方呼号不能相同` |
| 记录号/呼号/波段/模式为空 | `422` | 对应字段不能为空 |
| 未知演练（查询/写入/封存/锁定） | `404` | `drill_not_found` |
| 演练号已存在 | `409` | `drill_exists` |
| 写入/封存/锁定已封存演练 | `409` | `drill_sealed`（封存后查询仍 `200`） |
| 批次内记录号重复 | `409` | `duplicate_record_in_batch` |
| 锁定的两个记录号相同 | `422` | `lock_same_record` |
| 锁定引用了不存在的记录 | `404` | `lock_record_missing` |
| 两条日志未相互指认 / 波段或模式不一致 / 时差超限 | `409` | `lock_not_candidate` |
| 锁定涉及的记录已被其他锁定占用 | `409` | `record_already_locked` |
| 解除不存在的锁定 | `404` | `lock_not_found` |
| 写入/更正使任一既有锁定失效 | `409` | `lock_invalidated`（整次写入拒绝、状态不变） |
| 取回/比较未留存版本（含升级前旧版本） | `404` | `snapshot_not_found` |

---

## 7. 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由、异常处理、lifespan 初始化
│   ├── schemas.py       # 请求/响应模型与输入校验（时区/负值/同呼号）
│   ├── database.py      # SQLite 持久化、事务、版本/封存/锁定/版本快照
│   ├── matching.py      # 配对核心算法（二分图最小费用最大流，支持强制锁定）
│   └── config.py        # 环境变量配置
├── scripts/
│   ├── init_db.py       # 数据初始化（建库/建表/WAL/旧库快照回填，幂等）
│   └── demo.py          # 端到端调用演示（含版本回看/比较，纯标准库）
├── tests/               # 算法单测（含穷举对照）+ HTTP 集成测试（含快照）
├── examples/requests.http
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

### 并发与持久化说明

- SQLite 启用 **WAL**：写入串行的同时允许并发读取，封存/重算期间
  结果查询不被阻塞；
- 每个写请求使用独立连接执行 `BEGIN IMMEDIATE`，配合 `busy_timeout=30s`
  排队获取写锁；`version`、日志变更、配对结果在同一事务提交；
- 集成测试 `tests/test_api.py::test_concurrent_writes_are_serialized`
  以 20 个并发线程验证最终版本严格为 20、无丢失更新。
