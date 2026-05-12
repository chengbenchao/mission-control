# Mission Control

通用服务器管理仪表盘。服务动态发现，本机配置本地化 — 克隆到任意 systemd Linux 机器即可运行。

## 特性

- **全动态发现** — 通过 systemd + ss 自动发现所有服务，无需手动配置
- **资源监控** — CPU / 内存 / 磁盘 / 运行时间，读取 `/proc`，零外部依赖
- **服务管理** — 启动 / 停止 / 重启 systemd 服务，查看 journalctl 日志
- **可配置分类** — `config.json` 定义核心服务白名单和外部链接
- **Token 认证** — API 默认要求 Bearer token，避免暴露后被跨站控制
- **一键部署** — `install.sh` 自动检测环境、创建 systemd 守护

## 快速开始

```bash
git clone https://github.com/chengbenchao/mission-control.git
cd mission-control
./install.sh
```

浏览器打开 `http://localhost:8880`，输入安装脚本输出的 Login token。
token 默认保存在 `~/.config/mission-control/token`；直接运行 `python3 server.py`
时如果 token 不存在也会自动生成。

## 配置

仓库里的 `config.json` 是可移植默认配置，保持为空即可。每台机器自己的配置写到
`config.local.json`，安装脚本会自动创建它，且它已被 `.gitignore` 忽略。

```json
{
  "infra_patterns": ["nginx", "hermes", "model"],
  "service_urls": {
    "my-app": "https://example.com"
  }
}
```

- `infra_patterns` — 正则匹配，命中则在 UI 归入「核心服务」
- `service_urls` — 服务名 → 外部链接；完整 URL 原样打开，相对路径会按服务端口拼成目标地址
- `CONFIG=/path/to/config.json python3 server.py` — 使用单独配置文件，跳过默认加载顺序

## 通过 nginx 暴露

```nginx
location /manage/ {
    proxy_pass http://127.0.0.1:8880/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Prefix /manage;
}
```

前端会根据当前访问路径自动推断 API 前缀，所以 `/manage/`、`/ops/` 这类前缀不需要改前端代码。
如果反代不剥离前缀，请设置 `MISSION_CONTROL_BASE_PATH=/manage` 或传递
`X-Forwarded-Prefix`。

API 不再开放跨站 CORS。通过 nginx 暴露后仍需要在页面中输入 token；
建议只在 HTTPS 或可信内网中使用。

## 技术栈

- Python 3.10+ 标准库（`http.server`），零 pip 依赖
- 原生 HTML/CSS/JS 单页应用，零前端框架
- systemd user service 守护
