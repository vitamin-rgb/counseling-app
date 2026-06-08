# Google Docs MCP の初回認証用スクリプト
# 使い方:
#   1. Node.js LTS をインストールし、PowerShell を開き直す
#   2. 環境変数を設定してから実行:
#        $env:GOOGLE_CLIENT_ID = "あなたのクライアントID"
#        $env:GOOGLE_CLIENT_SECRET = "あなたのシークレット"
#        .\scripts\google-mcp-auth.ps1

if (-not $env:GOOGLE_CLIENT_ID -or -not $env:GOOGLE_CLIENT_SECRET) {
    Write-Host "GOOGLE_CLIENT_ID と GOOGLE_CLIENT_SECRET を設定してから再実行してください。" -ForegroundColor Yellow
    exit 1
}

$npx = Get-Command npx -ErrorAction SilentlyContinue
if (-not $npx) {
    Write-Host "npx が見つかりません。https://nodejs.org/ から Node.js LTS をインストールし、ターミナルを開き直してください。" -ForegroundColor Red
    exit 1
}

Write-Host "ブラウザで Google 認証が開きます..." -ForegroundColor Cyan
& npx -y @fryorcraken/google-docs-mcp auth
