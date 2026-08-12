# 下载中间商

<p align="center">
  <img src="https://img.shields.io/badge/Platform-AstrBot-236b8e" alt="Platform">
  <img src="https://img.shields.io/badge/Language-Python-%233776AB?logo=python" alt="Language">
  <img src="https://img.shields.io/badge/OneBot%20V11-Supported-%2366ccff" alt="OneBot V11">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/Downloads-curl%20%26%20git-blue" alt="Downloads">
</p>

AstrBot 插件：通过 `/DW` 指令下载文件或克隆 GitHub 仓库，并经 OneBot V11 发送给用户。

## 功能特性

- 支持下载任意可下载文件（优先使用 `curl`）
- 支持克隆 GitHub 仓库并自动压缩为 `.zip`（压缩后删除原文件夹）
- 文件小于 50MB 时直接发送文件给用户；大于等于 50MB 时发送国内下载地址
- 每次下载前自动清理下载目录中停留超过 3 天的文件
- 下载日志记录到 `data/plugin_data/astrbot_plugin_download_middleman/Downloads.log`（含时间、网址、文件名、大小、是否成功）

## 使用方法

发送 `/DW [网址]` 即可触发，例如：

```
/DW https://example.com/files/app_v1.2.zip
/DW https://github.com/AstrBotDevs/AstrBot
```

- 网址为 GitHub 仓库地址（`https://github.com/owner/repo` 或带 `.git`）时，克隆并打包为 `.zip` 发送。
- 网址为其他可下载文件时，直接下载后发送。
- 网址无效（404、连接失败、非 http/https 链接等）时回复"网址无效"。

## 配置项

可在 AstrBot WebUI 插件配置页修改：

| 配置项 | 说明 |
|---|---|
| `download_dir` | 下载目录。留空则使用默认目录 `data/plugin_data/astrbot_plugin_download_middleman/DownloadFiles/`；支持绝对路径或相对插件数据目录的路径 |
| `download_url_prefix` | 大文件（≥50MB）的国内下载地址前缀，例如 `http://example.com/downloads`。留空时，大文件回退为直接通过 OneBot 发送 |

### 配合 Nginx 提供下载

`download_url_prefix` 可配合 Nginx 静态文件服务使用，把下载目录暴露为公网可访问的地址，供用户下载大文件：

1. 用 Nginx 托管下载目录（默认 `data/plugin_data/astrbot_plugin_download_middleman/DownloadFiles/`）；
2. 在插件配置中把 `download_url_prefix` 设为 Nginx 对应的访问地址。

Nginx 配置示例：

```nginx
location /downloads/ {
    alias /AstrBot/data/plugin_data/astrbot_plugin_download_middleman/DownloadFiles/;
    autoindex off;
}
```

配置后 `download_url_prefix` 填 `https://example.com/downloads`，大文件会以 `国内下载地址：https://example.com/downloads/<文件名>` 的形式发送给用户。

## 部署说明

AstrBot 与 OneBot 协议端（NapCat、Lagrange 等）位于不同机器/容器时，协议端无法直接访问 AstrBot 侧的本地文件路径。插件会自动处理：

- 已配置 AstrBot 的 `callback_api_base`（文件服务）时，文件通过回调链接下发；
- 未配置时，文件内容以 `base64://` 内联到消息段，由协议端自行解码发送。

建议在 AstrBot 配置中设置 `callback_api_base`，大文件走文件服务更省流量。

## 安装

将本目录内容复制到 `AstrBot/data/plugins/<插件名>/`，在 WebUI 插件管理中启用并重载插件。

依赖：`curl`、`git`（系统命令），无需第三方 Python 库。
