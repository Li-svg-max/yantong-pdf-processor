# 推送到 CLIProxyAPI 服务仓库

下面的命令只会把当前目录作为 `cli-proxy-cloud` 分支推送到现有的 `yantong-pdf-processor` 仓库，不会把小程序主仓库内容带进去。

在 PowerShell 中执行：

```powershell
$service = "C:\Users\16950\Desktop\研通\services\cli-proxy-cloud"
$git = "D:\Git\cmd\git.exe"
Set-Location $service

if (!(Test-Path -LiteralPath (Join-Path $service ".git"))) {
  & $git init
}

& $git switch -C cli-proxy-cloud
& $git add Dockerfile docker-entrypoint.sh .dockerignore .gitignore .env.example README.md test-deployment-files.js PUSH_TO_GITHUB.md Copy-CodexOAuthSecret.ps1
& $git diff --cached --check
& $git commit -m "Add CloudBase CLIProxyAPI service"

$remote = (& $git remote get-url origin 2>$null)
if (!$remote) {
  & $git remote add origin "ssh://git@ssh.github.com:443/Li-svg-max/yantong-pdf-processor.git"
}

$env:GIT_SSH_COMMAND = "ssh -p 443"
& $git push -u origin cli-proxy-cloud
```

推送前确认暂存区只包含本目录列出的部署文件。不要把 `.env`、`auth`、`config.yaml`、OAuth JSON、API Key 或其他凭证放进这个目录。

CloudBase 从该仓库部署时选择：

- 分支：`cli-proxy-cloud`
- Dockerfile：仓库根目录的 `Dockerfile`
- 容器端口：`8080`
- 服务名：`cli-proxy-api`
