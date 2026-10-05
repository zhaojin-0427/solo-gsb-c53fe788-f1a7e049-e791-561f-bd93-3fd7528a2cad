# 离线矢量瓦片编译 API

基于 **Python + FastAPI + SQLite + Shapely + mapbox-vector-tile** 的离线矢量瓦片服务。
上传 **EPSG:4326 GeoJSON** 图层后，服务以**不可变 revision（修订版本）**保存；
请求瓦片时按 **Web Mercator (EPSG:3857)** 投影、在线编译为 **Mapbox Vector Tile (.mvt)**，
并保证**相同输入永远产生字节级一致的瓦片和 ETag**。

---

## 1. 功能特性

| 能力 | 说明 |
| --- | --- |
| 发布图层 | `POST /datasets` 上传 GeoJSON `FeatureCollection`（multipart 或原始 JSON body），创建数据集并发布 **revision 1** |
| 不可变版本 | 每次发布写入一个新的 revision；历史 revision 永不被覆盖，可通过 `?revision=N` 固定取瓦片 |
| 乐观并发控制 | 发布新版本必须携带 `expected_revision`，与当前 head 不一致时返回 **409 Conflict**；head 比较、revision 插入与 head 更新在单个跨进程有效的 `BEGIN IMMEDIATE` 事务内原子完成，多 worker 并发追加也只有一方成功 |
| 瓦片编译 | `GET /datasets/{id}/tiles/{z}/{x}/{y}.mvt`，支持 **z = 0–14** 的 XYZ 瓦片 |
| 点选（identify） | `GET /datasets/{id}/tiles/{z}/{x}/{y}/identify?px=&py=&radius_px=[&revision=]`，在瓦片内点击处（源像素 `0≤px,py<256`、半径 `0–8 px`）按**编译后可见几何**裁决命中：面内距离为 0，线 / 点须落在圆形半径内；逐源要素编译，**保留 0 基源序号**与 4096 网格上的**亚像素点击位置**，无 ID / 字符串 ID / 重复 ID 不合并、量化后消失的几何不命中、缓冲边缘按同半径裁决，结果按距离再按源序号排序（见 [§4.9](#49-点选identify)） |
| 离线增量清单 | `GET /datasets/{id}/tile-diff?from_revision=&to_revision=&z=`，按 **x、y 升序**返回两个修订间**实际 MVT 字节发生变化**的瓦片坐标及两端 ETag（ETag 与钉版瓦片逐字节一致）；候选范围由两份 GeoJSON 的全部要素几何（含无 ID / 重复 ID）外扩 **8 px 缓冲**求并集得到，新增 / 删除 / 移动 / 属性变化及缓冲邻瓦片都不漏报，源文档不同但瓦片编码相同不报，**z=14 局部更新只枚举局部邻瓦片** |
| 投影 | EPSG:4326 经纬度 → 球面 Web Mercator（R = 6378137），瓦片内坐标系为 4096 网格、Y 轴向下 |
| 8 像素缓冲裁剪 | 点 / 线 / 面先裁剪到瓦片外扩 **8 px（128/4096 单位）**的边界，保证跨瓦片连续 |
| 0.5 px 拓扑简化 | 以 **0.5 px（8/4096 单位）**容差做保拓扑 Douglas–Peucker 简化 |
| 4096 量化 | 坐标四舍五入到整数 4096 网格，并清除退化线 / 零面积环 |
| 退化环裁决 | 量化后面环坍缩（合法小多边形的顶点全部落入同一网格点）时**逐环清理、按能否围成有效面积裁决**：外环坍缩仅丢该面、内环坍缩仅去该孔、MultiPolygon 保留其余有效子面；瓦片返回 **200** 与可编码 MVT 而非 500，同瓦片其他要素与既有瓦片字节、钉版 ETag 均不变（见 [§4.8](#48-退化小多边形坍缩环裁决复核)） |
| 稳定排序 | 图层按名称、要素按 ID（数值 → 字符串 → 内容哈希）确定性排序后编码 |
| 字节一致 / ETag | 同一 revision + 同一瓦片坐标，重复请求**字节完全相同、ETag 相同**；已编译瓦片持久化到 SQLite |
| 条件请求 | 支持 `If-None-Match`，命中时返回 **304 Not Modified** |
| 错误处理 | 非法 GeoJSON / 非法几何 / 无效瓦片参数返回 **400**，数据集或版本不存在返回 **404**；点选的非法瓦片号、`px/py/radius_px/revision` 同样返回 **400**（见 [§4.9](#49-点选identify)） |
| 一键启动 | 提供 `Dockerfile` 与 `docker-compose.yml`，数据持久化在命名卷中；两个 worker 在全新卷上同时建库由**非阻塞**文件锁 + 有界重试串行化，两者都会完整启动（见 [§3.2](#32-全新数据卷初始化竞态复核)）；另有进程持续占锁时启动会**在退避预算内明确失败退出而不是悬挂**，解锁后可正常启动（见 [§3.3](#33-持锁--解锁启动复核docker-compose)） |

---

## 2. 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由：发布、版本、瓦片、点选、瓦片增量清单、错误处理
│   ├── tiler.py         # 裁剪 / 简化 / 量化 / 稳定排序 / MVT 编码
│   ├── identify.py      # 点选裁决：逐源编译可见几何，按到亚像素点击点的距离判定命中
│   ├── tilediff.py      # 瓦片增量候选集：几何 mercator 包围盒外扩 8 px 求并集
│   ├── projection.py    # EPSG:4326 -> Web Mercator -> 瓦片 4096 网格
│   ├── geojson.py       # GeoJSON 校验与规范化
│   ├── db.py            # SQLite 存储（datasets / revisions / tile_cache，非阻塞跨进程建库锁与事务）
│   └── config.py        # 环境变量配置
├── data/
│   ├── sample.geojson          # 示例图层（点 / 线 / 面）
│   ├── tiny-polygon.geojson    # 退化小多边形复核 revision 1（含 ~0.1 mm 小三角，见 §4.8）
│   ├── tiny-polygon-v2.geojson # 复核 revision 2：删除小三角、点属性改名
│   └── tiny-polygon-v3.geojson # 复核 revision 3：仅把小三角加回（瓦片与 v2 逐字节相同）
├── scripts/
│   └── hold_init_lock.py # 持锁复核辅助：独占 <db>.init.lock（可定时释放）
├── tests/               # pytest 测试（134 个用例，含点选裁决、瓦片增量清单、量化坍缩环裁决、非阻塞建库锁、初始化竞态与跨进程追加发布竞态回归）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 3. Docker Compose 一键启动（推荐）

```bash
docker compose up -d --build
```

启动后：

- 服务地址：<http://localhost:8000>
- 交互文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/healthz>
- 点选 API（发布数据后即可调用）：
  `GET http://localhost:8000/datasets/{id}/tiles/{z}/{x}/{y}/identify?px=&py=&radius_px=`
  （用法与真实请求 / 响应示例见 [§4.9](#49-点选identify)）
- SQLite 数据库保存在 Docker 命名卷 `tile-data`（容器内 `/data/tiles.db`），容器重建后数据不丢
- 默认以 **2 个 uvicorn worker 进程**启动（`VT_WORKERS=2`），两个进程共享同一个
  SQLite 文件；**首次启动建库由跨进程文件锁串行化、并带有界重试**（见
  [§3.2](#32-全新数据卷初始化竞态复核)），跨进程的追加发布则由 `BEGIN IMMEDIATE`
  事务串行化，可直接按 [§4.2](#42-发布新版本乐观并发控制) 末尾的并发命令复核竞态
- 若**另一个进程持续持有** `/data/tiles.db.init.lock` 排他锁，worker 不会无限等待：
  取锁是**非阻塞尝试**，失败后与 SQLite 忙锁共用同一份重试 / 退避预算，预算耗尽即抛
  `SchemaUnavailable` 明确结束启动；持锁进程释放后重启即可正常建库。可用
  Compose 一键复核，见 [§3.3](#33-持锁--解锁启动复核docker-compose)

常用命令：

```bash
docker compose ps            # 查看状态与健康检查
docker compose logs -f       # 查看日志
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据卷
```

> 如需把数据库放到宿主机目录，可把 `docker-compose.yml` 中的
> `tile-data:/data` 改为 `./data:/data`。

### 3.1 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export VT_DB_PATH=./data/tiles.db          # SQLite 文件路径
# 追加发布的跨进程竞态可像 Docker 一样用多 worker 复核：追加 --workers 2
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

运行测试：

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

### 3.2 全新数据卷初始化竞态复核

两个 worker 在全新卷上同时执行 lifespan 建库时，`PRAGMA busy_timeout` 会在
`PRAGMA journal_mode=WAL` **之前**安装；建库 DDL 全部使用
`CREATE TABLE/INDEX IF NOT EXISTS`，并由容器内 `/data/tiles.db.init.lock` 上的
排他文件锁（`flock`）串行化。取锁使用 **`LOCK_EX | LOCK_NB` 非阻塞尝试**：抢不到锁
（例如另一个进程正在建库、或持锁不放）时立即抛 `InitLockUnavailable`，与 SQLite 的
`database is locked` 忙错误**共用同一份**最多 8 次、指数退避（0.1 s 起，上限 2 s）的
有界预算；抢到锁的进程仍串行建库，且 DDL 异常时在 `finally` 中释放已获取的锁。最后
重新打开连接复核 WAL 模式与全部 schema 对象均可见。因此两个 worker 都会完整启动、建同
一份 schema 且不重复建表；而一个**持续不放锁的外部进程**只会让启动在预算内重试后退
出（见 [§3.3](#33-持锁--解锁启动复核docker-compose)），绝不会卡在阻塞式 `flock` 里
悬挂。

**第 1 步：在全新数据卷上一键启动**（`down -v` 确保卷是空的）：

```bash
docker compose down -v          # 仅复核时需要：删除旧卷，模拟全新数据盘
docker compose up -d --build
```

**第 2 步：确认两个 worker 都完成启动，且没有 `database is locked`**：

```bash
docker compose logs vector-tiles | grep -E "Started server process|startup complete|locked"
```

预期输出（两个进程各一次 `Application startup complete`，且无任何 `locked`）：

```text
INFO:     Started server process [10]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Started server process [11]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

**第 3 步：确认健康检查稳定为 200**（容器状态与连续 HTTP 请求两路复核）：

```bash
docker compose ps
# NAME                      ... STATUS
# offline-vector-tile-api   ... Up (healthy)

for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code} " http://localhost:8000/healthz
done; echo
```

```text
200 200 200 200 200 200 200 200 200 200
```

`/healthz` 的响应体：

```bash
curl -s -i http://localhost:8000/healthz
```

```http
HTTP/1.1 200 OK
content-type: application/json
```

```json
{"status": "ok"}
```

**第 4 步：直接检查卷内数据库——WAL 已启用、四张 schema 对象各只定义一次**：

```bash
docker compose exec vector-tiles python - <<'EOF'
import sqlite3
conn = sqlite3.connect("/data/tiles.db")
print("journal_mode =", conn.execute("PRAGMA journal_mode").fetchone()[0].lower())
rows = conn.execute(
    "SELECT type, name FROM sqlite_master "
    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
).fetchall()
for kind, name in rows:
    print(f"{kind:5s} {name}")
dupes = conn.execute(
    "SELECT name FROM sqlite_master GROUP BY name HAVING COUNT(*) > 1"
).fetchall()
print("duplicate objects:", dupes)
EOF
```

```text
journal_mode = wal
index idx_tile_cache_rev
table datasets
table revisions
table tile_cache
duplicate objects: []
```

**第 5 步（可选）：反复清空卷并启动，多轮复核**：

```bash
for round in 1 2 3; do
  docker compose down -v >/dev/null 2>&1
  docker compose up -d --build >/dev/null 2>&1
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/healthz)
  locked=$(docker compose logs vector-tiles | grep -c 'database is locked' || true)
  echo "round $round: healthz=$code locked_errors=$locked"
done
```

```text
round 1: healthz=200 locked_errors=0
round 2: healthz=200 locked_errors=0
round 3: healthz=200 locked_errors=0
```

初始化相关参数也可以用环境变量调整（见 [§7](#7-配置环境变量)）：
`VT_SQLITE_BUSY_TIMEOUT_MS`（默认 30000）、`VT_INIT_MAX_ATTEMPTS`（默认 8）、
`VT_INIT_RETRY_DELAY_S`（默认 0.1）。

### 3.3 持锁 / 解锁启动复核（Docker Compose）

本节复核修复点：**另一个进程持续持有 `tiles.db.init.lock` 排他锁时，启动不能悬挂**。
worker 的取锁为非阻塞尝试，与 SQLite 忙锁共用 `VT_INIT_MAX_ATTEMPTS` / 退避预算；预算
内锁被释放则两个 worker 串行建库、`/healthz` 返回 200；锁一直不释放则明确抛
`SchemaUnavailable` 并结束启动，容器进入重启退避而不是无限等待。

Compose 提供一个独立的 `init-lock-holder` 服务（放在 `locktest` profile 中，
**普通 `docker compose up` 不会启动它**），它用 `scripts/hold_init_lock.py` 抢同一把
`/data/tiles.db.init.lock` 排他锁：不带秒数时持续持锁直到被 `stop`，带秒数时到时自动
释放（用于复核“预算内释放”）。

**第 1 步：全新卷 + 先启动持锁者，再启动 API（持续占锁）**

```bash
docker compose down -v
docker compose --profile locktest up -d init-lock-holder
docker compose logs init-lock-holder
# vt-init-lock-holder  | holding exclusive lock on /data/tiles.db.init.lock until stopped
docker compose up -d vector-tiles
```

**第 2 步：确认 worker 在退避预算内重试后明确失败，而不是悬挂**

```bash
sleep 10
docker compose logs vector-tiles | grep -E "init_db attempt|SchemaUnavailable|startup failed" | head -20
```

预期日志（每个 worker 先按 0.1 s、0.2 s、0.4 s 退避重试，预算耗尽即抛
`SchemaUnavailable` 并结束启动）：

```text
WARNING:  init_db attempt 1/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.10s
WARNING:  init_db attempt 2/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.20s
WARNING:  init_db attempt 3/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.40s
ERROR:    init_db gave up after 8 attempts: init lock is held by another process: /data/tiles.db.init.lock
app.db.SchemaUnavailable: database initialisation failed after 8 attempts: init lock is held by another process: /data/tiles.db.init.lock
ERROR:    Application startup failed. Exiting.
```

此时端口尚未提供服务，健康检查不通（**不会有任何请求悬挂**）：

```bash
curl -s -o /dev/null -w "%{http_code}\n" --max-time 3 http://localhost:8000/healthz || echo "connection refused (startup not complete)"
```

```text
000
connection refused (startup not complete)
```

容器状态表现为反复重启（`restart: unless-stopped`），但每次启动都是有界的：

```bash
docker compose ps vector-tiles
# NAME                      ... STATUS
# offline-vector-tile-api   ... Restarting (...)
```

**第 3 步：释放锁并重启——两个 worker 串行建好同一份完整 schema，/healthz 返回 200**

```bash
docker compose stop init-lock-holder
docker compose restart vector-tiles
sleep 5
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code} " http://localhost:8000/healthz
done; echo
```

```text
200 200 200 200 200 200 200 200 200 200
```

```bash
docker compose logs vector-tiles | grep -E "Started server process|Application startup complete"
```

```text
INFO:     Started server process [10]
INFO:     Application startup complete.
INFO:     Started server process [11]
INFO:     Application startup complete.
```

两个 worker 看到的是同一份完整 schema（WAL + 三张表 + 索引，各只定义一次），
检查命令与输出与 [§3.2 第 4 步](#32-全新数据卷初始化竞态复核) 相同：

```text
journal_mode = wal
index idx_tile_cache_rev
table datasets
table revisions
table tile_cache
duplicate objects: []
```

**第 4 步（可选）：预算内自动释放——worker 重试若干次后在锁释放时直接建库成功**

把持锁者换成“持锁 2 秒后自动释放”（`run` 覆盖 `command` 追加秒数；注意
`--profile` 要放在子命令之前）。默认 8 次重试、退避 0.1→0.2→0.4→0.8→1.6→2.0…
（累计预算约 7 s）足以覆盖这 2 秒，因此**无需重启 API**：正在重试的 worker 在锁释放
后抢到锁即可完成启动。

```bash
docker compose down -v
docker compose --profile locktest run -d --name vt-lock-timed \
  init-lock-holder \
  python scripts/hold_init_lock.py /data/tiles.db.init.lock 2
docker compose up -d vector-tiles
sleep 8
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/healthz
docker compose logs vector-tiles | grep -E "init_db attempt" | wc -l
docker compose logs vector-tiles | grep -c "Application startup complete"
```

```text
200
# 锁释放前两个 worker 各会出现若干条 init_db attempt 重试警告（具体条数取决于调度，通常 6-10）
2
```

重试日志形如：

```text
WARNING:  init_db attempt 1/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.10s
WARNING:  init_db attempt 2/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.20s
WARNING:  init_db attempt 3/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.40s
...
INFO:     Application startup complete.
```

> 复核结束后清理：`docker compose --profile locktest down -v`。

---

## 4. HTTP API 与 curl 示例

以下示例假设服务运行在 `http://localhost:8000`。

### 4.1 发布图层（创建数据集，revision 1）

使用 `multipart/form-data` 上传（字段 `file` 为 GeoJSON，`layer` 为 MVT 图层名）：

```bash
curl -s -X POST http://localhost:8000/datasets \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "layer=demo"
```

返回 `201 Created`：

```json
{
  "id": "32dca29ff4a24c869d9aa4b4686c26fa",
  "layer": "demo",
  "revision": 1,
  "content_hash": "96b7b15130feb9a517c348cf985e007217e29c456f4baa80b728ee7186408",
  "feature_count": 4,
  "published_at": "2026-09-29T14:21:52Z"
}
```

也可以直接发送原始 GeoJSON body（图层名用 query 参数，省略时为 `default`）：

```bash
curl -s -X POST "http://localhost:8000/datasets?layer=demo" \
  -H "Content-Type: application/geo+json" \
  --data-binary @data/sample.geojson
```

把数据集 ID 存到变量，后续示例复用：

```bash
DS=32dca29ff4a24c869d9aa4b4686c26fa
```

### 4.2 发布新版本（乐观并发控制）

新版本必须携带 `expected_revision`（当前 head 版本号）。成功后 revision 前进为 2：

```bash
curl -s -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "expected_revision=1"
```

如果 `expected_revision` 已过期（head 已被别人推进），返回 **409 Conflict**：

```bash
curl -i -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "expected_revision=1"
```

```http
HTTP/1.1 409 Conflict
```

```json
{
  "error": "revision conflict: expected_revision does not match current head",
  "dataset_id": "32dca29ff4a24c869d9aa4b4686c26fa",
  "expected_revision": 1,
  "current_revision": 2
}
```

客户端应重新拉取 head、合并变更后，再用新的 `expected_revision` 重试。

#### 并发发布（跨进程竞态复核）

乐观锁的比较与写入在单个 `BEGIN IMMEDIATE` 事务内完成（比较 head → 插入新
revision → 更新 head 原子提交，见 `app/db.py` 的 `publish_next_revision`）。
因此即使**两个独立工作进程**以相同的 `expected_revision` 同时提交，也只有一方
得到 `201`，其余方返回结构不变的 `409`（`current_revision` 指向新 head），不会
出现重复版本、半提交或未映射的数据库错误。

下面用 8 个并发请求同时把 `expected_revision=1` 追加到同一数据集（Compose 默认
就是 2 个 worker 进程共享同一库；`xargs -P8` 让请求真正同时发出）：

```bash
seq 8 | xargs -P8 -I{} sh -c '
  curl -s -o /tmp/race-{}.json -w "request %{http_code}\n" \
    -X POST "http://localhost:8000/datasets/$DS/revisions" \
    -F "file=@data/sample.geojson;type=application/geo+json" \
    -F "expected_revision=1"'
```

实测结果——**恰好 1 个 201，7 个 409**（哪一个请求成为 201 取决于调度，每次运行
可能不同）：

```text
request 201
request 409
request 409
request 409
request 409
request 409
request 409
request 409
```

获胜请求的 `201 Created` 响应体：

```json
{
    "id": "25c78460dd9745c5a36c37169b7e3458",
    "layer": "demo",
    "revision": 2,
    "content_hash": "96b7b15130feb9a517c348cf985e007217e29c456f4baa80b728ee7186408",
    "feature_count": 4,
    "published_at": "2026-09-30T06:58:41Z"
}
```

落败请求的 `409 Conflict` 响应（`-i` 同时显示状态行）：

```http
HTTP/1.1 409 Conflict
content-type: application/json
```

```json
{
  "error": "revision conflict: expected_revision does not match current head",
  "dataset_id": "25c78460dd9745c5a36c37169b7e3458",
  "expected_revision": 1,
  "current_revision": 2
}
```

确认库里没有产生额外版本（只有 revision 1、2，head 为 2）：

```bash
curl -s http://localhost:8000/datasets/$DS | python3 -m json.tool
```

```json
{
  "id": "25c78460dd9745c5a36c37169b7e3458",
  "layer": "demo",
  "latest_revision": 2,
  "revisions": [
    { "revision": 1, "...": "..." },
    { "revision": 2, "...": "..." }
  ]
}
```

历史 revision 不可变，仍可按旧版本读取瓦片（响应头 `x-revision: 1`，固定版本为
`immutable` 缓存）：

```bash
curl -s -D - -o /tmp/r1.mvt \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt?revision=1"
```

```http
HTTP/1.1 200 OK
etag: "75dd0d37d05a70e4ed743cb72731f3bb8f3d0a513cfa58a1b473c3e265f4a481"
x-revision: 1
cache-control: public, max-age=31536000, immutable
```

### 4.3 查看数据集与版本列表

```bash
curl -s http://localhost:8000/datasets
curl -s http://localhost:8000/datasets/$DS | python3 -m json.tool
```

### 4.4 获取矢量瓦片

瓦片路径为标准 XYZ 编号（`y=0` 在最北），缩放级别 **0–14**：

```bash
# 0 级世界瓦片
curl -s -o tile-0-0-0.mvt \
  -D - \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
```

响应头：

```http
HTTP/1.1 200 OK
content-type: application/vnd.mapbox-vector-tile
etag: "75dd0d37d05a70e4ed743cb72731f3bb8f3d0a513cfa58a1b473c3e265f4a481"
x-revision: 3
vary: If-None-Match
cache-control: public, no-cache
```

取一个高级别瓦片（例如北京附近的 z=14 瓦片）：

```bash
curl -s -o beijing-z14.mvt \
  "http://localhost:8000/datasets/$DS/tiles/14/13489/6208.mvt"
```

**固定版本取瓦片**（适合长缓存，响应为 `Cache-Control: …, immutable`）：

```bash
curl -s "http://localhost:8000/datasets/$DS/tiles/10/807/401.mvt?revision=1" -o r1.mvt
```

### 4.5 条件请求（If-None-Match → 304）

先用 `-c` 把 ETag 存进 cookie jar，再带上 `If-None-Match`：

```bash
curl -s -o tile.mvt -c cookies.txt \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"

ETAG=$(grep -i etag cookies.txt | awk '{print $NF}')

curl -s -o /dev/null -w "%{http_code}\n" \
  -H "If-None-Match: $ETAG" \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
# 304
```

或者手工复制 ETag：

```bash
curl -i -H 'If-None-Match: "75dd0d37d05a70e4ed743cb72731f3bb8f3d0a513cfa58a1b473c3e265f4a481"' \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
# HTTP/1.1 304 Not Modified（无响应体）
```

### 4.6 离线瓦片增量清单（tile-diff）

当某个数据集从 `from_revision` 更新到 `to_revision` 时，只重新拉取**真正变化的瓦片**
即可完成离线增量更新。端点把两份不可变 GeoJSON 都走一遍既有编译链路，并以**编码后的
MVT 字节**裁决：

1. **候选集（只增不漏）**：对**两个修订的每个要素**，用与瓦片管线完全相同的 Web
   Mercator 投影（含 ±85.05112878° 纬度截断）求 mercator 包围盒，并按该 z 下
   **8 px（128/4096 个瓦片跨度）**向四周外扩，映射为覆盖到的瓦片索引范围后取并集。
   因此新增、删除、移动、属性变化的要素，以及只通过 **8 px 缓冲**影响到的邻瓦片都在
   候选范围内；候选完全按几何计算，**无 ID 或重复 ID 的要素也不会漏报**。
2. **字节裁决（不多报）**：候选瓦片逐块经过投影 → 8 px 缓冲裁剪 → 0.5 px 简化 →
   4096 量化 → 稳定排序 → MVT 编码，**两端字节不同才上报**。源 GeoJSON 不同但编码
   结果相同（如亚网格级抖动、要素重排）不报。
3. 结果按 **x 升序、再 y 升序**排列；`from_etag` / `to_etag` 与
   `?revision=N` **钉版瓦片接口返回的 ETag 完全一致**（两端共用同一条
   LRU → SQLite `tile_cache` → 确定性编译的取片路径）。
4. **z=14 不枚举全球**：局部更新只覆盖几何附近的若干邻瓦片（一个点最多影响
   2×2 块），不会遍历该级别的 2³⁴ 个瓦片。

下面用北京附近的一个点（属性 `open` 由 `true` 改 `false`）、一条道路（中间节点
微调）和一个新增咖啡馆做示例（先按 [§4.1](#41-发布图层创建数据集revision-1)、
[§4.2](#42-发布新版本乐观并发控制) 发布 revision 1、2）。

**请求**（`z=14`，三个参数均必填）：

```bash
curl -s "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=2&z=14" \
  | python3 -m json.tool
```

**响应** `200 OK`（仅列出实际字节变化的 4 个相邻 z=14 瓦片，x、y 升序）：

```json
{
    "dataset_id": "f662e702b8eb456980d7452481e31469",
    "z": 14,
    "from_revision": 1,
    "to_revision": 2,
    "changed_tiles": [
        {
            "z": 14,
            "x": 13489,
            "y": 6207,
            "from_etag": "\"8ba8454b30a5f0e1fd98803b9d953bee2536637514bbcd583354a57ff5a68202\"",
            "to_etag": "\"08d0deddbec0bd7247e7760725a47e5b84a3b3f113937c5d7e9502e7b5ce009b\""
        },
        {
            "z": 14,
            "x": 13489,
            "y": 6208,
            "from_etag": "\"4ebd015258c6a944f17b85c605be554e1ed63bb109755c51f418c3bd48a36b69\"",
            "to_etag": "\"6ff02963e1b2a4e41598857a5a127683becf0985764aafc579a06981fbae9beb\""
        },
        {
            "z": 14,
            "x": 13490,
            "y": 6207,
            "from_etag": "\"fb23cb0773a32f5f6b542ff818b1c13509d0d87dec1d26838a8e06fec43295db\"",
            "to_etag": "\"bc9cb408bb2c82fae4e43a5163ba3f502f88ec3cde907203b06ae29ce6246cc7\""
        },
        {
            "z": 14,
            "x": 13490,
            "y": 6208,
            "from_etag": "\"cc0391a6e59e477238a5dcd8b16902411e5c28f2627b45f20a988a01e6959491\"",
            "to_etag": "\"243426eb6e8a578f480518e17c22af2b83169a6afeddaa0c66012b4b772e80d0\""
        }
    ]
}
```

**ETag 与钉版瓦片一致**——清单里的 ETag 可直接与钉版瓦片响应头核对：

```bash
curl -s -D - -o /dev/null \
  "http://localhost:8000/datasets/$DS/tiles/14/13489/6207.mvt?revision=1"
```

```http
HTTP/1.1 200 OK
etag: "8ba8454b30a5f0e1fd98803b9d953bee2536637514bbcd583354a57ff5a68202"
x-revision: 1
cache-control: public, max-age=31536000, immutable
```

同一区域在 **z=12** 只落在 2 个瓦片中（缩放级别越低，瓦片越大、清单越短）：

```bash
curl -s "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=2&z=12"
```

```json
{
    "dataset_id": "f662e702b8eb456980d7452481e31469",
    "z": 12,
    "from_revision": 1,
    "to_revision": 2,
    "changed_tiles": [
        {
            "z": 12, "x": 3372, "y": 1551,
            "from_etag": "\"3ccc98772e5f0a8cfe6dae7c87a1409794cdf23f8323c37a203c890d7e9ec197\"",
            "to_etag": "\"5d171ff36343ad961d78929dc9cb78816d419c2143b7ff5860da7fa7a63520e0\""
        },
        {
            "z": 12, "x": 3372, "y": 1552,
            "from_etag": "\"3864b1fd9a3e19f6b764f4140d3ed3d379d33d22a13193b8e7b193173ebb9937\"",
            "to_etag": "\"e28236a78c24a371e0fb5c213a1e01454668a5cf55301ea80d1a00144db3d3d1\""
        }
    ]
}
```

**相同修订返回空清单**（不可变修订 + 确定性管线，两端字节必然相同，不枚举任何瓦片）：

```bash
curl -s -i "http://localhost:8000/datasets/$DS/tile-diff?from_revision=2&to_revision=2&z=12"
```

```http
HTTP/1.1 200 OK
content-type: application/json
```

```json
{"dataset_id":"f662e702b8eb456980d7452481e31469","z":12,"from_revision":2,"to_revision":2,"changed_tiles":[]}
```

**参数 / 存在性错误**：

```bash
# 修订不存在
curl -s -i "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=9&z=12"
# HTTP/1.1 404 Not Found
# {"error":"revision 9 does not exist","dataset_id":"...","latest_revision":2}

# z 超出 0-14（from_revision/to_revision 缺失、非整数或 < 1 同样返回 400）
curl -s -i "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=2&z=15"
# HTTP/1.1 400 Bad Request
# {"error":"zoom z must be between 0 and 14"}

# 数据集不存在
curl -s -i "http://localhost:8000/datasets/nope/tile-diff?from_revision=1&to_revision=2&z=0"
# HTTP/1.1 404 Not Found
# {"error":"dataset not found","dataset_id":"nope"}
```

增量清单只依赖既有的发布与瓦片链路，不改变任何历史数据：旧的
`GET /datasets/{id}/tiles/...` 接口、`If-None-Match` 与钉版（`?revision=`）结果均保持
可用，清单编译出的瓦片也会复用 / 写入同一张 `tile_cache`。

### 4.7 错误示例

| 场景 | 状态码 |
| --- | --- |
| GeoJSON 不是 FeatureCollection / 空集合 / JSON 解析失败 / 坐标越界 / 自交多边形 / null 几何 | `400` |
| 瓦片 `z` 超出 0–14、`x/y` 为负或超出该级别范围、非整数 | `400` |
| tile-diff 缺少 `from_revision` / `to_revision` / `z`、修订号非整数或 < 1、`z` 超出 0–14 | `400` |
| identify 缺少 `px` / `py` / `radius_px`、非数字或非有限数、`px/py` 不满足 `0≤值<256`、`radius_px` 不满足 `0≤值≤8`、`revision` 非正整数、瓦片号非法 | `400` |
| 数据集或 `revision` 不存在（含 tile-diff 的任一修订、identify 的钉版） | `404` |
| 发布版本时 `expected_revision` 与 head 不一致 | `409` |
| 上传体积超过限制（默认 50 MB） | `413` |

```bash
# 非法缩放级别
curl -i "http://localhost:8000/datasets/$DS/tiles/15/0/0.mvt"
# HTTP/1.1 400 Bad Request
# {"error":"zoom z must be between 0 and 14"}

# x 超出范围（z=2 时合法范围为 0..3）
curl -i "http://localhost:8000/datasets/$DS/tiles/2/9/0.mvt"
# {"error":"tile coordinates out of range for z=2: 0 <= x,y < 4"}

# 非法 GeoJSON
curl -i -X POST http://localhost:8000/datasets \
  -H "Content-Type: application/json" \
  -d '{"type":"FeatureCollection","features":[]}'
# HTTP/1.1 400 Bad Request
# {"error":"FeatureCollection contains no features"}
```

### 4.8 退化小多边形：坍缩环裁决复核

源 GeoJSON 中**完全合法**的小多边形（例如边长约 0.1 mm 的三角形）在 4096 网格
量化时，环的全部顶点可能四舍五入到**同一个网格点**：环坍缩成两点，无法构成 GEOS
LinearRing。管线在**重建面之前逐环清理**，并按“能否围成有效面积”裁决（见
`app/tiler.py` 的 `_clean_quantized_polygon`）：

| 坍缩对象 | 裁决 |
| --- | --- |
| 外环 | 仅丢弃**该面**（其内环随之丢弃），同瓦片其他要素不受影响 |
| 内环（孔） | 仅去除**该孔**，外环与其余孔保留 |
| MultiPolygon 的某个子面 | 仅丢弃**该子面**，其余有效子面保留 |

因此瓦片请求始终返回 **200** 与可解码的 MVT，而不是 500；未坍缩的几何编译结果、
既有瓦片字节与钉版 ETag 完全不变。下面用 `data/tiny-polygon.geojson` 复核——它包含
一个 ~0.1 mm 小三角（id=1）、一个普通点（id=2）、一个带微孔的矩形（id=3）和一个
含微小部分的 MultiPolygon（id=4）：

**第 1 步：发布（revision 1）**

```bash
curl -s -X POST http://localhost:8000/datasets \
  -F "file=@data/tiny-polygon.geojson;type=application/geo+json" \
  -F "layer=demo"
```

```json
{
    "id": "9aa5d65f03624f5e9c8bd50889ee1932",
    "layer": "demo",
    "revision": 1,
    "content_hash": "e571e857bf09853247b5ad70ec77da50d44d933350414bd57b3bf2e8a49d0220",
    "feature_count": 4,
    "published_at": "2026-10-04T07:42:18Z"
}
```

```bash
DS=9aa5d65f03624f5e9c8bd50889ee1932   # 换成实际上一步返回的 id
```

**第 2 步：请求 z=0 世界瓦片——200，其余要素全部保留**

```bash
curl -s -D - -o /tmp/tiny-z0.mvt \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
```

```http
HTTP/1.1 200 OK
etag: "78531fb9e3334d4e091edb5d7f6e4b1c1c69b818448a8ec6dcef2744e4c50493"
x-revision: 1
content-length: 181
cache-control: public, no-cache
content-type: application/vnd.mapbox-vector-tile
```

解码可见：小三角（id=1）被丢弃，矩形的微孔与 MultiPolygon 的微小部分被去除，
但三个要素本身与点要素都保留在瓦片中（在容器内解码，无需本地安装依赖）：

```bash
docker compose exec -T vector-tiles python - <<EOF
import json, urllib.request, mapbox_vector_tile
url = "http://127.0.0.1:8000/datasets/$DS/tiles/0/0/0.mvt"
body = urllib.request.urlopen(url).read()
dec = mapbox_vector_tile.decode(body, default_options={"y_coord_down": True})
for name, layer in dec.items():
    for f in layer["features"]:
        print(name, "id:", f["id"], f["geometry"]["type"],
              json.dumps(f["properties"], ensure_ascii=False))
EOF
```

```text
demo id: 2 Point {"kind": "capital", "name": "Beijing"}
demo id: 3 Polygon {"name": "Rectangle with a tiny hole"}
demo id: 4 Polygon {"name": "MultiPolygon with a tiny part"}
```

即使在 z=14（1 网格单位 ≈ 0.6 m），0.1 mm 的三角形仍然坍缩，瓦片同样正常返回
200（该瓦片只剩点要素，66 字节）：

```bash
curl -s -D - -o /dev/null \
  "http://localhost:8000/datasets/$DS/tiles/14/13489/6208.mvt?revision=1"
```

```http
HTTP/1.1 200 OK
etag: "202dfb9ef3bd1a8f6bdc01122dff1d96874afb9e1395072f9849b1a14ac525ee"
x-revision: 1
content-length: 66
cache-control: public, max-age=31536000, immutable
```

**第 3 步：发布 revision 2（删除小三角、点属性改名）**

```bash
curl -s -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/tiny-polygon-v2.geojson;type=application/geo+json" \
  -F "expected_revision=1"
```

```json
{
    "id": "9aa5d65f03624f5e9c8bd50889ee1932",
    "layer": "demo",
    "revision": 2,
    "content_hash": "f20f39e4ecf704de4b6163fb36a1f855c6419eaf730ea573c0b3e75ed72e293e",
    "feature_count": 3,
    "published_at": "2026-10-04T07:42:36Z"
}
```

**第 4 步：增量清单——复用同一编译路径，按最终字节裁决**

```bash
curl -s "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=2&z=0" \
  | python3 -m json.tool
```

```json
{
    "dataset_id": "9aa5d65f03624f5e9c8bd50889ee1932",
    "z": 0,
    "from_revision": 1,
    "to_revision": 2,
    "changed_tiles": [
        {
            "z": 0,
            "x": 0,
            "y": 0,
            "from_etag": "\"78531fb9e3334d4e091edb5d7f6e4b1c1c69b818448a8ec6dcef2744e4c50493\"",
            "to_etag": "\"e100dd6348db1b4e814c65753f85c317ef5c220fd8cebfc165f626640e78dc85\""
        }
    ]
}
```

瓦片 (0,0,0) 的字节变化来自点要素属性改名（`Beijing` → `Beijing HQ`）；小三角的
删除不贡献任何字节变化——它从未进入瓦片。`from_etag` 与第 2 步钉版瓦片的 ETag
一致；`to_etag` 可用钉版接口核对：

```bash
curl -s -D - -o /dev/null "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt?revision=2" \
  | grep -i etag
# etag: "e100dd6348db1b4e814c65753f85c317ef5c220fd8cebfc165f626640e78dc85"
```

**第 5 步：只有小三角变化的修订——清单为空（不多报）**

把小三角加回去发布 revision 3（`data/tiny-polygon-v3.geojson`，其余内容与
revision 2 完全相同）：

```bash
curl -s -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/tiny-polygon-v3.geojson;type=application/geo+json" \
  -F "expected_revision=2"

curl -s "http://localhost:8000/datasets/$DS/tile-diff?from_revision=2&to_revision=3&z=0" \
  | python3 -m json.tool
```

```json
{
    "dataset_id": "9aa5d65f03624f5e9c8bd50889ee1932",
    "z": 0,
    "from_revision": 2,
    "to_revision": 3,
    "changed_tiles": []
}
```

源文档确实不同（`content_hash` 分别为 `f20f39e4…` 与 `45fc2d3e…`），但两个修订的
瓦片**逐字节相同**——钉版 ETag 都是 `e100dd63…`，因此清单为空：

```bash
curl -s -D - -o /dev/null "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt?revision=3" \
  | grep -i etag
# etag: "e100dd6348db1b4e814c65753f85c317ef5c220fd8cebfc165f626640e78dc85"
```

**非法源几何仍在发布期拒绝（400）**，不会进入瓦片管线（自相交 bowtie 多边形）：

```bash
curl -s -w "\n%{http_code}\n" -X POST http://localhost:8000/datasets \
  -H "Content-Type: application/json" \
  -d '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},"geometry":{"type":"Polygon","coordinates":[[[0,0],[10,10],[10,0],[0,10],[0,0]]]}}]}'
```

```json
{"error":"feature[0]: geometry is topologically invalid (Self-intersection[5 5])"}
```

```text
400
```

### 4.9 点选（identify）

在已发布的 revision 上对某张瓦片内的一次点击做点选查询，返回点击命中的要素列表。
端点复用瓦片的**同一条编译链路**（投影 → 8 px 缓冲裁剪 → 0.5 px 简化 → 4096
量化 → 清洗，见 [§5](#5-瓦片规格编译管线)），但**逐源要素编译**而不是解码合并后的
MVT，因此天然保留：

- **0 基源序号 `source_index`**：命中要素在发布的 GeoJSON `features` 数组中的下标
  （从 `0` 开始）；
- **亚像素点击位置**：`px/py`（源像素，`0 ≤ px,py < 256`）映射到 4096 网格
  （`× 16`）后**保持浮点、不做量化**，所以点到量化点要素 0.26 px 时会如实报
  0.26 px，而不是吸附为 0；
- **要素身份**：无 ID、字符串 ID、重复 ID 的要素各自独立裁决，**绝不合并**；
- **可见几何**：裁剪掉、维度降级或量化后坍缩消失（如 §4.8 的 0.1 mm 小三角）的
  几何在该瓦片中不可见，**不可能被点中**。

**裁决规则**（距离均在 4096 网格上量算、再换算回源像素，1 px = 16 单位）：

| 几何 | 命中条件 |
| --- | --- |
| 面（Polygon/MultiPolygon） | 点击落在**编译后面内部**（含边界面）→ `distance_px = 0`；点在面外时按到面边界的距离与半径比较 |
| 线（LineString/MultiLineString） | 点击到线的距离 **≤ `radius_px`** |
| 点（Point/MultiPoint） | 点击到点的距离 **≤ `radius_px`** |

跨瓦片的几何只在 **8 px 缓冲**内可见，因此**瓦片缓冲边缘处的点击同样按
`radius_px` 圆形半径裁决**（缓冲外没有编译几何，自然不命中）。命中列表按
`distance_px` 升序、距离相同再按 `source_index` 升序排列。

**查询参数**：

| 参数 | 必填 | 取值 | 说明 |
| --- | --- | --- | --- |
| `px`, `py` | 是 | `0 ≤ 值 < 256`（支持小数） | 点击点在瓦片内的源像素坐标，原点为瓦片左上角、X 向右、Y 向下 |
| `radius_px` | 是 | `0 ≤ radius_px ≤ 8` | 圆形命中半径（像素）；`0` 表示只接受零距离（面内部、正好压在线 / 点量化位置） |
| `revision` | 否 | 正整数 | 钉到指定历史 revision；**省略时查当前 head** |

#### 请求

先用 [§4.1](#41-发布图层创建数据集revision-1) 发布 `data/sample.geojson`
（含北京点、线、矩形），然后点选北京点要素所在的 **z=14** 瓦片。瓦片号
`13489/6208` 是北京（116.4074, 39.9042）所在瓦片；`px=212.51、py=84.087` 是该
经纬度在瓦片内的亚像素位置，半径给 4 px：

```bash
curl -s "http://localhost:8000/datasets/$DS/tiles/14/13489/6208/identify?px=212.51&py=84.087&radius_px=4" \
  | python3 -m json.tool
```

#### 响应（200 OK）

```json
{
    "dataset_id": "4c35e9016c2944168107b48155e8949c",
    "revision": 1,
    "z": 14,
    "x": 13489,
    "y": 6208,
    "px": 212.51,
    "py": 84.087,
    "radius_px": 4.0,
    "hits": [
        {
            "source_index": 0,
            "source_id": 1001,
            "properties": {
                "kind": "capital",
                "name": "Beijing"
            },
            "distance_px": 0.0265
        }
    ]
}
```

- 北京点在源数组下标 `0`（0 基），`source_id` 即 GeoJSON 的 `id=1001`；
- `distance_px` 为 **0.0265**：点击点（亚像素浮点）与量化后的点格点相差约
  0.03 px，小于 4 px 半径而命中；
- `revision` 是实际查询的版本（省略 `revision` 时即当前 head）。

**面内点击 → 距离 0**。对示例矩形（`id=3001`，源下标 `3`）所在的 z=3 瓦片
`6/3`，用 `radius_px=0` 点在矩形内部：

```bash
curl -s "http://localhost:8000/datasets/$DS/tiles/3/6/3/identify?px=85.312&py=169.312&radius_px=0"
```

```json
{
    "dataset_id": "4c35e9016c2944168107b48155e8949c",
    "revision": 1,
    "z": 3,
    "x": 6,
    "y": 3,
    "px": 85.312,
    "py": 169.312,
    "radius_px": 0.0,
    "hits": [
        {
            "source_index": 3,
            "source_id": 3001,
            "properties": {"name": "Demo rectangle"},
            "distance_px": 0.0
        }
    ]
}
```

点在无几何区域 / 半径不足时返回空 `hits`（仍是 200）；钉历史版本加
`&revision=N`，省略即 head：

```bash
curl -s "http://localhost:8000/datasets/$DS/tiles/0/0/0/identify?px=10&py=10&radius_px=0"
# {"dataset_id":"...","revision":1,"z":0,"x":0,"y":0,"px":10.0,"py":10.0,"radius_px":0.0,"hits":[]}
```

#### 错误（400 / 404）

非法瓦片号、坐标或半径返回 **400**，数据集或版本不存在返回 **404**：

```bash
# px 越界（必须 0 <= px < 256）
curl -s -i "http://localhost:8000/datasets/$DS/tiles/0/0/0/identify?px=256&py=10&radius_px=2"
# HTTP/1.1 400 Bad Request
# {"error":"'px' must satisfy 0 <= px < 256"}

# 半径越界（必须 0 <= radius_px <= 8）
curl -s -i "http://localhost:8000/datasets/$DS/tiles/0/0/0/identify?px=10&py=10&radius_px=9"
# HTTP/1.1 400 Bad Request
# {"error":"'radius_px' must satisfy 0 <= radius_px <= 8"}

# 缺少必填参数 / 非数字 / revision 非法
curl -s -i "http://localhost:8000/datasets/$DS/tiles/0/0/0/identify?px=10&py=10"
# HTTP/1.1 400 Bad Request
# {"error":"missing required query parameter 'radius_px'"}

curl -s -i "http://localhost:8000/datasets/$DS/tiles/15/0/0/identify?px=1&py=1&radius_px=1"
# HTTP/1.1 400 Bad Request
# {"error":"zoom z must be between 0 and 14"}

# 数据集 / 版本不存在
curl -s -i "http://localhost:8000/datasets/nope/tiles/0/0/0/identify?px=1&py=1&radius_px=1"
# HTTP/1.1 404 Not Found
# {"error":"dataset not found","dataset_id":"nope"}

curl -s -i "http://localhost:8000/datasets/$DS/tiles/0/0/0/identify?px=1&py=1&radius_px=1&revision=99"
# HTTP/1.1 404 Not Found
# {"error":"revision 99 does not exist","dataset_id":"...","latest_revision":1}
```

> **不影响瓦片**：点选只读取 revision 的源要素并在请求内重编译几何做距离裁决，
> **不读取也不写入** SQLite `tile_cache`，因此既有的
> `GET …/{z}/{x}/{y}.mvt` 瓦片字节与 ETag 完全不变。

---

## 5. 瓦片规格（编译管线）
对每个瓦片坐标与每个源要素依次执行：

1. **投影**：经度/纬度（EPSG:4326）→ 球面 Web Mercator 米（EPSG:3857，
   纬度在 ±85.05112878° 处截断），再线性映射到当前瓦片的 **4096 × 4096**
   局部整数网格（原点左上、X 向右、Y 向下）。
2. **裁剪**：与外扩 **8 像素**（8/256 × 4096 = **128 单位**）的矩形求交；
   裁剪后维度降级的部分被丢弃（线不允许退化成点，面不允许退化成线）。
3. **简化**：`preserve_topology=True` 的 Douglas–Peucker 简化，容差
   **0.5 像素 = 8 单位**。
4. **量化与清洗**：四舍五入到整数网格并夹在缓冲窗口内；删除重合点、
   零长度线段与零面积环，保证几何有效。面在**重建之前逐环清理**并按能否
   围成有效面积裁决：外环坍缩（合法小多边形的顶点全部量化到同一网格点）
   仅丢弃该面，内环坍缩仅去除该孔，MultiPolygon 的坍缩子面不影响其余有效
   子面——坍缩环因此永远不会以两点 LinearRing 的形式进入 GEOS 重建而抛出
   `GEOSException`，同瓦片其他要素保持不变（复核见
   [§4.8](#48-退化小多边形坍缩环裁决复核)）。
5. **稳定排序编码**：图层名升序，要素按 ID 分类确定性排序
   （非负整数 ID → 字符串 ID → 无 ID 者按内容 SHA-256），属性键排序，
   再交给 `mapbox-vector-tile` 编码为 MVT v2（`extent=4096`）。

**确定性**：管线中不含任何随机量或无序字典遍历；瓦片首次编译后，字节体与
ETag 会持久化在 SQLite `tile_cache` 表中，因此重复请求乃至服务进程重启后，
同一 `(dataset, revision, z, x, y)` 的字节和 ETag 始终一致。

> **增量清单如何选瓦片**：要素只会被编译进“瓦片外扩 8 px 缓冲盒”与其几何相交的
> 瓦片，因此 tile-diff 把两个修订中每个要素的 Web Mercator 包围盒同样外扩
> 128/4096 个瓦片跨度（8 px）并映射为瓦片索引、取并集，得到只增不漏的候选集；
> 随后每个候选仍走上面的完整管线并按编码字节裁决（见
> [§4.6](#46-离线瓦片增量清单tile-diff)）。

> 说明：MVT 要素 ID 按协议仅支持非负 uint64 整数；字符串 ID 的要素仍会被
> 确定性排序，但其 ID 不写入 protobuf（属性中若需要可自行保留）。

---

## 6. API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/datasets` | 上传 GeoJSON，创建数据集并发布 revision 1 |
| `POST` | `/datasets/{id}/revisions` | 追加不可变 revision（需 `expected_revision`，冲突返回 409） |
| `GET` | `/datasets` | 列出全部数据集 |
| `GET` | `/datasets/{id}` | 数据集元数据与全部 revision |
| `GET/HEAD` | `/datasets/{id}/tiles/{z}/{x}/{y}.mvt` | 获取瓦片（支持 `?revision=N` 与 `If-None-Match`） |
| `GET` | `/datasets/{id}/tiles/{z}/{x}/{y}/identify` | 瓦片内点选（必填 `px`、`py`（0–<256）、`radius_px`（0–8），可选 `revision`；省略查 head） |
| `GET` | `/datasets/{id}/tile-diff` | 两修订间实际字节变化的瓦片增量清单（`from_revision`、`to_revision`、`z=0–14`） |
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/docs` | Swagger UI 交互文档 |

---

## 7. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VT_DB_PATH` | `/data/tiles.db` | SQLite 数据库路径 |
| `VT_HOST` / `VT_PORT` | `0.0.0.0` / `8000` | 监听地址与端口 |
| `VT_MIN_ZOOM` / `VT_MAX_ZOOM` | `0` / `14` | 允许编译的缩放级别范围 |
| `VT_MAX_UPLOAD_BYTES` | `52428800` | 单次上传大小上限（50 MB） |
| `VT_WORKERS` | `2`（Docker） | uvicorn 工作进程数；多进程共享同一 SQLite 文件，首次建库与追加发布分别由文件锁和跨进程事务串行化 |
| `VT_TILE_CACHE_SIZE` | `4096` | 进程内瓦片 LRU 缓存条目数 |
| `VT_SQLITE_BUSY_TIMEOUT_MS` | `30000` | SQLite 忙等待（busy handler）毫秒数；连接建立后、切换 WAL 之前生效 |
| `VT_INIT_MAX_ATTEMPTS` | `8` | lifespan 建库有界重试次数；SQLite 忙锁与**非阻塞 init 文件锁抢锁失败**共用此预算，耗尽即抛 `SchemaUnavailable` 结束启动 |
| `VT_INIT_RETRY_DELAY_S` | `0.1` | 建库重试的初始退避秒数（指数翻倍，上限 2 秒），同样作用于文件锁抢锁重试 |
