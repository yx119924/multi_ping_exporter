# multi_ping_exporter

一个面向 Prometheus 的多目标 ICMP 探测 exporter，用于替代大量目标共用
`blackbox_exporter` 时的集中探测模式。

## 解决的问题

`blackbox_exporter` 通常在 Prometheus scrape 时即时执行探测。当目标较多、
探测包数较多或网络抖动时，容易在同一个 scrape 周期内产生并发峰值，导致：

- exporter 探测超时；
- Prometheus scrape timeout；
- 明明从其他机器 ping 通，但监控误报离线；
- 单次探测失败直接触发告警。

本项目由后台线程持续探测目标并缓存结果，Prometheus 只读取 `/metrics`，
因此可以把探测压力打散，并通过最近窗口统计降低单次失败造成的误告警。

## 工作方式

```text
multi_ping_exporter.json
          |
          v
后台调度器 -> 多个 worker -> 系统 ping 命令 -> 缓存最近结果
                                      |
                                      v
                           /metrics、/targets、/healthz
                                      |
                                      v
                         Prometheus -> Grafana / Alertmanager
```

exporter 使用部署机器上的系统 `ping` 命令，支持 Linux 和 Windows 的
常见输出格式。探测不是在 Prometheus 请求到来时临时执行的。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `multi_ping_exporter.py` | exporter 主程序 |
| `multi_ping_exporter.json` | 监听、探测参数和目标列表 |
| `multi_ping_exporter.service` | Linux systemd 服务文件 |
| `prometheus_scrape.yml` | Prometheus scrape 配置示例 |
| `prometheus_rules.yml` | 连通性、丢包、延迟和队列告警规则 |
| `grafana_ping_multi_exporter_dashboard.json` | Grafana 仪表盘导入文件 |

## 环境要求

- Linux 服务器推荐使用 Python 3.10 或更高版本；
- 服务器必须安装并允许执行 `ping`；
- exporter 运行用户必须有执行 `ping` 的权限；
- Prometheus 能访问 exporter 的监听端口，默认是 `9116`；
- Grafana 已配置一个 Prometheus 数据源。

## 快速部署

以下示例将 exporter 部署到 `/opt/multi-ping-exporter`，运行用户为
`prometheus`。

### 1. 准备目录和文件

```bash
sudo mkdir -p /opt/multi-ping-exporter
sudo cp multi_ping_exporter.py /opt/multi-ping-exporter/
sudo cp multi_ping_exporter.json /opt/multi-ping-exporter/
sudo cp multi_ping_exporter.service /etc/systemd/system/
sudo chown -R prometheus:prometheus /opt/multi-ping-exporter
```

如果服务器没有 `prometheus` 用户：

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin prometheus
sudo chown -R prometheus:prometheus /opt/multi-ping-exporter
```

### 2. 检查配置

```bash
python3 -m json.tool /opt/multi-ping-exporter/multi_ping_exporter.json >/dev/null
sudo -u prometheus python3 -m py_compile \
  /opt/multi-ping-exporter/multi_ping_exporter.py
```

### 3. 启动服务

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now multi_ping_exporter
sudo systemctl status multi_ping_exporter --no-pager
```

查看实时日志：

```bash
sudo journalctl -u multi_ping_exporter -f
```

### 4. 本机验证

```bash
curl -fsS http://127.0.0.1:9116/healthz
curl -fsS http://127.0.0.1:9116/targets
curl -fsS http://127.0.0.1:9116/metrics | grep -E \
  'ping_target_enabled|ping_probe_reason'
```

预期结果：

- `/healthz` 返回 `ok`；
- `/targets` 返回每个目标的探测状态；
- `/metrics` 中出现 `ping_probe_success` 和 `ping_probe_reason`；
- 首轮探测完成后，目标的 `reason` 不再是 `not_probed`。

## 配置说明

配置文件为 `multi_ping_exporter.json`：

```json
{
  "listen": "0.0.0.0:9116",
  "probe_interval_seconds": 15,
  "probe_timeout_seconds": 3,
  "ping_count": 5,
  "success_required": 1,
  "workers": 20,
  "jitter_seconds": 0.05,
  "window_size": 20,
  "targets": [
    {
      "target": "192.0.2.2",
      "env": "example"
    },
    {
      "target": "192.0.2.1",
      "env": "example",
      "alert_schedule": "business_hours",
      "enabled": false,
      "comment": "维护中"
    }
  ]
}
```

### 全局参数

