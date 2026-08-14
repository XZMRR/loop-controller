#Requires -Version 5.1
<#
.SYNOPSIS
    在本地启动 OPA（Open Policy Agent）服务，加载 policies/ 目录下的 Rego 策略。

.DESCRIPTION
    本脚本默认监听 127.0.0.1:8181，以 bundle 模式加载 policies/。
    如果未找到 opa 可执行文件，会提示下载地址。

.PARAMETER Port
    OPA HTTP 服务端口，默认 8181。

.PARAMETER PoliciesDir
    策略目录，默认项目根目录下的 policies/。

.PARAMETER LogLevel
    日志级别，默认 error。

.EXAMPLE
    .\scripts\run_opa.ps1
    .\scripts\run_opa.ps1 -Port 8182
#>

[CmdletBinding()]
param (
    [int]$Port = 8181,
    [string]$PoliciesDir = "",
    [string]$LogLevel = "error"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrEmpty($PoliciesDir)) {
    $PoliciesDir = Join-Path $projectRoot "policies"
}

$opaExe = Join-Path (Split-Path -Parent $PSScriptRoot) ".opa" "opa.exe"
$opa = Get-Command opa -ErrorAction SilentlyContinue

if (Test-Path $opaExe) {
    $opa = $opaExe
} elseif (-not $opa) {
    Write-Host "未找到 opa 可执行文件。" -ForegroundColor Yellow
    Write-Host "请执行以下命令下载：.\scripts\download_opa.ps1" -ForegroundColor Yellow
    Write-Host "或前往 https://www.openpolicyagent.org/docs/latest/#running-opa 下载，" -ForegroundColor Yellow
    Write-Host "或执行：choco install opa" -ForegroundColor Yellow
    exit 1
}

Write-Host "Starting OPA on http://127.0.0.1:$Port with policies from $PoliciesDir" -ForegroundColor Green

& $opa run --server `
    --addr "127.0.0.1:$Port" `
    --log-level $LogLevel `
    --bundle "$PoliciesDir"
