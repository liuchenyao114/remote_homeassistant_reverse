# Remote Push

[![HACS Custom][hacs-badge]][hacs-url]
[![Release][release-badge]][release-url]
[![Validate][validate-badge]][validate-url]
[![License][license-badge]](./LICENSE)

**让没有公网 IP 的 B 主动外连有公网的 A**，把 B 的实体状态推到 A，并允许 A 把服务调用转发回 B。

上游 [`custom-components/remote_homeassistant`](https://github.com/custom-components/remote_homeassistant) 的连接方向是写死的：主实例必须能访问远端。本集成把方向反过来——**B 主动连 A**，所以 B 不需要公网 IP、不需要端口映射、不需要 DDNS，只要能出网访问 A 即可。

```
B(agent, 无公网) ──ws/wss 主动外连──> A(hub, 有公网) /api/websocket
        B 全量状态 + state_changed 事件  ==>  A 上的镜像实体
        A 服务调用(显式服务或实体拦截)  ==>  B 本地执行，结果回传
```

## HACS 安装

[![在 Home Assistant 中打开仓库][myhass-badge]][myhass-url]

1. HACS → 右上角 ⋮ → **自定义存储库 / Custom repositories**
2. 仓库地址填 `https://github.com/liuchenyao114/remote_homeassistant_reverse`，类别选 **Integration**
3. 在 HACS 中搜索 **Remote Push** → 下载
4. 重启 Home Assistant
5. 按下面「配置」章节在 **A 和 B 两侧**的 `configuration.yaml` 写入 `remote_push:` 配置，再次重启

HACS 会在有新 Release 时提示升级，升级后重启生效。

### 手动安装（不使用 HACS）

把 `custom_components/remote_push` 整个目录复制到 **A 和 B** 的 HA 配置目录下，重启：

```
<HA 配置目录>/custom_components/remote_push/
    __init__.py
    manifest.json
    const.py
    hub.py
    agent.py
    brand/icon.png
```

## 配置

### A（有公网，hub）

先在 A 上生成长期访问令牌：个人资料 → 安全 → 长期访问令牌 → 创建令牌。建议为 B 单独建一个受限用户，用它生成令牌，**不要用管理员主账号令牌**。

```yaml
remote_push:
  role: hub
  agents:
    - agent_id: b
      secret: !secret remote_push_secret
      prefix: b_
```

`secret` 是 A、B 两边约定的共享密钥，与 HA 令牌无关，用于防止他人冒充 agent 注册。

### B（无公网，agent）

```yaml
remote_push:
  role: agent
  agent_id: b
  secret: !secret remote_push_secret
  host: ha.example.com      # A 的域名或公网 IP
  port: 443                 # A 的 HA 端口；走反代通常是 443
  secure: true              # true = wss，false = ws
  verify_ssl: true
  token: !secret a_long_lived_token

  # 可选：只同步指定域 / 实体
  include_domains:
    - sensor
    - binary_sensor
    - switch
    - light
  exclude_entities:
    - sensor.internal_debug
```

如果 A 是裸 8123 HTTP，用 `port: 8123`、`secure: false`；如果 A 走 HTTPS 反代，用域名 + 443 + `secure: true`。

A 侧 `prefix` 决定镜像实体 ID：`域.前缀对象ID`，默认前缀 `<agent_id>_`，例如 `sensor.b_温度`。

## 在 A 上调用 B 的设备

**方式一：显式服务（推荐，最稳）**

```yaml
action: remote_push.call_service
data:
  agent_id: b
  domain: light
  service: turn_on
  entity_id: light.b_客厅灯
  service_data:
    brightness: 200
```

**方式二：直接调用镜像实体**

在 A 的自动化里对 `light.b_客厅灯` 调 `light.turn_on`，hub 会拦截 `call_service` 事件并转发给 B。注意 A 本地没有对应实体对象时，HA 自身可能打印一条 "Unable to find entity" 日志，不影响转发；若某个集成不接受这种方式，请改用方式一。

服务执行结果以事件 `remote_push_service_result` 发到 A 的事件总线，字段为 `agent_id`、`request_id`、`ok`、`error`。

## 工作方式

- A 注册自定义 WebSocket 命令，接收 B 的注册、状态推送、状态删除、服务执行结果。
- B 用 A 的长期访问令牌登录 A 的 WebSocket API，再用 `agent_id + secret` 注册。
- 连接断开后 A 自动移除该 agent 的镜像状态；B 每 10 秒重连。
- 镜像实体的属性里带 `remote_push_agent` 标记，便于识别来源。

## 安全说明

- B 持有的长期令牌等于该用户在 A 上的完整 API 权限。请为 B 单独建用户，只开放必要实体权限，并把 secret 放进 `secrets.yaml`。
- `secure: true` 时走 wss，公网环境强烈建议开启；`verify_ssl: false` 仅用于自签证书调试。
- 没有任何端口需要从公网访问 B。

## 测试

仓库自带端到端测试（用 aiohttp 模拟 HA WebSocket 服务端，stub 掉 HA API，无需真实 HA）：

```bash
pip install aiohttp voluptuous
python tests/test_remote_push.py
```

覆盖：首次全量状态推送、前缀与过滤、增量 `state_changed`、显式服务转发、实体级服务拦截、服务结果事件、断线后镜像状态清理。CI 每次提交都会跑。

### 真实 HA 双实例联调（已通过）

在云机上用 Docker 起了**两个真实 Home Assistant 2026.11.2 实例**：A 映射端口作 hub，B 不映射任何端口、只在容器网络内主动外连 A，复现"B 无公网"的拓扑；并在 B 上安装真实的 **Uptime Kuma v2** 集成（含 HTTP 监控项）产生真实传感器实体。

| 用例 | 结果 |
| --- | --- |
| B 全量状态推送，A 生成 `b_` 前缀镜像 | 通过（18 个实体） |
| B 改 `input_number` 42→88→99，A 增量同步 | 通过 |
| A 调 `remote_push.call_service` 让 B 执行（42→66） | 通过 |
| A 直接对镜像实体调 `input_number.set_value`（→33） | 通过（hub 拦截转发） |
| 停掉 B，A 清理全部镜像（20→0）；B 重启后恢复推送 | 通过 |
| B 上 Uptime Kuma 真实传感器推到 A | 通过：`sensor.b_ha_a_status=up`、`sensor.b_ha_a_response_time=3` 等 6 个实体 |

真机联调额外发现并修复了 5 个只在真实 HA 上才暴露的问题，详见 [`VERIFICATION.md`](./VERIFICATION.md)。

## 限制

- 同步的是**状态机中的实体状态**，不是 B 的自动化、设备注册表或配置。
- 方向是单向的：B → A（状态），A → B（服务调用）。若还要 A → B 的实体同步，在 B 上再装上游的 `remote_homeassistant` 连 A 即可（A 有公网，那个方向天然可行）。
- 集成通过 YAML 配置（`config_flow: false`），不支持在「设置 → 设备与服务」里点按添加。
- 依赖 HA 的 `websocket_api`。**已在真实 HA 2026.11.2 完成双实例联调**，`hacs.json` 声明的最低版本为 2024.1.0；代码同时兼容旧版 HA（连接清理优先用 `subscriptions`，退回 `async_register_disconnect_callback`；发送优先用 `send_message`，退回 `send_json`）。

## HA 2026 兼容性说明

真实 HA 2026.11.2 相比旧版有几处 API / 协议变化，本实现已适配：

- WebSocket 命令处理函数必须加 `@websocket_api.async_response`，否则协程不会被 await。
- 每条发往 hub 的消息必须带**递增的 `id`**，缺失会被判为 "Received invalid command"；状态变更使用独立发送队列保证 id 顺序。
- 服务结果不能用 hub 的请求 id 回传（会被 id 递增校验丢弃），改为新 id + `request_id` 字段关联。
- `ActiveConnection` 不再有 `send_json`，hub→agent 改用 `send_message`。
- `ActiveConnection` 不再有 `async_register_disconnect_callback`，改用 `connection.subscriptions` 的 unsubscribe 回调做断线清理。
- 创建长期令牌的 WebSocket 命令是 `auth/long_lived_access_token`，参数为 `lifespan`（天）与 `client_name`。

## 许可

MIT，见 [LICENSE](./LICENSE)。灵感来自 `custom-components/remote_homeassistant`，但连接方向与协议实现均为独立编写。

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/liuchenyao114/remote_homeassistant_reverse?display_name=tag
[release-url]: https://github.com/liuchenyao114/remote_homeassistant_reverse/releases
[validate-badge]: https://github.com/liuchenyao114/remote_homeassistant_reverse/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/liuchenyao114/remote_homeassistant_reverse/actions/workflows/validate.yml
[license-badge]: https://img.shields.io/github/license/liuchenyao114/remote_homeassistant_reverse
[myhass-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[myhass-url]: https://my.home-assistant.io/redirect/hacs_repository/?owner=liuchenyao114&repository=remote_homeassistant_reverse&category=integration
