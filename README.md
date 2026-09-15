# PT 站点每日自动签到系统

[![CI](https://github.com/ilvsx/pt-checkin/actions/workflows/ci.yml/badge.svg)](https://github.com/ilvsx/pt-checkin/actions/workflows/ci.yml)
[![Build and Push Docker Image](https://github.com/ilvsx/pt-checkin/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/ilvsx/pt-checkin/actions/workflows/docker-publish.yml)
[![ghcr.io](https://img.shields.io/badge/ghcr.io-ilvsx%2Fpt--checkin-blue)](https://github.com/ilvsx/pt-checkin/pkgs/container/pt-checkin)

面向 PT 站（已适配 **HHClub / hhanclub.net**，NexusPHP 系）的完整签到解决方案：

- ⏰ **每日定时自动签到**：按账号设置时间点，带稳定随机延迟，进程重启后不会漂移
- 📊 **签到记录可查**：每次尝试的完整审计（状态、获得积分、连续天数、排名、耗时、错误原因）
- 🎯 **精确判断当日是否已签到**：直接解析站点页面内嵌的签到台账，而不是靠脆弱的文字匹配
- 🖥️ **可视化 Web 界面**：概览 / 记录 / 日历 / 设置四个页面，零前端构建
- 🔔 **结果通知**：通用 Webhook、Server酱、Bark、Telegram、ntfy
- 📦 **零第三方依赖**：仅用 Python 标准库即可运行，NAS / 小主机 / 容器都友好

---

## 1. 它是如何"准确判断今天有没有签到"的

这是本项目的核心。抓包分析 `attendance.php` 后发现，HHClub 的签到页在 HTML 里内嵌了两段**机器可读**的数据：

```javascript
// ① 站点认定的"今天"
const nowDate = new Date("2026/09/15");

// ② 按日期索引的签到台账
let data = {"2026-09-14":{"id":5795223,"uid":12345,"points":120,"date":"2026-09-14",
            "is_retroactive":0,"created_at":"2026-09-14 00:54:51", ...},
            "2026-09-15":{"id":5805306,"uid":12345,"points":125,"date":"2026-09-15",
            "is_retroactive":0,"created_at":"2026-09-15 11:07:51", ...}};
```

因此判断逻辑是确定的：

> **`nowDate` 格式化成的日期，是否存在于台账 `data` 中。**

配套的判定细节：

| 场景 | 判定依据 |
| --- | --- |
| 今日已签到 | `nowDate` 是 `data` 的键，且站点 `created_at` **早于**本次请求（说明不是本次签的） |
| 本次签到成功 | `nowDate` 是 `data` 的键，且 `created_at` 落在本次请求时间附近，且本地此前无今日记录 |
| 签到未生效 | 页面正常登录，但台账中**没有**今天 → 判为失败并重试 |
| Cookie 失效 | 页面返回 HTTP 200 但被截断、不含 `nowDate` / 台账 → 判为 `auth_failed` |

**关键前提**：在 HHClub 上 `GET /attendance.php` 本身就是签到动作，且**对同一天幂等** ——
已签到时会原样回显当日结果，不会重复签到、不会重复计分。所以"查询状态"和"执行签到"
是同一个请求，反复调用是安全的。

此外还有文本兜底：`这是您的第24次签到，已连续签到24天，本次签到获得125个憨豆`
与 `今日签到排名：1234 / 5678`，在台账结构变化时仍可工作。

---

## 2. 快速开始

### 2.1 运行（无需安装任何依赖）

```bash
cd pt-checkin
python3 run.py
```

然后浏览器打开 **http://127.0.0.1:8787** ，点击「+ 添加账号」。

### 2.2 添加账号（两种方式）

**方式 A：粘贴 cURL（推荐）**

在浏览器中登录 PT 站 → F12 → Network → 点击签到按钮 → 右键该请求 → Copy as cURL，
粘贴到界面的「① 粘贴 cURL」框，点「解析并填入」，站点地址、Cookie、User-Agent 会自动识别。

**方式 B：命令行**

```bash
python3 run.py add --curl - --name 'HHClub 主号' < 抓包内容.txt

# 或手动指定
python3 run.py add --name 'HHClub 主号' \
  --url https://hhanclub.net \
  --cookie 'c_secure_uid=...; c_secure_pass=...; c_secure_login=...'
```

添加时会立即校验 Cookie 并把站点近一个月的签到台账同步到本地。

> Cookie 获取位置：浏览器 F12 → Application → Cookies → `hhanclub.net`，
> 复制 `c_secure_uid`、`c_secure_pass`、`c_secure_ssl`、`c_secure_tracker_ssl`、`c_secure_login` 五个字段。

### 2.3 命令行速查

```bash
python3 run.py                 # 启动 Web + 定时调度（默认）
python3 run.py --help          # 查看全部命令

python3 run.py status          # 各账号今日状态 / 下次运行时间
python3 run.py once            # 立即签到一次（全部账号）
python3 run.py once --account 'HHClub 主号' --verbose
python3 run.py accounts        # 列出账号
python3 run.py add --curl - < 抓包.txt
python3 run.py remove --account 'HHClub 主号'
python3 run.py curl 'curl ...'  # 只解析 curl，不入库
python3 run.py notify-test     # 发一条测试通知
python3 run.py doctor          # 环境自检
```

---

## 3. Web 界面

| 页面 | 内容 |
| --- | --- |
| **概览** | 统计卡片（今日已签到 / 待签到 / 失败）、调度器状态与下次运行、每个账号的状态卡（含立即签到 / 刷新 / 日历 / 编辑 / 删除） |
| **签到记录** | 按账号、状态、日期区间筛选，分页浏览每一次尝试；「查看」可展开解析详情与站点原文提示 |
| **签到日历** | 月历视图，绿=已签到、蓝=补签、红=漏签、蓝框=今天；显示当月签到天数与累计憨豆 |
| **设置** | 调度参数、网络与安全、通知渠道配置（可增删多种渠道并发送测试通知） |

---

## 4. 配置说明

配置文件位于 `data/config.json`（首次在界面「设置」页保存，或用环境变量/命令行修改后生成；
不存在时全部使用内置默认值），也可在界面「设置」页修改。

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `timezone` | `Asia/Shanghai` | 全局时区，决定"今天"和签到时刻 |
| `default_schedule_time` | `00:30` | 默认每日签到时间（站点时区） |
| `default_jitter_seconds` | `600` | 随机延迟上限；按 `(账号, 日期)` 哈希，**同一天重启不漂移** |
| `max_retries_per_day` | `5` | 当日失败后的最大重试次数 |
| `retry_interval_seconds` | `900` | 两次尝试之间的最小间隔 |
| `request_timeout` | `30` | 单次请求超时（秒） |
| `verify_ssl` | `true` | 是否校验 TLS 证书 |
| `proxy` | 空 | 例：`http://127.0.0.1:7890` |
| `scheduler_enabled` | `true` | 是否启用自动签到 |
| `scheduler_tick_seconds` | `20` | 调度器检查间隔 |
| `keep_raw_days` | `30` | 原始响应片段保留天数 |
| `web.host` / `web.port` | `127.0.0.1` / `8787` | 监听地址与端口 |
| `web.auth_token` | 空 | 非空时所有请求需携带令牌（`?token=` 或 `X-Auth-Token`） |

**账号级覆盖**：每个账号可单独设置 `schedule_time`、`jitter_seconds`、`timezone`，
留空表示使用全局默认。

**环境变量**：`PTCHECKIN_DATA`（数据目录）、`PTCHECKIN_HOST`、`PTCHECKIN_PORT`、
`PTCHECKIN_TOKEN`、`PTCHECKIN_PROXY`、`PTCHECKIN_TZ`。

### 调度行为

- 到达时间点且当日未签到 → 执行
- 失败 → 按 `retry_interval_seconds` 重试，直到 `1 + max_retries_per_day` 次为止
- **启动补签**：进程重启后若已过签到时间且当天未签到，会立即补一次（不会漏签）
- 当日已签到 → 不再重复请求，只在次日时间点再执行

### 通知渠道

在「设置 → 通知」中添加，支持：

| 类型 | 必填字段 |
| --- | --- |
| `webhook` | `url`（POST JSON，字段 `title` / `body` / `text`） |
| `serverchan` | `sendkey` |
| `bark` | `url`（默认 `https://api.day.app`）、`key` |
| `telegram` | `bot_token`、`chat_id` |
| `ntfy` | `url`（含 topic）、可选 `token` |

可分别控制「成功 / 已签到 / 失败 / 恢复」是否通知；失败通知只在首次失败和最后一次重试时发送，避免刷屏。

---

## 5. 部署

### Docker（推荐）

CI 会自动构建多架构镜像（`linux/amd64` + `linux/arm64`）并推送到 GitHub Container Registry。

**方式一：直接使用预构建镜像**

```bash
cd pt-checkin
docker compose pull && docker compose up -d
# 打开 http://<主机IP>:8787
```

**方式二：本地自行构建**（不依赖 GHCR）

```bash
cd pt-checkin
docker compose up -d --build
```

也可以不用 compose：

```bash
docker run -d --name pt-checkin --restart unless-stopped \
  -p 8787:8787 \
  -v "$PWD/data:/data" \
  -e TZ=Asia/Shanghai \
  -e PTCHECKIN_TOKEN=change-me \
  ghcr.io/ilvsx/pt-checkin:latest
```

数据持久化在 `./data`。对外暴露时请设置 `PTCHECKIN_TOKEN`（compose 里取消注释即可）。

> **首次使用 GHCR 镜像注意**：Actions 推送的包默认是 **私有** 的。若希望匿名拉取，
> 请到 GitHub 仓库右侧 **Packages → pt-checkin → Package settings → Change visibility**
> 改为 Public；否则需要先 `docker login ghcr.io` 再拉取。

**可用标签**

| 标签 | 说明 |
| --- | --- |
| `latest` | main 分支最新构建 |
| `main` | main 分支 |
| `sha-xxxxxxx` | 对应提交的短 SHA |
| `1.2.3` / `1.2` / `1` | 推送 `v1.2.3` 这类 tag 时自动生成 |
| `pr-N` | PR 构建（仅验证，不推送） |

### systemd

```bash
sudo useradd -r -s /usr/sbin/nologin ptcheckin
sudo mkdir -p /opt/pt-checkin /var/lib/pt-checkin
sudo cp -r . /opt/pt-checkin/
sudo chown -R ptcheckin:ptcheckin /var/lib/pt-checkin
sudo cp deploy/pt-checkin.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pt-checkin
```

---

## 6. 数据与安全

数据全部存放在 `data/` 目录：

```
data/
├── config.json     # 配置（不含明文 Cookie）
├── ptcheckin.db    # SQLite：账号、每日台账、每次尝试记录
├── secret.key      # Cookie 加密密钥（0600）—— 请务必备份！
└── logs/           # 滚动日志
```

- Cookie 使用 **Fernet（AES-128-CBC + HMAC）** 加密后入库；缺少 `cryptography`
  时自动退化为带完整性校验的混淆方案，并在界面与日志中明确提示。
- 界面上 Cookie 始终脱敏显示（如 `c_secure_pass=********`）。
- **`secret.key` 丢失后已保存的 Cookie 将无法解密**，需要重新添加账号。
- 默认只监听 `127.0.0.1`。若需远程访问，请设置 `web.auth_token` 并使用局域网/反代 + HTTPS。
- **请勿把真实 Cookie 提交到 Git 仓库**。`data/`、`.probe/` 已在 `.gitignore` 中排除；
  测试与文档中一律使用占位符（如 `c_secure_pass=0123456789abcdef0123456789abcdef`）。
  一旦真实 Cookie 被推送或分享，请立即在站点重新登录以使其失效。

---

## 7. 常见问题

**Q：会不会一天签到两次、重复扣分？**
不会。站点接口对同一天幂等，且本系统在 `checkin_days` 台账里记录每日状态，
当日已签到后调度器不再发起请求（已通过实测验证）。

**Q：状态显示"今日已签到"但我想确认是不是真的签上了？**
看「签到记录」里该条的站点提示，或打开站点的签到日历。本系统显示的状态直接来自站点台账，
不是本地猜测。

**Q：Cookie 失效了怎么办？**
状态会变成 `Cookie 失效` 并触发通知。重新抓一次 Cookie，在账号「编辑」里粘贴保存即可（留空表示不修改）。

**Q：站点改版导致解析失败？**
签到记录里会保留解析详情（`raw_snippet`）。解析优先走结构化数据，另有文本兜底；
若站点大改，一般只需调整 `ptcheckin/parser.py` 中的正则。

**Q：为什么默认签到时间是 00:30？**
站点每日 00:00 重置，00:30 左右签到可以保证连续天数不断，同时避开整点的请求高峰。
默认还会叠加 0–10 分钟随机延迟，进一步分散请求。

---

## 8. 项目结构

```
pt-checkin/
├── run.py                    # 入口
├── requirements.txt           # 仅可选依赖（cryptography）
├── Dockerfile / docker-compose.yml / .dockerignore
├── .github/workflows/
│   ├── ci.yml                 # 单元测试矩阵 + 敏感信息扫描
│   └── docker-publish.yml     # 构建多架构镜像并推送到 GHCR
├── deploy/pt-checkin.service  # systemd 单元
├── ptcheckin/
│   ├── cli.py                 # 命令行
│   ├── settings.py            # 配置
│   ├── store.py               # SQLite 持久层
│   ├── parser.py              # 页面解析（核心：nowDate + 台账 JSON）
│   ├── client.py              # HTTP 客户端（标准库）
│   ├── checkin.py             # 签到主流程
│   ├── scheduler.py           # 定时调度 / 重试 / 补签
│   ├── curlparse.py           # curl 抓包解析
│   ├── notify.py              # 通知渠道
│   ├── secretsbox.py          # Cookie 加密
│   └── web/                   # Web 服务 + 前端（原生 JS）
└── tests/                     # 98 个单元测试
```

---

## 9. 测试

```bash
cd pt-checkin
python3 -m unittest discover -s tests -t .
```

覆盖：页面解析（含字符串内花括号、未登录页、结构变化兜底）、curl 解析（使用真实抓包）、
持久层（加密、台账、连续天数、分页、清理）、签到流程（成功/已签到/失败/失效/网络错误/并发保护）、
调度逻辑（定时、随机延迟、重试、上限、补签）。

CI（`.github/workflows/ci.yml`）会在 Python 3.10 / 3.11 / 3.12 上跑同一套测试，
并附带一道**敏感信息扫描**：一旦有 `data/`、`secret.key` 或真实 Cookie 被提交，CI 会直接失败。

---

## 10. 免责声明

本项目仅用于自动化个人的每日签到操作，不修改站点数据、不绕过任何访问控制。
请遵守所使用站点的规则，因使用本工具产生的任何后果由使用者自行承担。
