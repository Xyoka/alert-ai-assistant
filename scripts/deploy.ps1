# alert-ai-assistant 一键部署脚本
# 请在 PowerShell 中运行：  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "=== alert-ai-assistant 部署脚本 ===" -ForegroundColor Cyan
Write-Host "目标目录: $ProjectRoot"
Write-Host ""

# 1. 检查 Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[ERROR] 未找到 Python，请先安装 Python 3.11+" -ForegroundColor Red
    Write-Host "下载地址: https://www.python.org/downloads/"
    exit 1
}
$pyVer = & python --version
Write-Host "[1/5] Python 版本: $pyVer" -ForegroundColor Green

# 2. 创建虚拟环境
if (Test-Path "$ProjectRoot\.venv") {
    Write-Host "[2/5] 虚拟环境已存在，跳过创建" -ForegroundColor Yellow
} else {
    Write-Host "[2/5] 创建虚拟环境..." -ForegroundColor Green
    & python -m venv "$ProjectRoot\.venv"
}

# 3. 安装依赖
Write-Host "[3/5] 安装依赖..." -ForegroundColor Green
& "$ProjectRoot\.venv\Scripts\pip.exe" install -U pip
& "$ProjectRoot\.venv\Scripts\pip.exe" install -e "$ProjectRoot\[test]"

# 4. 初始化配置
$configPath = "$ProjectRoot\config.yaml"
if (Test-Path $configPath) {
    Write-Host "[4/5] config.yaml 已存在，跳过创建" -ForegroundColor Yellow
} else {
    Write-Host "[4/5] 创建 config.yaml，请填写个人信息..." -ForegroundColor Green
    Copy-Item "$ProjectRoot\config.example.yaml" $configPath
    Write-Host ""
    Write-Host "请编辑 config.yaml，修改以下配置项:" -ForegroundColor Cyan
    Write-Host "  1. monitor_api.sid          - 你的网管平台 SID" -ForegroundColor White
    Write-Host "  2. owner_instance_name       - 你的姓名" -ForegroundColor White
    Write-Host "  3. bucket_search_units 中的  - 所有 instance_name 改为你的姓名" -ForegroundColor White
    Write-Host "  4. llm.api_key              - LLM API 密钥" -ForegroundColor White
    Write-Host "  5. wecom.webhook_url        - 企业微信群机器人 Webhook" -ForegroundColor White
    Write-Host "  6. mask_names               - 需要脱敏的同事姓名" -ForegroundColor White
}

# 5. 运行测试
Write-Host "[5/5] 运行测试验证安装..." -ForegroundColor Green
& "$ProjectRoot\.venv\Scripts\python.exe" -m pytest "$ProjectRoot\tests"

Write-Host ""
Write-Host "=== 部署完成 ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "快速验证（dry-run 模式）:" -ForegroundColor Yellow
Write-Host "  .\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config config.yaml --dry-run" -ForegroundColor White
Write-Host ""
Write-Host "配置定时任务（每小时准点推送）:" -ForegroundColor Yellow
Write-Host "  schtasks /create /tn ""alert-ai-assistant"" /tr ""$ProjectRoot\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config $ProjectRoot\config.yaml"" /sc hourly /mo 1 /st HH:00 /f" -ForegroundColor White
Write-Host "  （将 HH 替换为当前小时+1，例如当前10点则填11）" -ForegroundColor White