| 参数 | 说明 | 建议 |
| --- | --- | --- |
| `listen` | HTTP 监听地址和端口 | 内网部署使用 `0.0.0.0:9116` |
| `probe_interval_seconds` | 每轮探测间隔 | 目标较多时使用 15 到 30 秒 |
| `probe_timeout_seconds` | 每个 ping 包的等待时间 | 内网 2 到 3 秒，跨网络可用 5 秒 |
| `ping_count` | 每个目标每轮发送的包数 | 推荐 5，便于观察延迟和抖动 |
| `success_required` | 收到多少个包算本轮成功 | 推荐 1，降低偶发丢包误报 |
| `workers` | 并发探测 worker 数量 | 几十个目标可用 20 |
| `jitter_seconds` | 目标入队之间的间隔 | 用于打散探测峰值 |
| `window_size` | 最近多少轮用于窗口成功率 | 推荐 20 |

目标对象中除 `target`、`enabled`、`comment` 外的字段都会作为 Prometheus
标签输出，例如 `env` 和 `alert_schedule`。

`alert_schedule: "business_hours"` 表示该目标只在北京时间 `07:30` 到
`18:30` 触发连通性、丢包和延迟告警；省略该字段表示全天告警。

### 维护停用

维护主机时设置：

```json
{
  "target": "192.0.2.1",
  "env": "example",
  "alert_schedule": "business_hours",
  "enabled": false,
  "comment": "维护中"
}
```

修改配置后重启服务：

```bash
sudo systemctl restart multi_ping_exporter
```

`enabled: false` 的行为：

- 跳过该目标的 ping；
- `ping_target_enabled` 输出为 `0`；
- `ping_probe_reason` 输出 `reason="disabled"`；
- 本项目告警规则不会对该目标触发离线、丢包或高延迟告警；
- `comment` 只用于维护说明，不参与探测原因判断；
- `alert_schedule` 只影响告警规则，不影响 exporter 的实际 ping 探测。

## Prometheus 接入

把 `prometheus_scrape.yml` 中的 job 合并到 Prometheus 的
`prometheus.yml`。如果 exporter 与 Prometheus 不在同一台机器，将
`127.0.0.1:9116` 改成 exporter 的实际地址。

```yaml
scrape_configs:
  - job_name: 'multi_ping'
    scrape_interval: 30s
    scrape_timeout: 10s
    static_configs:
      - targets:
          - '127.0.0.1:9116'
```

检查并重载：

```bash
promtool check config /etc/prometheus/prometheus.yml
curl -X POST http://127.0.0.1:9090/-/reload
```

也可以使用：

```bash
sudo systemctl restart prometheus
```

在 Prometheus 的 Graph 页面验证：

```promql
ping_probe_success{job="multi_ping"}
ping_probe_reason{job="multi_ping"}
ping_target_enabled{job="multi_ping"}
```

## 告警规则

将 `prometheus_rules.yml` 加入 Prometheus 的 rule 文件，并检查：

```bash
promtool check rules /etc/prometheus/prometheus_rules.yml
```

当前规则包括：

- 最近 5 分钟没有成功探测时告警；
- 最近 10 分钟平均丢包率超过 50% 时告警；
- 最近一次平均延迟超过 200 ms 且持续 1 分钟时告警；
- 探测队列持续堆积时告警。

连通性、丢包和延迟规则都带有：

```promql
and on(target) (ping_target_enabled{job="multi_ping"} == 1)
```

因此维护停用目标不会触发这些告警。

### 工作时间告警窗口

连通性、丢包和延迟这三类目标告警对配置了
`alert_schedule: "business_hours"` 的目标使用工作时间告警窗口：

- 北京时间每天 `07:30` 到 `18:30`（不含 `18:30`）正常触发这三类告警；
- `18:30` 到次日 `07:30` 不触发这三类告警；
- 没有配置 `alert_schedule` 或配置其他值的目标全天正常告警；
- `PingExporterProbeBacklog` 不受影响，仍然全天告警。

规则使用 Prometheus 时间戳加 `8` 小时计算北京时间，不依赖 Prometheus
服务器本机时区。

## 指标说明

### 目标和探测状态

| 指标 | 说明 |
| --- | --- |
| `ping_target_info` | 目标静态信息，标签包括 `target`、`env`、`comment` |
| `ping_target_enabled` | 是否启用探测，`1` 是启用，`0` 是停用 |
| `ping_probe_success` | 最近一次探测是否成功 |
| `ping_probe_reason` | 最近一次探测的固定原因标签，值恒为 `1` |
| `ping_probe_sent_packets` | 最近一次发送的包数 |
| `ping_probe_received_packets` | 最近一次收到的包数 |
| `ping_probe_packet_loss_ratio` | 最近一次丢包比例 |
| `ping_probe_rtt_seconds` | 最近一次平均 RTT |
| `ping_probe_min_rtt_seconds` | 最近一次最小 RTT |
| `ping_probe_max_rtt_seconds` | 最近一次最大 RTT |
| `ping_probe_duration_seconds` | 最近一次探测耗时 |

