param([int]$Port = 8080, [string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
New-Item -ItemType Directory -Force -Path 'runtime' | Out-Null
$secretPath = Join-Path $PSScriptRoot 'runtime\session.key'
if (-not $env:QINGYOU_SECRET) {
    if (-not (Test-Path -LiteralPath $secretPath)) {
        $key = & $Python -c 'import secrets; print(secrets.token_hex(32))'
        if ($LASTEXITCODE -ne 0) { throw '无法生成会话密钥' }
        Set-Content -LiteralPath $secretPath -Value $key -Encoding ascii
    }
    $env:QINGYOU_SECRET = (Get-Content -Raw -LiteralPath $secretPath).Trim()
}
& $Python -X utf8 app.py serve --port $Port
exit $LASTEXITCODE
