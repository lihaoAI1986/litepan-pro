# LitePan-Pro

> 基于 [Ponphil/LitePan](https://github.com/Ponphil/LitePan) 的升级版：
> **STRM 链接格式 + 302 反代方式 全面对齐 [walkingddd/TgtoDrive](https://github.com/walkingddd/TgtoDrive) 风格。**

## 🚀 一键安装

> 完整部署指南：[DEPLOY.md](./DEPLOY.md)

镜像由 GitHub Actions 自动构建并发布到 GitHub Container Registry：

```bash
docker run -d \
  --name litepan-pro \
  --restart unless-stopped \
  --network host \
  -e TZ=Asia/Shanghai \
  -v /opt/litepan-pro/data:/app/data \
  -v /opt/litepan-pro/log:/app/log \
  -v /opt/litepan-pro/strm:/app/strm \
  -v /opt/litepan-pro/plugins:/app/plugins \
  ghcr.io/<你的GitHub用户名>/litepan-pro:latest
```

浏览器访问 `http://你的NAS_IP:5211`。

> ⚠️ `<你的GitHub用户名>` 替换成你发布镜像时用的 GitHub 用户名。

## 📌 本次升级做了什么？

LitePan 原有 STRM 是 `http://host:5211/api/strm/play/{account_id}/{file_id}?token=&sign=` 这种带 file_id 的形式，
在 Emby / Infuse 场景下偶尔有兼容性/性能问题。

LitePan-Pro 新增 **v3 路径直跳格式**，STRM 内容形如：

```
http://your-host:5211/d/1/Movies/阿凡达 (2009) {tmdb-19995}/阿凡达.mkv
```

播放器请求这个 URL 时：

1. LitePan-Pro 查 `(account_id, remote_path) -> file_id` 缓存
2. 用 file_id 拿网盘直链
3. **302 跳到 115/123 等网盘 CDN**
4. 客户端从 CDN 直接拉流

完全复刻 TgtoDrive 的 `/d/<path>` 反代 + 30 分钟内存缓存 + 同目录预缓存设计。

## 兼容性

| 链接格式 | URL 样例 | 兼容性 | 备注 |
|---|---|---|---|
| **v3**（推荐） | `http://host:5211/d/1/Movies/...` | ✅ Emby / Infuse 最友好 | 新部署默认 |
| v1 | `/api/strm/play/1/{fid}?token=&sign=` | ⚠️ 老格式 | 兼容老 STRM 文件 |
| v2 | `/api/strm/v2/play/1/{base64fid}/t/{tk}/s/{sig}` | ⚠️ 老格式 | 兼容老 STRM 文件 |

v1 / v2 路由继续保留，**不会破坏现有部署**。

## v3 路径缓存

| 项 | 默认值 | 说明 |
|---|---|---|
| 路径缓存 TTL | 1800 秒（30 分钟） | 缓存 `(path -> CDN 直链)`，到期重取 |
| 路径缓存容量 | 5000 条 | LRU，超出后淘汰最旧条目 |
| 同目录预缓存 | 开 | 播放一集时预热同目录其它兄弟文件 |
| 预缓存批大小 | 100 | 单次预取的最大兄弟文件数 |
| 302 Cache-Control max-age | 300 秒 | 浏览器/CDN 可缓存 302 结果 |

缓存分两层：
- **进程内 LRU** (`core/path_download_cache.py`)：30 分钟热路径，命中后秒级 302
- **SQLite 持久化** (`database/db.py` 表 `path_file_cache`)：STRM 同步时回填，重启后保留

## 支持的网盘驱动

与原 LitePan 一致：

- ✅ 115 网盘 Open（v3 路径直跳优先适配）
- 123 云盘 Open / Reverse
- 139 云盘、189 云盘、夸克网盘、百度网盘、光鸭云盘、移动云盘、天翼云盘、OneDrive、WebDAV

> **注意**：v3 路径直跳格式目前**优先支持 115 Open**；其它驱动切换到 v3 后会自动 fallback 到原 file_id 行为。

## 仓库差异一览

相对于原 LitePan，本项目新增/修改：

| 文件 | 状态 | 说明 |
|---|---|---|
| `core/strm_security.py` | 修改 | 增加 `build_strm_v3_play_url`、`parse_strm_v3_play_path` |
| `core/path_download_cache.py` | **新增** | LRU + TTL 缓存，仿 TgtoDrive url_cache |
| `core/driver_service.py` | 修改 | 增加 `resolve_path_to_file`、`list_sibling_paths_for_precache` |
| `core/strm_sync_manager.py` | 修改 | 支持 v3 链接写入，同步回填 `path_file_cache` |
| `database/db.py` | 修改 | 新增 `path_file_cache` 表与方法 |
| `api/path_play.py` | **新增** | `/d/{account_id}/{file_path}` 302 反代入口 |
| `api/strm_admin.py` | 修改 | 增加 v3 / 路径缓存相关配置项 |
| `main.py` | 修改 | 注册 `path_play_router` |
| `web/src/components/admin/StrmGenerator.vue` | 修改 | 增加 v3 选项 + 路径缓存设置 UI |
| `.github/workflows/docker.yml` | **新增** | GitHub Actions 自动构建并发布到 ghcr.io |
| `DEPLOY.md` | **新增** | 完整部署指南 |
| `push-to-github.ps1` | **新增** | Windows 一键推送脚本 |
| `README.md` | 替换 | 本文件 |
| `DOCKER.md` | 追加 | 新增环境变量说明 |

## 许可证

本项目沿用 [PolyForm Noncommercial License 1.0.0](./LICENSE)。

- ✅ 个人学习 / 研究 / hobby 等非商业用途
- ❌ 禁止商业用途
