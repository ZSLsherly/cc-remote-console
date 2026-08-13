# CC 远程控制台 v2

手机远程操作 + 监控 Claude Code：**监控会话进度、给专用会话发消息、完整 Web 终端**。
公网可用，端到端加密，主流安全措施。

## 架构

```
手机浏览器（Android / 鸿蒙 / iOS，任意浏览器）
   │  HTTPS（Let's Encrypt，TLS 1.3）— tailscale serve 终止
   ▼
Tailscale 组网（WireGuard 端到端加密，不暴露任何公网端口）
   │
   ▼ 127.0.0.1:8765（HTTP，仅本机）          127.0.0.1:8766（WebSocket 终端）
自建 Python 服务（登录/CSRF/审计 + 监视 + 手机发送 + xterm.js 终端）
```

## 快速开始

1. **首次运行设置密码**（密码只以 PBKDF2 哈希保存）：
   ```
   双击 start.bat   （或命令行: python server\main.py）
   ```
2. **Tailscale 部署**（一次）：
   ```
   winget install Tailscale.Tailscale
   tailscale login       # 浏览器用 outlook 微软账号授权
   tailscale up
   tailscale serve --bg http://127.0.0.1:8765
   tailscale serve --bg --set-path /term --websocket http://127.0.0.1:8766
   ```
   建议在 tailscale.com 管理页把本机名改成好记的名字（如 `mypc`）。
3. **手机**：装 Tailscale 官方 App 并登录同一账号 → 浏览器打开 `https://<主机名>.ts.net`
   → 输入用户名密码 → 监视 / 终端 两个标签页即用。Android 可"添加到主屏幕"安装为 PWA。

## 功能

| 视图 | 能力 |
|---|---|
| 监视 | 所有 CC 会话实时消息流（正文/思考/工具调用/结果）、运行状态、向专用手机会话发消息、停止任务 |
| 终端 | 完整 bash 终端（Git Bash 登录 shell，自带 DeepSeek 环境，直接可跑 `claude`），断线重连不丢会话（重连回放 3000 行） |

手机会话与终端会话完全隔离：手机发的消息只在固定 UUID 的专用会话中执行，
不会干扰电脑上正在跑的终端 CC 会话。

## 安全措施

- **网络**：WireGuard 组网；服务只绑定 127.0.0.1，零公网端口暴露
- **传输**：tailscale serve 提供 Let's Encrypt 证书（TLS 1.3），浏览器真实 HTTPS
- **认证**：PBKDF2-HMAC-SHA256（21 万次迭代 + 随机盐），密码只存哈希（config.json）
- **会话**：256-bit 随机 token 存服务端，HttpOnly + SameSite=Strict + Secure cookie，7 天过期
- **防滥用**：登录失败 5 次锁 5 分钟；登录限流；CSRF 双重提交；WebSocket 握手 cookie 鉴权
- **追溯**：`audit.log` 记录登录成/败、终端连接/断开、手机发送内容、停止事件
- **加固**：CSP / nosniff / X-Frame-Options: DENY；URL 中不含任何敏感参数

## 常用操作

- 重置密码：`python server\main.py --set-passwd 新密码`
- 局域网直连（明文 HTTP，不建议）：`python server\main.py --lan`
- 查看审计日志：`audit.log`

## 备胎方案

`remote_claude.bat`（claude-remote-runner）：走第三方中继 + 端到端加密的手机终端，
Tailscale 方案不可用时临时使用。注意其终端里打印的链接含密钥，勿外传。

## 注意事项

- 终端里执行的命令以当前 Windows 用户权限运行；手机发送的消息由 CC 自动执行（含改文件），
  请谨慎发送
- 服务端 `config.json` / `audit.log` 已加入 .gitignore，勿提交到公开仓库
- Tailscale 账号建议开启设备共享限制，仅让常用设备加入 tailnet
