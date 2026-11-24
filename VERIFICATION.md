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

---

# 第二轮：HACS 打包与发行包真机安装验证

## 仓库改造内容

| 项目 | 说明 |
| --- | --- |
| `hacs.json` | `name: Remote Push`、`homeassistant: 2024.1.0`、`render_readme: true` |
| `custom_components/remote_push/manifest.json` | 补 `codeowners: ["@liuchenyao114"]`、`documentation`、`issue_tracker`，指向本仓库 |
| `custom_components/remote_push/brand/icon.png` | 256×256 图标，HACS 列表显示用 |
| `custom_components/remote_push/services.yaml` | 描述 `remote_push.call_service` 的 5 个字段（hassfest 强制要求） |
| `LICENSE` | MIT |
| `.github/workflows/validate.yml` | hassfest + HACS validation + 端到端测试，三个 job |
| 仓库元信息 | description、topics（`home-assistant`、`hacs`、`hacs-integration`、`integration` 等）、Issues 开启 |
| Release | `v1.0.0`，与 manifest `version` 一致 |

## CI 结果（commit eeac868）

```
hassfest            success
HACS                success
integration tests   success
```

期间修掉两个 hassfest 报错：注册了服务但没有 `services.yaml`；`services.yaml` 里 `{"brightness": 200}` 未加引号导致 YAML 解析失败。

## 发行包结构验证

按 HACS 的下载路径取 `archive/refs/tags/v1.0.0.zip`（35 KB，22 个条目），解包后：

```
remote_homeassistant_reverse-1.0.0/
  custom_components/remote_push/
    __init__.py  agent.py  const.py  hub.py
    manifest.json  services.yaml  brand/icon.png
  hacs.json  LICENSE  README.md  VERIFICATION.md
  tests/test_remote_push.py  example_configuration.yaml
```

`manifest.json` 的 `version` = `1.0.0`，与 tag 一致（HACS 以此判断升级）。发行包内测试套件重跑：

```
PASS  initial state push + prefix + filters
PASS  incremental state_changed push
PASS  service call forwarding + result event
PASS  entity-level service interception
PASS  disconnect cleanup
ALL TESTS PASSED
```

## 真机按 HACS 安装方式复测

把发行包里的 `custom_components/remote_push`（含 `brand/`、`services.yaml`）覆盖到两台真实 HA 2026.11.2 的配置目录，`docker restart` 后验证：

| 用例 | 结果 |
| --- | --- |
| B 启动后自动连 A 并全量推送 | 通过：`pushed 18 states to hub` |
| A 侧镜像实体 | 通过：26 个 `b_` 前缀实体，含 Uptime Kuma 的 `sensor.b_ha_a_status=up` |
| A 重启后 B 自动重连并恢复镜像 | 通过：重启 A 后镜像数恢复为 26 |
| `services.yaml` 被真实 HA 解析 | 通过：`/api/services` 返回 `remote_push.call_service`，`name=Call service on agent`，字段 `agent_id/domain/service/entity_id/service_data` |
| A 调 `remote_push.call_service` 改 B 的 `input_number` | 通过：77.0 → 55.0，A 侧镜像同步 |

注意：在补 `services.yaml` 之前，真实 HA 每次读取服务描述都会打印
`ERROR Failed to load services.yaml for integration: remote_push`；补上后该报错消失。

---

# 第三轮：v1.1.0 界面点按配置（config flow）

## 代码改动

| 文件 | 变化 |
| --- | --- |
| `config_flow.py`（新增） | 角色菜单（Hub / Agent）+ 两套表单；Agent 提交前用令牌访问 `http(s)://host:port/api/` 校验，401/403 → `invalid_auth`，连不上 → `cannot_connect`；同实例只允许一个 Agent 条目，Hub 侧同 agent_id 不重复登记；`OptionsFlow` 按角色给不同字段 |
| `__init__.py` | 新增 `async_setup_entry` / `async_unload_entry` / 更新监听；hub 按条目增删 agent（`PushHub.add_agent` / `async_remove_agent`），最后一条 agent 删掉后 hub 注销；YAML 路径保留 |
| `hub.py` | `PushHub(hass, agents_conf=None)`、`add_agent()`、`async_remove_agent()`、`_started` 幂等启动 |
| `agent.py` | 新增 `async_stop()`，并回收停止监听 |
| `strings.json` + `translations/en.json` + `translations/zh-Hans.json`（新增） | 中英文案，表单字段说明与错误提示 |
| `manifest.json` | `config_flow: true`，版本 1.1.0 |
| `hacs.json` | 最低 HA 版本提到 2024.11.0（选项流依赖该版本自动注入的 `OptionsFlow.config_entry`） |

## 真机验证（HA 2026.11.2，两台实例）

前置：把 A、B 的 `configuration.yaml` 里 `remote_push:` 整段删除，重启后集成处于未配置状态，完全靠界面流程建立。

| 用例 | 结果 |
| --- | --- |
| A：`POST /api/config/config_entries/flow` 打开流程 | 通过：`type=menu`，`menu_options=[hub, agent]` |
| A：选 Hub → 表单字段 | 通过：`agent_id`(text)、`secret`(text)、`prefix`(text)、`include/exclude_domains`(text)、`include/exclude_entities`(entity) |
| A：提交 Hub 表单 | 通过：`type=create_entry`，标题 `Hub · agent b`，条目状态 `loaded` |
| B：选 Agent → 表单字段 | 通过：在 Hub 字段基础上多 `host`(text)、`port`(number, 默认 8123)、`secure`(boolean)、`verify_ssl`(boolean)、`token`(text) |
| B：填错令牌提交 | 通过：表单原地返回 `errors={"base": "invalid_auth"}`，未创建条目 |
| B：填正确令牌提交 | 通过：`create_entry`，标题 `Agent b → ha-a` |
| 条目生效（无 YAML） | 通过：B 日志 `pushed 26 states to hub`，A 侧 26 个 `b_` 镜像实体，含 `sensor.b_ha_a_status=up` |
| 删除 B 的 Agent 条目 | 通过：`DELETE .../entry/{id}` → `{"require_restart": false}`，A 镜像 25 → 0 |
| 重新点按添加 Agent | 通过：镜像恢复到 26 |
| 选项流（表单与取值） | 通过：在 HA 运行环境中直接驱动 `RemotePushOptionsFlow.async_step_init`，Hub 给 6 个字段、Agent 给 10 个字段，取值与原条目数据合并且类型正确（`port` 转 int、域名列表按逗号拆分、`agent_id` 不被表单覆盖） |

