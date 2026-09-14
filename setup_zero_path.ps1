# 把 zero 命令放到用户 PATH 上（profile 之外的通用机制，远程会话同样生效）
$bin = "C:\Users\Zhoutao\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null

# shim：只转发到可编辑安装生成的入口点，不把整个 venv\Scripts 暴露到 PATH
$cmd = Join-Path $bin "zero.cmd"
Set-Content -Path $cmd -Encoding ASCII -Value @'
@echo off
"C:\Users\Zhoutao\Desktop\PythonProjects\zero\.venv\Scripts\zero.exe" %*
'@

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
