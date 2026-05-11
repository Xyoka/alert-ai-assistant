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

建议创建两个任务：

- 每小时运行一次摘要任务：

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config C:\path\to\config.yaml
```

- 每天运行一次清理任务：

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant cleanup --config C:\path\to\config.yaml
```

任务计划程序的“起始于”目录设置为项目目录。程序会使用 `data/run.lock` 防止上一次未结束时重复运行。

## 网管接口联调点

真实接口返回格式尚未确认，联调时优先确认这些字段：

- IP 字段
- 主机名字段
- 告警时间字段
- 告警标题字段
- 告警内容字段
- 告警级别字段
- 唯一 ID 字段
- 未处理、处理中、已结束的 `status` 取值

确认后只需要调整 `config.yaml` 的 `field_mapping` 和 `bucket_search_units`。

## 测试

```bash
pytest
```

