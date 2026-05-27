# Mission Control

通用服务器管理仪表盘。零硬编码，全动态发现 — 克隆到任意 Linux 机器即可运行。

## 特性

- **全动态发现** — 通过 systemd + ss 自动发现所有服务（user + system scope），无需手动配置
- **资源监控** — CPU / 内存 / 磁盘 / 运行时间，读取 `/proc`，零外部依赖
- **并发健康检查** — 多线程并发探测所有服务，响应速度不随服务数量线性增长
- **服务管理** — 启动 / 停止 / 重启 systemd 服务（自动尝试 user / system scope），查看 journalctl 日志
- **可配置分类** — `config.json` 定义核心服务白名单和外部链接
- **Session 认证** — HMAC-SHA256 签名 token，24h TTL，nginx auth_request 共享认证
- **安全加固** — 无默认密码、登录频率限制、访问日志、服务名注入防护、XSS 防护
- **一键部署** — `install.sh` 自动检测环境、交互式设置密码、创建 systemd 守护

## 快速开始

```bash
# 方式一：install.sh 交互式部署（推荐）
cd mission-control
./install.sh
# 脚本会提示输入用户名和密码，自动写入 systemd service

# 方式二：手动指定环境变量
export MC_USERNAME=admin
export MC_PASSWORD=your_strong_password
python3 server.py
```

浏览器打开 `http://localhost:8880`。

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `MC_USERNAME` | ✅ | 登录用户名（无默认值，不设置则拒绝启动） |
| `MC_PASSWORD` | ✅ | 登录密码（无默认值，不设置则拒绝启动） |
| `MC_SESSION_SECRET` | 可选 | HMAC 签名密钥，不设则每次启动自动生成（重启后 session 失效） |
| `PORT` | 可选 | 监听端口，默认 `8880` |
| `HOST` | 可选 | 绑定地址，默认 `127.0.0.1` |
| `CONFIG` | 可选 | config.json 路径，默认同目录 |

参考 `.env.example` 文件。

## 配置 config.json

编辑 `config.json`（可选，不配也能运行）：

```json
{
  "infra_patterns": ["nginx", "hermes", "model"],
  "service_urls": {
    "my-app": "https://example.com"
  },
  "static_sites": [
    {"name": "文档站", "url": "/docs/", "desc": "项目文档"}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `infra_patterns` | 正则匹配服务名，命中则在 UI 归入「核心服务」 |
| `service_urls` | 服务名 → URL，卡片上显示可点击链接 |
| `static_sites` | 纯静态站点，显示在核心服务区 |

## 移植到新机器

```bash
# 1. 复制项目文件
scp -r ~/.hermes/mission-control/ user@newserver:~/.hermes/

# 2. 在新机器上运行安装脚本
ssh user@newserver
cd ~/.hermes/mission-control
./install.sh   # 会提示设置新密码

# 3. 按需修改 config.json（服务名和 URL 因机器不同需重新配置）
# 4. 配置 nginx 反向代理（见下方）
```

## 共享认证（nginx auth_request）

其他服务可复用 Mission Control 的 session：

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

## 查看日志

```bash
# 服务运行日志（含访问日志、登录记录）
journalctl --user -u mission-control.service -f

# 查看最近 100 条
journalctl --user -u mission-control.service -n 100
```

## 技术栈

- Python 3.10+ 标准库（`http.server` + `concurrent.futures`），零 pip 依赖
- 原生 HTML/CSS/JS 单页应用，零前端框架
- systemd user service 守护
