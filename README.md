# remote_push

让**无公网 IP 的 B** 主动外连到**有公网的 A**，把 B 的实体状态推到 A，并允许 A 把服务调用转发回 B。

这是针对上游 `custom-components/remote_homeassistant` 连接方向写死（主实例必须能连到远端）的适配实现：上游是 A 主动连 B，这里改成 **B 主动连 A**，因此 B 不需要公网 IP、不需要端口映射，只需要能出网访问 A。

## 工作方式

```
B(agent) --ws/wss 主动连接--> A(hub) /api/websocket
B 全量状态 + state_changed 事件  ==> A 镜像实体
A 服务调用(实体拦截或显式服务)  ==> B 本地执行 ==> 结果事件回传
```

- A 注册自定义 WebSocket 命令，接收 B 的注册、状态推送、状态删除、服务执行结果。
- B 用 A 的长期访问令牌登录 A 的 WebSocket API，再用 `agent_id + secret` 注册。
- 连接断开后 A 会自动移除该 agent 的镜像状态；B 每 10 秒重连。
- 镜像实体 ID = `域.前缀对象ID`，默认前缀为 `<agent_id>_`，例如 `sensor.b_温度`。

## 安装

把 `custom_components/remote_push` 目录分别复制到 **A 和 B** 的 HA 配置目录下，然后重启两个实例。目录结构应为：

```
<HA 配置目录>/custom_components/remote_push/
    __init__.py
    manifest.json
    const.py
    hub.py
    agent.py
```

## A（有公网，hub）配置

先在 A 上生成长期访问令牌：个人资料 → 长期访问令牌 → 创建令牌。建议为 B 单独建一个受限用户，用它生成令牌，不要用管理员主账号令牌。

```yaml
remote_push:
  role: hub
  agents:
    - agent_id: b
      secret: !secret remote_push_secret
      prefix: b_
```

`secret` 是 A、B 两边约定的共享密钥，与 HA 令牌无关，用于防止他人冒充 agent 注册。

## B（无公网，agent）配置

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
  prefix: b_                # 可选，实际由 hub 侧前缀决定，此处仅作记录

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

## 在 A 上调用 B 的设备

两种做法：

1. **显式服务（推荐，最稳）**：

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

2. **直接调用镜像实体**：在 A 的自动化里对 `light.b_客厅灯` 调 `light.turn_on`，hub 会拦截 `call_service` 事件并转发给 B。注意 A 本地没有对应实体对象时，HA 自身可能打印一条 "Unable to find entity" 日志，不影响转发；若某个集成不接受这种方式，请改用第 1 种。

服务执行结果会以事件 `remote_push_service_result` 发到 A 的事件总线，字段为 `agent_id`、`request_id`、`ok`、`error`。

## 安全说明

- B 持有的长期令牌等于 A 的完整 API 权限。请为 B 单独建用户，只开放必要实体权限，并把 secret 放进 `secrets.yaml`。
- `secure: true` 时走 wss，公网环境强烈建议开启；`verify_ssl: false` 仅用于自签证书调试。
- 没有任何端口需要从公网访问 B。

## 限制

- 同步的是**状态机中的实体状态**，不是 B 的自动化、设备注册表或配置；实体属性会带 `remote_push_agent` 标记。
- 方向是单向的：B → A。若还要 A → B 的实体同步，在 B 上再装上游的 `remote_homeassistant` 连 A 即可（A 有公网，那个方向天然可行）。
- 依赖 HA 的 `websocket_api`，适用于近几个大版本的 HA（2024.x 及以后）。此实现已做语法检查，但未在真实 HA 实例上跑过端到端联调。
