# 企业微信告警 AI 摘要助手

轻量旁路程序，用于从网管平台接口或本地样例拉取告警，保存到 SQLite，生成每小时摘要，并通过企业微信智能机器人发送给个人。

第一版不改造网管平台，不自动认领、关闭或派单，只读告警接口并发送摘要。

## 当前能力

- 支持企业微信复制文本样例解析，用于本地开发验证。
- 预留网管平台接口适配，支持未处理、处理中、已结束三类告警列表。
- SQLite 保存原始告警和摘要记录。
- 负责人信息脱敏。
- OpenAI-compatible LLM 摘要；未配置或调用失败时自动降级为规则摘要。
- 企业微信智能机器人 HTTP 推送；未配置时 dry-run。
- 锁文件避免 Windows 任务计划程序重复运行。

## 安装

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .[test]
```

macOS 本地验证也可以使用：

```bash
python3 -m pip install -e .[test]
```

## 配置

复制配置模板：

```bash
cp config.example.yaml config.yaml
```

本地样例验证时保持：

```yaml
source:
  kind: mock_text
```

部署到能访问网管平台的工作电脑后改为：

```yaml
source:
  kind: monitor_api
monitor_api:
  enabled: true
  sid: "你的SID"
  owner_instance_name: "你的负责人实例名称"
```

密钥也可以用环境变量覆盖：

```text
ALERT_AI_MONITOR_SID
ALERT_AI_LLM_API_KEY
ALERT_AI_WECOM_TOKEN
ALERT_AI_WECOM_WEBHOOK_URL
```

## 运行

解析样例文件并生成摘要：

```bash
python3 -m alert_ai_assistant summarize-sample --input "/Users/zhangyanrui/Downloads/企业微信告警消息复制.txt"
```

按配置拉取、入库、摘要、推送一次：

```bash
python3 -m alert_ai_assistant run-once --config config.yaml
```

清理过期数据：

```bash
python3 -m alert_ai_assistant cleanup --config config.yaml
```

## Windows 任务计划程序

摘要任务已创建并启用，每小时执行一次（3 分钟延迟以防网管平台告警拥塞）：

```powershell
TaskName: \alert-ai-assistant
Schedule: 每小时一次，起始 17:03，每整点后的 3 分钟触发
Command: g:\vibe\alert-ai-assistant\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config g:\vibe\alert-ai-assistant\config.yaml
Start In: g:\vibe\alert-ai-assistant
```

程序会使用 `data/run.lock` 防止上一次未结束时重复运行。

如果需要手动重建任务：

```powershell
schtasks /create /tn "alert-ai-assistant" /tr "g:\vibe\alert-ai-assistant\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config g:\vibe\alert-ai-assistant\config.yaml" /sc hourly /mo 1 /st 17:03 /f
```

然后设置"起始于"(WorkingDirectory) 为 `g:\vibe\alert-ai-assistant`（通过任务计划程序 UI 或 XML 导入）。

清理过期数据的任务建议手动执行（不需要定时）：

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant cleanup --config config.yaml
```

## 测试

```bash
pytest
```

