# CC Remote Console · Claude Code 手机远程控制台

在手机上**监视和控制电脑上的 Claude Code**：实时查看所有会话进度、向任意会话发消息执行任务、完整的 Web 终端（多终端标签 + 重命名）。公网可用，端到端加密，主流安全措施齐备。

## 功能特性

| 模块 | 能力 |
|---|---|
| 📊 **监视** | 所有 CC 会话的实时消息流（正文 / 思考 / 工具调用 / 结果与错误），长会话分页加载，可拖动滚动条 |
| 📱 **发消息** | 手机向**任意 CC 会话**发送指令（`claude -p --resume` 续聊执行），结果实时回显；电脑端正在处理的会话自动拦截；支持 `/clear` 清空上下文、`/help` 查看命令 |
| 🖥️ **终端** | 完整 bash 终端（Git Bash 登录 shell，自带 DeepSeek 环境可直接跑 `claude`）；多终端标签：切换 / 关闭 / 重命名 / 断线重连回放 3000 行 |
| 🔔 **电脑联动** | 手机发指令 / 任务完成 / 失败时，电脑右下角弹系统通知；电脑浏览器打开监视页同样实时可见 |
| 🔐 **安全** | 见下文安全措施表 |

## 架构

```
手机浏览器（Android / 鸿蒙 / iOS，任意浏览器，可 PWA 安装）
   │  HTTPS（Let's Encrypt，TLS 1.3）— tailscale serve 终止
   ▼
Tailscale 组网（WireGuard 端到端加密，零公网端口暴露）
   │
   ▼ 127.0.0.1:8765（HTTP，仅本机）          127.0.0.1:8766（WebSocket 终端）
自建 Python 服务（登录/CSRF/审计 + 监视 + 手机发送 + xterm.js 终端）
   │
   ├─ 只读尾随 ~/.claude/projects/**/*.jsonl（监视数据源，不干扰 CC）
   ├─ 派生 claude -p --resume <sessionId>（手机发消息）
   └─ winpty(ConPTY) 派生 Git Bash（Web 终端）
```

## 安全措施

| 层 | 措施 |
|---|---|
| 网络 | WireGuard 组网；服务只绑定 127.0.0.1，零公网端口暴露 |
| 传输 | tailscale serve 提供 Let's Encrypt 证书（TLS 1.3），浏览器真实 HTTPS |
| 认证 | PBKDF2-HMAC-SHA256（21 万次迭代 + 随机盐），密码只存哈希（config.json） |
| 会话 | 256-bit 随机 token 存服务端；HttpOnly + SameSite=Strict + Secure cookie；7 天过期 |
| 防滥用 | 登录失败 5 次锁 5 分钟；登录限流；CSRF 双重提交；WebSocket 握手 cookie 鉴权 |
| 追溯 | audit.log 记录登录成/败、终端连接/断开、手机发送内容、停止事件 |
| 加固 | CSP / nosniff / X-Frame-Options: DENY；URL 不含任何敏感参数 |

## 快速开始

### 1. 依赖

- Windows 10/11，Python 3.10+，Git for Windows（提供 bash）
- Node.js（仅用于安装 Claude Code 本体）

```bash
pip install pywinpty websockets
```

### 2. 首次启动（生成登录凭据）

```bash
python server/main.py          # 交互式设置用户名与密码（仅存哈希）
```

### 3. Tailscale 组网（一次配置）

```bash
winget install Tailscale.Tailscale
tailscale up                   # 浏览器登录（微软/Apple/Google 账号任一）
tailscale serve --bg http://127.0.0.1:8765
tailscale serve --bg --set-path /term http://127.0.0.1:8766
```

### 4. 使用

- **电脑**：双击 `start.bat`（自愈式启动器：已运行则直接打开页面，端口被残留进程占用会自动清理）
- **手机**：安装 Tailscale App 并登录同一账号 → 浏览器打开 `https://<主机名>.ts.net` → 登录 → 监视 / 📱 发消息 / 终端
- Android 可用浏览器菜单"添加到主屏幕"安装为 PWA