未做端到端复测：选项流「保存 → 条目自动重载」这一段（`entry.add_update_listener` → `async_reload`）。代码路径简单，但没在实例上跑通保存动作。

## 过程中遇到的问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `POST /auth/login_flow` 返回 `expected a list at 'handler'` | HA 2026 把 `handler` 改成 `[类型, 唯一ID]` 二元组 | 传 `["homeassistant", null]` |
| 测试脚本导入 `remote_push` 时报 `No module named homeassistant.config_entries` | 本地 stub 没有该模块 | `__init__.py` 里把 `ConfigEntry` 挪到 `TYPE_CHECKING` 下，仅作类型标注 |
| 首次渲染 Hub 表单时 `prefix` 默认值显示 `_` | `f"{''}_"` | 只有已知 `agent_id` 时才生成 `<agent_id>_`，否则留空，提交时在 `normalize()` 里补 |

---

# 第四轮：v1.1.1 / v1.1.2 启动与停止修复

装到真实 HA 后暴露两个只在 1.1.0 出现的问题，1.1.2 已修复。

| 现象 | 原因 | 修复 |
| --- | --- | --- |
| 每次重启/关闭 HA，日志出现 `ValueError: list.remove(x): x not in list` | `EVENT_HOMEASSISTANT_STOP` 是用 `async_listen_once` 注册的；HA 自己触发后监听已被移除，代码随后又调 `unsub()` 反注册，于是 `list.remove` 抛错 | `_async_stop` 不再反注册一次性监听，只清引用 |
| 首次全量推送缺晚加载平台的实体（如 `uptime_kuma`），要等这些实体下次变化才出现在 A 上 | agent 在 `async_setup` 阶段就连接，此时 HA 还没启动完成，部分平台尚未 `async_setup_entry` | 改为监听 `homeassistant_started` 事件后再连接。用普通监听而不是 `async_listen_once`——一次性监听触发后同样无法取消，会重演上面的 `ValueError` |

版本一路 `1.1.0 → 1.1.1 → 1.1.2`，1.1.1 存活时间太短（只改了 stop 监听，启动时序问题紧接着暴露），已删除该 tag 与 Release，只保留 1.1.2。

## 复测（两台真实 HA 2026.11.2，安装 1.1.2 发行包）

| 用例 | 结果 |
| --- | --- |
| 两侧 `manifest.json` 版本 | 1.1.2 |
| B 启动后首次推送 | 通过：`pushed 26 states to hub`（含 Kuma 的 6 个实体，说明首发即完整） |
| A 侧镜像实体 | 通过：26 个 `b_` 实体，`sensor.b_ha_a_status=up` |
| 连续重启期间的日志 | 通过：无 `ValueError`、无 `Traceback`；只有正常的 `agent b disconnected, mirrored states removed` |
| 界面条目状态 | 通过：`Hub · agent b` = `loaded` |
| CI（commit 76ad938） | hassfest ✅ / HACS ✅ / integration tests ✅ |

---

# 第五轮：v1.1.3 心跳断线竞态

真机日志出现一次：

```
File "/config/custom_components/remote_push/agent.py", line 175, in _session
    async for msg in ws:
...
File "/usr/local/lib/python3.14/site-packages/aiohttp/client_ws.py", line 471, in receive
    await self.pong(msg.data)
...
aiohttp.client_exceptions.ClientConnectionResetError: Cannot write to closing transport
```

| 现象 | 原因 | 修复 |
| --- | --- | --- |
| Agent 收到 Hub 的 PING 时连接已经在关闭，aiohttp 自动回 PONG 失败，异常冒泡到 `_run` 被 `_LOGGER.exception` 打成 ERROR traceback | aiohttp 3.13/3.14 的 `ClientWebSocketResponse.receive()` 处理 PING 时会同步调用 `pong()`；对端已发 CLOSE、传输层正在关闭时写帧抛 `ClientConnectionResetError`。这属于正常断线竞态，不是同步逻辑错误 | `_run` 对 `ClientConnectionResetError` / `ConnectionResetError` 走 debug 日志，对其他 `OSError`（例如连接被拒、DNS 失败）走 warning 日志，只有真正未知异常才保留完整 traceback；`_close_ws` 统一吞掉关闭竞态的 `OSError` |

行为不变：异常后仍按 `RECONNECT_DELAY` 重连，Hub 侧镜像状态照常清理并重建。

测试补充（`tests/test_remote_push.py`）：

- 用一个 `close()` 抛 `ClientConnectionResetError` 的假 WebSocket 验证 `_close_ws` 不抛错
- 用一次性 `_session()` 抛同类异常的探针验证 `_run()` 不抛错且会重试第二次

本地结果：

```
PASS  heartbeat close race handled without error
PASS  initial state push + prefix + filters
PASS  incremental state_changed push
PASS  service call forwarding + result event
PASS  entity-level service interception
PASS  disconnect cleanup
ALL TESTS PASSED
```

版本：`1.1.2 → 1.1.3`。
