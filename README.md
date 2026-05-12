# Mission Control

通用服务器管理仪表盘。零硬编码，全动态发现 — 克隆到任意 Linux 机器即可运行。

## 特性

- **全动态发现** — 通过 systemd + ss 自动发现所有服务，无需手动配置
- **资源监控** — CPU / 内存 / 磁盘 / 运行时间，读取 `/proc`，零外部依赖
- **服务管理** — 启动 / 停止 / 重启 systemd 服务，查看 journalctl 日志
- **可配置分类** — `config.json` 定义核心服务白名单和外部链接
- **一键部署** — `install.sh` 自动检测环境、创建 systemd 守护

## 快速开始

```bash
git clone https://github.com/chengbenchao/mission-control.git
cd mission-control
./install.sh
```

浏览器打开 `http://localhost:8880`。

## 配置

编辑 `config.json`（可选，不配也能跑）：

```json
{
  "infra_patterns": ["nginx", "hermes", "model"],
  "service_urls": {
    "my-app": "https://example.com"
  }
}
```

- `infra_patterns` — 正则匹配，命中则在 UI 归入「核心服务」
- `service_urls` — 服务名 → 外部链接，卡片上显示可点击链接

## 通过 nginx 暴露

```nginx
location /manage/ {
    proxy_pass http://127.0.0.1:8880/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Prefix /manage;
}
```

## 技术栈

- Python 3.10+ 标准库（`http.server`），零 pip 依赖
- 原生 HTML/CSS/JS 单页应用，零前端框架
- systemd user service 守护
