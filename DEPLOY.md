# LitePan-Pro 部署指南

本文档说明如何把 LitePan-Pro 发布成可一条 `docker run` 安装的镜像。

> 核心思路：用 GitHub Actions 自动构建 Docker 镜像并发布到 GitHub Container Registry (ghcr.io)，完全免费、自动、不需要本地装 Docker Desktop。

## 前置准备

- 一个 GitHub 账号（如果还没有，去 https://github.com 注册）
- 你的 iStoreOS 机器能访问公网

## 一、推送源码到 GitHub（5 分钟）

### 1. 在 GitHub 网页上创建仓库

1. 打开 https://github.com/new
2. 填写：
   - **Repository name**: `litepan-pro`（或你自己取的名字）
   - **Description**: `LitePan with TgtoDrive-style /d/ STRM 302 reverse proxy`
   - **Public / Private**: 选 **Public**（不然 ghcr.io 拉镜像要登录）
   - **不要勾选** "Add a README file"、"Add .gitignore"、"Choose a license"（我们已经有了）
3. 点 **Create repository**
4. **记下仓库地址**，形如：`https://github.com/你的用户名/litepan-pro.git`

### 2. 上传源码

把 LitePan-Pro 整个目录（除了 `data/` `log/` `strm/` `plugins/` 这些运行时目录）上传上去。

#### 方法 A：用 GitHub Desktop（推荐，最简单）

1. 下载安装 https://desktop.github.com
2. File → Add local repository → 选 `D:\codex\litepan-upgrade\LitePan-Pro`
3. 它会自动识别现有代码，让你提交
4. 点 "Publish repository" 推送到 GitHub

#### 方法 B：用命令行 Git

打开 PowerShell：

```powershell
cd D:\codex\litepan-upgrade\LitePan-Pro

git init
git add .
git commit -m "Initial commit: LitePan-Pro v3 path-based STRM"
git branch -M main
git remote add origin https://github.com/你的用户名/litepan-pro.git
git push -u origin main
```

第一次 push 会让你输入 GitHub 用户名 + Personal Access Token（不是密码）。

#### 方法 C：网页直接上传

1. 在新建的 GitHub 仓库页面点 "uploading an existing file"
2. 把 LitePan-Pro 整个目录拖进去（先在 Windows 上 `压缩` 成 zip，然后解压上传 zip 内的所有内容）
3. 点 "Commit changes"

### 3. 触发自动构建

代码推送上去后，GitHub Actions 会自动跑。

1. 进入仓库页面 → 顶栏 **Actions** → 左边 **Build & Publish LitePan-Pro Docker Image**
2. 看到绿色 ✅ = 构建成功
3. 大约等 3-5 分钟（首次构建要装 Node + Python 依赖）

构建成功后镜像会发布到：
```
ghcr.io/你的用户名/litepan-pro:latest
```

### 4. 让镜像可公开拉取

1. 仓库页面 → 右边栏 **Packages** → 点 `litepan-pro`
2. 进入 Package settings → 勾上 **Make public**（不然别的地方拉不到）

## 二、在 iStoreOS 上运行（3 分钟）

SSH 进 iStoreOS：

```bash
# 1. 创建工作目录
mkdir -p /opt/litepan-pro/data /opt/litepan-pro/log /opt/litepan-pro/strm /opt/litepan-pro/plugins
cd /opt/litepan-pro

# 2. 创建 docker-compose.yml
cat > docker-compose.yml << 'EOF'
services:
  litepan:
    image: ghcr.io/你的用户名/litepan-pro:latest
    container_name: litepan-pro
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./data:/app/data
      - ./log:/app/log
      - ./strm:/app/strm
      - ./plugins:/app/plugins
    environment:
      TZ: Asia/Shanghai
EOF

# 3. 启动
docker compose up -d

# 4. 看日志
docker compose logs -f
```

启动成功后，浏览器访问 `http://你的iStoreOS_IP:5211`。

## 三、或者用一条 docker run（最简）

如果你懒得用 docker-compose，直接：

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
  ghcr.io/你的用户名/litepan-pro:latest
```

## 四、升级 LitePan-Pro

修改源码后：

```powershell
cd D:\codex\litepan-upgrade\LitePan-Pro
git add .
git commit -m "update"
git push
```

GitHub Actions 自动重新构建 → 推送到 ghcr.io。

然后 iStoreOS 上拉新镜像：

```bash
docker compose pull
docker compose up -d
```

## 常见问题

### Q: ghcr.io 拉镜像超时？
国内 iStoreOS 拉 ghcr.io 可能很慢。可以：
- 方案 1：给 docker 配置镜像加速（`/etc/docker/daemon.json` 加 `registry-mirrors`）
- 方案 2：登录到 GitHub Container Registry 后拉（私有包必须登录）
- 方案 3：推到阿里云容器镜像服务（中国大陆访问快，免费）

### Q: 怎么改成 Docker Hub？
把 `.github/workflows/docker.yml` 里的 `ghcr.io` 改成 `docker.io` 并加上 Docker Hub login 即可。

### Q: 怎么改成 Gitee？
Gitee 不支持自动构建镜像。建议还是用 GitHub + ghcr.io。

### Q: 私有仓库能用吗？
私有仓库也行，但需要在 iStoreOS 上先用 `docker login ghcr.io -u 你的用户名` 登录。