### 统计和运行状态

| 指标 | 说明 |
| --- | --- |
| `ping_probe_window_success_ratio` | 最近窗口成功率 |
| `ping_probe_consecutive_failures` | 连续失败次数 |
| `ping_probe_attempts_total` | 总探测次数 |
| `ping_probe_failures_total` | 总失败次数 |
| `ping_probe_last_probe_timestamp_seconds` | 最近探测时间 |
| `ping_probe_last_success_timestamp_seconds` | 最近成功时间 |
| `ping_exporter_targets` | 配置的目标总数 |
| `ping_exporter_queue_size` | 当前等待探测的队列长度 |

### `ping_probe_reason` 的取值

`reason` 使用固定枚举值，避免把不断变化的原始错误文本放入标签，
从而产生大量 Prometheus 时序：

| reason | 含义 |
| --- | --- |
| `ok` | 最近一次探测成功 |
| `no_reply` | 发送了 ping，但没有收到回包 |
| `command_timeout` | ping 命令执行超过整体超时时间 |
| `command_error` | 系统无法执行 ping 命令 |
| `insufficient_replies` | 收到包数少于 `success_required` |
| `probe_failed` | 探测失败，但无法归入其他固定类型 |
| `not_probed` | 尚未完成首轮探测 |
| `disabled` | 配置中的 `enabled` 为 `false` |

如需查看更完整的系统错误文本，可查询：

```bash
curl -s http://127.0.0.1:9116/targets
```

`/targets` 返回的 `last_result.error` 用于诊断，不会作为 Prometheus 标签。

## Grafana 导入

导入 `grafana_ping_multi_exporter_dashboard.json`：

1. 打开 Grafana；
2. 进入 `Dashboards`；
3. 选择 `Import`；
4. 上传 JSON 文件；
5. 将模板变量 `DS_PROMETHEUS` 映射到实际 Prometheus 数据源；
6. 点击导入并打开仪表盘。

仪表盘包含：

- 状态明细；
- 探测状态；
- 平均和最大延迟；
- 丢包率；
- 最近窗口成功率；
- 连续失败次数；
- 探测耗时；
- 单独的备注信息表。

主表会过滤来自多个 Prometheus 查询的重复 `env`、`comment`、
`alert_schedule`、`job` 和 `instance` 字段，避免出现 `comment 1`、
`comment 2` 等重复列。

## 参数调优

### 目标数量增加时

建议按以下顺序调整：

1. 将 `probe_interval_seconds` 从 15 调整到 30 秒；
2. 保持 `ping_count: 5`，不要为了速度直接降到 1；
3. 根据服务器能力增加 `workers`；
4. 观察 `ping_exporter_queue_size`；
5. 如果队列持续大于 0，将目标拆分到多个 exporter 实例。

### 关于 `ping_count`

`ping_count: 5` 每轮发送 5 个包，可以看到平均、最小、最大延迟和短时
丢包情况。`success_required: 1` 表示 5 个包中至少收到 1 个就算本轮
连通，适合降低偶发丢包造成的离线误报。

## 故障排查

### exporter 没有指标

```bash
sudo systemctl status multi_ping_exporter --no-pager
sudo journalctl -u multi_ping_exporter -n 100 --no-pager
curl -v http://127.0.0.1:9116/healthz
```

### `reason="command_error"`

确认系统存在 `ping`：

```bash
command -v ping
sudo -u prometheus ping -c 1 127.0.0.1
```

### `reason="no_reply"`

在 exporter 所在机器上直接执行同样的探测：

```bash
ping -c 5 <目标地址>
```

如果本机也没有回包，可能是目标设备关闭、路由不通、防火墙过滤 ICMP
或目标只允许特定来源访问。

### `reason="command_timeout"`

检查 exporter 主机负载、路由和目标数量，并观察：

```bash
curl -s http://127.0.0.1:9116/targets
curl -s http://127.0.0.1:9116/metrics | grep ping_exporter_queue_size
```

必要时增加 `probe_timeout_seconds`、`workers` 或拆分目标。

## 安全和限制

- `/metrics`、`/targets` 和 `/healthz` 当前没有认证，建议只在监控内网开放
  `9116` 端口；
- exporter 依赖操作系统的 `ping` 命令，不是 TCP、HTTP 或 DNS 探测器；
- `ping_probe_reason` 只保留固定分类，详细错误通过 `/targets` 查看；
- Prometheus 只读取缓存结果，指标时间取决于 `probe_interval_seconds`；
- 修改目标配置后必须重启 systemd 服务，当前版本不会热加载 JSON。
