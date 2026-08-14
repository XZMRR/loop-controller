#Requires -Version 5.1
<#
.SYNOPSIS
    下载 OPA（Open Policy Agent）Windows 二进制到 .opa/opa.exe。

.DESCRIPTION
    从 GitHub Release 下载与当前系统匹配的 OPA 可执行文件。
    如果 .opa/opa.exe 已存在且版本正确，则跳过下载。

.PARAMETER Version
    要下载的 OPA 版本，默认 "1.19.0"。

.EXAMPLE
    .\scripts\download_opa.ps1
    .\scripts\download_opa.ps1 -Version "1.18.0"
#>

[CmdletBinding()]
param (
    [string]$Version = "1.19.0"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$opaDir = Join-Path $projectRoot ".opa"
$opaExe = Join-Path $opaDir "opa.exe"

if (Test-Path $opaExe) {
    $currentVersion = & $opaExe version | Select-String -Pattern "Version:\s*(.+)" | ForEach-Object { $_.Matches.Groups[1].Value }
    if ($currentVersion -eq $Version) {
        Write-Host "OPA $Version 已存在：$opaExe" -ForegroundColor Green
        exit 0
    }
    Write-Host "发现旧版本 OPA $currentVersion，将重新下载 $Version" -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path $opaDir | Out-Null

$arch = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else { "386" }
$url = "https://github.com/open-policy-agent/opa/releases/download/v$Version/opa_windows_$arch.exe"

Write-Host "Downloading OPA $Version from $url ..." -ForegroundColor Green

try {
    Invoke-WebRequest -Uri $url -OutFile $opaExe -UseBasicParsing
} catch {
    Write-Host "下载失败：$_" -ForegroundColor Red
    Write-Host "可手动下载 $url 并重命名为 $opaExe" -ForegroundColor Red
    exit 1
}

Write-Host "OPA 已下载到：$opaExe" -ForegroundColor Green
& $opaExe version
