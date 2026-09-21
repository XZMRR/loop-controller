# Loop Controller 本地联调一键启动。
# 全部使用相对路径（以仓库根为基准），任何机器克隆后即可复用。
#
# 用法:
#   scripts\dev-up.ps1            启动 OPA -> Go 内核 -> Python 运行时
#   scripts\dev-up.ps1 -Frontend  额外启动 vite 治理台 (5173)
#   scripts\dev-up.ps1 -Down      停止本脚本拉起的全部进程
#
# 前置: 复制 .env.example 为 .env 并填入本地值；tools/opa.exe 已就位
# （OPA 二进制不入库，见 config/policy_delivery.yaml 头部说明）。
[CmdletBinding()]
param(
    [switch]$Frontend,
    [switch]$Down
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$pidsFile = Join-Path $Root 'data\dev-up.pids'

function Stop-DevStack {
    if (-not (Test-Path $pidsFile)) { Write-Host '没有本脚本拉起的进程记录。'; return }
    foreach ($line in Get-Content $pidsFile) {
        if ($line -match '^\d+$') {
            $p = Get-Process -Id [int]$line -ErrorAction SilentlyContinue
            if ($p) { Stop-Process -Id $p.Id -Force; Write-Host "已停止 PID $line" }
        }
    }
    Remove-Item $pidsFile -Force -ErrorAction SilentlyContinue
    Write-Host '联调环境已停止。'
}

if ($Down) { Stop-DevStack; return }

# --- 读取 .env（Process 级，不写用户环境） -----------------------------------
$envFile = Join-Path $Root '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}

# --- 预检 --------------------------------------------------------------------
$required = @(
    'LOOP_CONTROLLER_API_KEY',
    'LOOP_CONTROLLER_AUDIT_HMAC_KEY',
    'LOOP_CONTROLLER_BUNDLE_TOKEN',
    'LOOP_CONTROLLER_OPA_STATUS_TOKEN',
    'LC_A2A_CONTROL_TOKEN',
    'GO_KERNEL_TOKEN_SECRET'
)
$missing = $required | Where-Object { -not [Environment]::GetEnvironmentVariable($_, 'Process') }
if ($missing) {
    Write-Host "缺少环境变量（请在 .env 中配置）: $($missing -join ', ')" -ForegroundColor Red
    exit 1
}

$opaBin = Join-Path $Root 'tools\opa.exe'
if (-not (Test-Path $opaBin)) {
    Write-Host '未找到 tools\opa.exe。请先下载 OPA 二进制（该目录已 gitignore，不入库）。' -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $Root 'data'))) { New-Item -ItemType Directory -Path (Join-Path $Root 'data') | Out-Null }
# 单写者数据目录：清理上次异常退出残留的锁文件
Get-ChildItem (Join-Path $Root 'data') -Filter '*.lock' -ErrorAction SilentlyContinue | Remove-Item -Force

function Test-PortFree([int]$Port) {
    return -not [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}
foreach ($port in 8181, 8080, 8000) {
    if (-not (Test-PortFree $port)) {
        Write-Host "端口 $port 已被占用。请先 scripts\dev-up.ps1 -Down 或手动停止旧进程。" -ForegroundColor Red
        exit 1
    }
}
if ($Frontend -and -not (Test-PortFree 5173)) {
    Write-Host '端口 5173 已被占用。' -ForegroundColor Red
    exit 1
}

Write-Host '提示: 若更换 LOOP_CONTROLLER_AUDIT_HMAC_KEY，旧 data\ 审计链会验签失败（fail-closed），' -ForegroundColor Yellow
Write-Host '      需先归档 data 目录重建；进程重启间隔请保持 3 秒以上。' -ForegroundColor Yellow

# --- 启动 --------------------------------------------------------------------
$launched = @()

$opa = Start-Process -FilePath $opaBin -ArgumentList 'run','--server','--bundle','policies','--addr','127.0.0.1:8181' -WorkingDirectory $Root -WindowStyle Hidden -PassThru
$launched += $opa.Id
Write-Host "OPA        : 127.0.0.1:8181 (PID $($opa.Id))"

$kernelArgs = @(
    'run','./cmd/kernel',
    '-addr','127.0.0.1:8080',
    '-allow-http',
    '-secret', $env:GO_KERNEL_TOKEN_SECRET,
    '-control-token', $env:LC_A2A_CONTROL_TOKEN,
    '-control-initiator', 'loop-controller-local',
    '-control-tenant', 'tenant-a'
)
$kernel = Start-Process -FilePath 'go' -ArgumentList $kernelArgs -WorkingDirectory (Join-Path $Root 'go') -WindowStyle Hidden -PassThru
$launched += $kernel.Id
Write-Host "Go 内核    : 127.0.0.1:8080 (PID $($kernel.Id))"

Start-Sleep -Seconds 3  # 数据目录单写者：与上次进程退出保持间隔

$pythonExe = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $pythonExe)) { $pythonExe = 'python' }
$py = Start-Process -FilePath $pythonExe -ArgumentList '-m','loop_controller.cli','--config-dir','config','server','--port','8000' -WorkingDirectory $Root -WindowStyle Hidden -PassThru
$launched += $py.Id
Write-Host "Python 运行时: 127.0.0.1:8000 (PID $($py.Id))"

if ($Frontend) {
    $fe = Start-Process -FilePath 'npm' -ArgumentList '--prefix','frontend','run','dev' -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    $launched += $fe.Id
    Write-Host "治理台     : http://localhost:5173 (PID $($fe.Id))"
}

$launched | Set-Content $pidsFile
Write-Host ''
Write-Host '启动完成。治理台登录使用 .env 中的 LOOP_CONTROLLER_API_KEY。' -ForegroundColor Green
Write-Host '停止: scripts\dev-up.ps1 -Down'
