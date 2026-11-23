# 真机验证报告

验证对象：`remote_push` 反向同步集成（B 无公网、主动推送到 A）。

## 环境

- 平台：InstaBox 云机（4 vCPU / 8 GiB / 20 GiB 持久盘），Docker 29.8.2
- 镜像：`ghcr.io/home-assistant/home-assistant:stable`，实际版本 **2026.11.2**
- 拓扑：两个真实 HA 容器
  - `ha-a`（hub）：映射 `18123:8123`，配置 `role: hub`，agent 前缀 `b_`
  - `ha-b`（agent）：**不映射任何端口**，仅加入 Docker 网络 `ha-test-net`，通过 `ws://ha-a:8123/api/websocket` 主动外连
  - 共享 secret：`test-shared-secret-12345`
- 真实集成：`uptime-kuma`（HACS 之外的核心集成）指向 `louislam/uptime-kuma:2`，含 1 个 HTTP 监控项 `HA A`（`http://ha-a:8123`）

## 验证步骤与结果

### 1. 加载与握手

- 两个实例均正常加载 `remote_push`（HA 仅打印 "custom integration not tested" 警告）。
- B 日志：`remote_push agent started` → `connecting to hub ws://ha-a:8123/api/websocket` → `pushed 18 states to hub`。
- A 侧自定义 WebSocket 命令全部注册成功（用独立客户端逐条探测 5 个命令，均返回 `unauthorized` 而非 `unknown_command`）。

### 2. 全量状态镜像

B 推送 18 个状态，A 上生成带前缀的镜像实体，例如：

```
input_number.b_test_sensor = 42.0
input_boolean.b_test_switch = off
sensor.b_backup_backup_manager_state = blocked
zone.b_home = 0
```

属性中带 `remote_push_agent: b`。

### 3. 增量状态同步

在 B 上调用 `input_number.set_value`：42 → 88 → 99，A 镜像每次都同步更新，`last_updated` 随之变化。

### 4. A → B 服务转发

两条路径都验证：

- 显式服务：A 调 `remote_push.call_service`（`agent_id: b`、`input_number.set_value`、`value: 66`），B 本地实体从 42 变为 66，A 镜像同步 66。
- 实体级拦截：A 直接对 `input_number.b_test_sensor` 调 `input_number.set_value`（`value: 33`），hub 拦截后剥离前缀转发，B 变为 33，A 镜像 33。

### 5. 断线清理与重连

- `docker stop ha-b` 后，A 上 20 个 `b_` 镜像实体全部移除。
- `docker start ha-b` 后，B 重新注册并全量推送，镜像实体恢复。

### 6. 真实 Uptime Kuma 集成

B 上通过配置流程接入 `uptime-kuma`（URL `http://uptime-kuma:3001` + API Key），产生真实传感器：

```
sensor.ha_a_status = up
sensor.ha_a_response_time = 3
sensor.ha_a_monitor_type = http
sensor.ha_a_monitored_url = http://ha-a:8123
sensor.ha_a_certificate_expiry = unknown
update.uptime_kuma_uptime_kuma_version = off
```

A 侧全部镜像成功：

```
sensor.b_ha_a_status = up
sensor.b_ha_a_response_time = 3
sensor.b_ha_a_monitor_type = http
sensor.b_ha_a_monitored_url = http://ha-a:8123
sensor.b_ha_a_certificate_expiry = unknown
update.b_uptime_kuma_uptime_kuma_version = off
```

## 真机联调发现并修复的问题

| # | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | A 日志 `coroutine 'ws_register' was never awaited`，注册流程卡住 | HA 2026 要求 WebSocket 处理函数加 `@websocket_api.async_response` | 5 个命令全部补装饰器 |
| 2 | 全量推送成功，但增量状态在 A 报 `Received invalid command` | agent 的 `state_changed` / `remove` 消息没有 `id`，HA 判为格式非法（id 必填且须递增） | 两条消息补 `id`，并加发送队列保证顺序 |
| 3 | A 收不到服务执行结果 | agent 把 hub 的请求 id 原样回传，被 hub 的 id 递增校验丢弃 | 结果改用新 id + `request_id` 关联，hub 读取 `request_id` |
| 4 | A→B 服务转发失败：`'ActiveConnection' object has no attribute 'send_json'` | HA 2026 的 `ActiveConnection` 只有 `send_message` | hub 优先 `send_message`，兼容旧接口 |
| 5 | 停掉 B 后 A 的镜像实体不清理 | HA 2026 移除了 `async_register_disconnect_callback` | 用 `connection.subscriptions` 的 unsubscribe 回调触发清理 |

## 备注

- 测试中 A 的登录 token 用的是 `auth/long_lived_access_token`（`lifespan: 30`、`client_name`）生成的 30 天令牌；HA 2026 不再接受旧的 `config/auth/long_lived_token/create`。
- 云机上的验证环境仍在：容器 `ha-a`（宿主 18123）、`ha-b`（无端口）、`uptime-kuma`（宿主 3001），配置目录 `/data/ha-real/{a,b}/config`。仅用于测试，含测试口令，勿直接用于生产。
- 本地 `tests/test_remote_push.py` 的 stub 测试与真机测试均通过；stub 不模拟 HA 的 id 校验与连接清理，因此上述问题只能由真机暴露。
