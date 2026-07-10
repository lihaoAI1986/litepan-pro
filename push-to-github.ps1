#Requires -Version 5.1
<#
.SYNOPSIS
    一键推送 LitePan-Pro 到 GitHub。
.DESCRIPTION
    需要你已经：
    1) 在 https://github.com/new 创建了空仓库（不要勾 README/.gitignore/license）
    2) 在 https://github.com/settings/tokens 创建 Personal Access Token（勾 repo 权限）
.PARAMETER RepoUrl
    形如 https://github.com/你的用户名/litepan-pro.git
.PARAMETER Token
    你的 GitHub Personal Access Token（ghp_ 开头）
.EXAMPLE
    .\push-to-github.ps1 -RepoUrl "https://github.com/myuser/litepan-pro.git" -Token "ghp_xxxxxxxxx"
#>
param(
    [Parameter(Mandatory=$true)] [string]$RepoUrl,
    [Parameter(Mandatory=$true)] [string]$Token
)

$ErrorActionPreference = "Stop"
$srcDir = $PSScriptRoot
Set-Location $srcDir

Write-Host "==[1/4]== 清理运行时残留..." -ForegroundColor Cyan
foreach ($d in @("data","log","strm","plugins")) {
    if (Test-Path $d) {
        $files = Get-ChildItem -Path $d -Recurse -Force -File -ErrorAction SilentlyContinue
        if ($files) {
            Write-Host "  ! $d 目录里有文件，请先手动清空（避免误传）" -ForegroundColor Yellow
        }
    }
}

Write-Host "==[2/4]== 初始化 git 仓库..." -ForegroundColor Cyan
if (-not (Test-Path .git)) {
    git init | Out-Null
}
git config user.email "litepan-pro@local" 2>$null
git config user.name  "LitePan-Pro Publisher" 2>$null

Write-Host "==[3/4]== 添加并提交..." -ForegroundColor Cyan
git add .
$status = git status --short
if (-not $status) {
    Write-Host "  没有需要提交的改动（可能已经推送过了）" -ForegroundColor Yellow
} else {
    git commit -m "feat: LitePan-Pro with TgtoDrive-style /d/ STRM 302 reverse proxy" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git commit 失败" }
}

Write-Host "==[4/4]== 推送到 GitHub..." -ForegroundColor Cyan
$branch = git symbolic-ref --short HEAD 2>$null
if (-not $branch) { $branch = "main"; git branch -M main }

# 用 token 拼装 URL 避免交互式登录
$authUrl = $RepoUrl -replace '^https://', "https://${Token}@"
git remote remove origin 2>$null
git remote add origin $authUrl
git push -u origin $branch --force

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "✅ 推送成功！" -ForegroundColor Green
    Write-Host ""
    Write-Host "下一步：" -ForegroundColor Yellow
    Write-Host "  1. 打开仓库 → Actions 页面，等 3-5 分钟看到 build 完成" -ForegroundColor White
    Write-Host "  2. 进入仓库 → Packages → litepan-pro → Package settings → 勾 Make public" -ForegroundColor White
    Write-Host "  3. iStoreOS 上执行 docker run -d --name litepan-pro --restart unless-stopped --network host -e TZ=Asia/Shanghai -v /opt/litepan-pro/data:/app/data -v /opt/litepan-pro/log:/app/log -v /opt/litepan-pro/strm:/app/strm -v /opt/litepan-pro/plugins:/app/plugins ghcr.io/$($RepoUrl -replace '.*github.com/([^/]+)/.*', '$1')/litepan-pro:latest" -ForegroundColor White
}
