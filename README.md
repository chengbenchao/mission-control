# 🛰️ Mission Control

> 通用服务器管理仪表盘 — 零硬编码，全动态发现，克隆到任意 Linux 机器即可运行。

---

## ✨ 特性

- 🔍 **全动态发现** — 通过 systemd + ss 自动发现所有服务（user + system scope），无需手动配置
- 📊 **资源监控** — CPU / 内存 / 磁盘 / 运行时间，读取 `/proc`，零外部依赖
- ⚡ **并发健康检查** — 多线程并发探测所有服务，响应速度不随服务数量线性增长
- 🎛️ **服务管理** — 启动 / 停止 / 重启 systemd 服务（自动尝试 user / system scope），查看 journalctl 日志
- 🗂️ **可配置分类** — `config.json` 定义核心服务白名单和外部链接
- 🔐 **Session 认证** — HMAC-SHA256 签名 token，24h TTL，支持 nginx auth_request 共享认证
- 🛡️ **安全加固** — 无默认密码、启动时哈希凭据、登录频率限制、可配置 CORS、XSS 防护、服务名注入防护
- 📣 **Webhook 告警** — 服务状态变化时推送飞书 / 钉钉 / 企微 / 自定义 Webhook
- 🚀 **一键部署** — `install.sh` 交互式设置、自动创建 systemd 守护、引导 Webhook 配置

---

## 🚀 快速开始

```bash
# 方式一：install.sh 交互式部署（推荐）
cd mission-control
./install.sh
# 脚本会提示：用户名、密码、Webhook（可选）

# 方式二：手动指定环境变量
export MC_USERNAME=admin
export MC_PASSWORD=your_strong_password
python3 server.py
```

浏览器打开 `http://localhost:8880` 即可访问。

---

## ⚙️ 环境变量

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `MC_USERNAME` | ✅ | 登录用户名（无默认值，不设置则拒绝启动） |
| `MC_PASSWORD` | ✅ | 登录密码（无默认值，启动时自动 HMAC 哈希，明文不留存） |
| `MC_SESSION_SECRET` | — | HMAC 签名密钥，不设则每次启动自动生成（重启后 session 失效） |
| `PORT` | — | 监听端口，默认 `8880` |
| `HOST` | — | 绑定地址，默认 `127.0.0.1` |
| `CONFIG` | — | config.json 路径，默认同目录 |
| `MC_ALLOWED_ORIGIN` | — | CORS 允许的源，留空则拒绝跨域。生产环境设为具体域名，本地开发可设 `*` |
| `MC_SKIP_PROCS` | — | 跳过的端口监听进程名（逗号分隔），默认 `sshd,systemd,...` 等系统进程 |
| `MC_WEBHOOK_URL` | — | Webhook 推送地址，留空则关闭告警 |
| `MC_WEBHOOK_TYPE` | — | Webhook 类型：`feishu` / `dingtalk` / `wecom` / `custom`，默认 `feishu` |
| `MC_WATCH_INTERVAL` | — | 健康检查轮询间隔（秒），默认 `60` |

> 📄 完整示例参考 `.env.example` 文件。

---

## 📝 配置 config.json

复制 `config.example.json` 为 `config.json` 并按需修改（可选，不配也能运行）：

```json
{
  "base_url": "https://your-domain.com",
  "infra_patterns": ["nginx", "your-app"],
  "service_urls": {
    "your-app": "/app/"
  },
  "service_ports": {
    "your-app": 3000
  },
  "static_sites": [
    {"name": "docs", "url": "/docs/", "desc": "项目文档", "dir": "/var/www/docs"}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `base_url` | 域名前缀，拼接 `service_urls` 中的相对路径。留空则使用相对路径 |
| `infra_patterns` | 正则匹配服务名，命中则在 UI 归入「核心服务」区 |
| `service_urls` | 服务名 → URL 路径，卡片上显示可点击链接 |
| `service_ports` | 手动指定服务端口（自动发现失败时的 fallback） |
| `static_sites` | 纯静态站点，显示在核心服务区 |

> ⚠️ `config.json` **不应包含** IP 地址或密码等敏感信息，敏感配置统一通过环境变量注入。

---

## 📦 移植到新机器

```bash
# 1. 复制项目文件
scp -r ~/.hermes/mission-control/ user@newserver:~/.hermes/

# 2. 在新机器上运行安装脚本
ssh user@newserver
cd ~/.hermes/mission-control
./install.sh   # 重新输入用户名和密码

# 3. 按需重建 config.json（服务名和 URL 因机器不同需重新配置）
cp config.example.json config.json
# 编辑 config.json

# 4. 配置 nginx 反向代理（见下方）
```

---

## 🔗 共享认证（nginx auth_request）

其他服务可复用 Mission Control 的 session，实现单点登录效果：

```nginx
location = /auth {
    internal;
    proxy_pass http://127.0.0.1:8880/api/auth-check;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Cookie $http_cookie;
}

location /protected/ {
    auth_request /auth;
    error_page 401 = @login_redirect;
    proxy_pass http://127.0.0.1:3000/;
}

location /manage/ {
    proxy_pass http://127.0.0.1:8880/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Prefix /manage;
}

location @login_redirect {
    return 302 /manage/login;
}
```

---

## 📋 查看日志

```bash
# 🔴 实时跟踪（含访问日志、登录记录、告警）
journalctl --user -u mission-control.service -f

# 📜 查看最近 100 条
journalctl --user -u mission-control.service -n 100
```

---

## 🔒 安全建议

- 🔑 密码使用随机强密码：`openssl rand -base64 20`
- 🗝️ 设置固定 `MC_SESSION_SECRET`，避免重启后用户被强制退出
- 🌐 生产环境 `HOST` 保持 `127.0.0.1`，通过 nginx 反向代理对外暴露
- 🚫 `MC_ALLOWED_ORIGIN` 设为具体域名，不要在生产使用 `*`
- 🔄 定期轮换密码：重新运行 `./install.sh` 即可

---

## 🛠️ 技术栈

| 层次 | 技术 |
|------|------|
| 🐍 后端 | Python 3.10+ 标准库（`http.server` + `concurrent.futures`），**零 pip 依赖** |
| 🎨 前端 | 原生 HTML / CSS / JS 单页应用，**零前端框架** |
| ⚙️ 守护 | systemd user service |
