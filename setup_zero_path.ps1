# 把 zero 命令放到用户 PATH 上（profile 之外的通用机制，远程会话同样生效）
$bin = Join-Path $env:USERPROFILE "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null

# shim：只转发到可编辑安装生成的入口点，不把整个 venv\Scripts 暴露到 PATH
# 项目路径取脚本所在目录，避免硬编码过期
$zeroExe = Join-Path $PSScriptRoot ".venv\Scripts\zero.exe"
$cmd = Join-Path $bin "zero.cmd"
Set-Content -Path $cmd -Encoding ASCII -Value @"
@echo off
"$zeroExe" %*
"@

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (($userPath -split ';') -contains $bin) {
    Write-Output "$bin already in user PATH"
} else {
    $trimmed = $userPath.TrimEnd(';')
    $newPath = if ($trimmed) { "$trimmed;$bin" } else { $bin }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Output "Added $bin to user PATH"
}
Write-Output "shim: $cmd"