## 配置与运维

| 操作 | 命令 |
|---|---|
| 重置密码 | `python server\main.py --set-passwd 新密码` |
| 局域网直连（明文，不建议） | `python server\main.py --lan` |
| 审计日志 | `audit.log`（与 config.json 同属敏感文件，已 gitignore） |

## 并发使用规则（手机 + 电脑同时操作）

- 手机发消息与电脑终端**操作的是同一份会话文件**，遵循：
  1. 电脑 CC 正在该会话中**生成回复**时，手机发送会被拒绝（423）；检测基于 CC 自带的会话登记文件（`~/.claude/sessions/`），电脑一处理完立刻恢复，不存在固定等待时长
  2. 电脑 CC 只是**停留**在某个会话（未在生成）时手机也可以发送：CC 内部队列会串行两边消息，不会互相覆盖（已实测并发写无损坏）
  3. 电脑想看手机的操作结果：监视页实时可见，或在 CC 中用 `claude --resume <会话ID>` 重开即读回完整历史
  4. 想要真并行请各开各的会话
- 手机端 `/clear`（清空上下文）：旧记录备份为同目录下 `.jsonl.bak`，下一条消息自动以同一会话 ID 开启全新对话；电脑 CC 正停留该会话时会被拒绝，切走后再清

## 手机端斜杠命令

| 命令 | 效果 |
|---|---|
| `/clear` | 清空所选会话上下文，开启全新对话（旧记录备份为 `.jsonl.bak`） |
| `/help` | 查看手机端支持的命令列表 |

其余斜杠命令（`/compact`、`/model` 等）是无头模式下的交互指令，手机端无法执行，会给出明确提示；`claude -p` 会在上下文接近上限时自动压缩，无需手动 `/compact`。

## 目录结构

```
cc-monitor/
├── server/
│   ├── main.py        # 入口：登录/PBKDF2/CSRF/锁out/限流/审计 + HTTP 路由 + 静态服务
│   ├── store.py       # transcript 增量解析（字节游标 + 半行缓冲）+ 系统噪音过滤
│   ├── send.py        # 任意会话发送管理器（claude -p --resume，串行单飞，运行中拦截）
│   ├── term.py        # 终端会话管理（winpty ConPTY + 环形缓冲 3000 行 + 多终端 + 重命名）
│   ├── ws.py          # WebSocket 服务（websockets，cookie 鉴权，断连静默恢复）
│   └── webutil.py     # 工具：claude shim 解析 / Git Bash 探测 / Windows 通知等
├── static/            # 移动端前端（登录/监视/终端三视图 + PWA + xterm.js 离线打包）
├── start.bat          # 自愈式启动器（健康检查 / 残留端口清理 / bash 路径探测）
├── cc_monitor_v1.py   # v1 单文件版（LAN 监视器，历史存档）
└── remote_claude.bat  # 备胎：claude-remote-runner（第三方中继方案）
```

## 隐私说明

- 本仓库**不包含任何 AI 会话数据**：Claude Code 的会话记录（transcript）位于 `~/.claude/projects/`，服务器只读访问，从不复制进项目目录
- `config.json`（密码哈希）与 `audit.log`（操作审计）已 gitignore，不会上传

## 常见问题

- **手机打不开页面**：检查手机 Tailscale App 是否 Connected（电池优化需设为"无限制"）；电脑上服务窗口是否开着
- **电脑浏览器打不开 ts.net**：系统代理会拦截，加 `*.ts.net` 直连规则，或直接访问 `http://localhost:8765`
- **终端列表为空**：确认服务端版本含 `/api/terms` 接口（重启 start.bat 加载最新代码）
- **手机切后台终端断开**：正常现象，回到页面自动重连并回放历史；窗口里的连接异常日志已静默处理

## License

MIT（可自行添加 LICENSE 文件）
