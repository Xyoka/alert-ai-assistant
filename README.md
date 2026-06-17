# 企业微信告警 AI 摘要助手

轻量旁路程序，从网管平台拉取告警 → SQLite 存储 → AI/规则摘要 → 企业微信每小时推送。

## 快速部署（给新用户）

### 前置条件

- Windows 10/11 办公电脑
- Python 3.11+（[下载](https://www.python.org/downloads/)）
- 能访问网管平台 `10.50.132.120` 的内网环境

### 一键安装

```powershell
# 1. 克隆代码（如已配置 SSH）
git clone git@github.com:Xyoka/alert-ai-assistant.git
cd alert-ai-assistant

# 2. 运行一键部署脚本
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1

# 3. 编辑配置文件，填写个人信息
notepad config.yaml
```

### 手动安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .[test]
copy config.example.yaml config.yaml
```

### 配置 config.yaml

打开 `config.yaml`，修改以下个人配置：

```yaml
source:
  kind: monitor_api                        # 使用真实网管API

monitor_api:
  enabled: true
  sid: "你的SID"                           # 从网管平台获取
  sid_param_name: "token"                  # 你提供的接口示例为 token=SID
  owner_instance_name: "你的姓名"           # 改为你的真实姓名

  bucket_search_units:
    unhandled/processing/ended:            # 三处的 instance_name 都改为你的姓名
      - attr: instance_name
        search: ["你的姓名"]
        operator: "="

llm:
  enabled: true
  base_url: "https://api.deepseek.com"
  api_key: "sk-你的密钥"                   # DeepSeek / OpenAI API密钥
  model: "deepseek-chat"

wecom:
  enabled: true
  webhook_url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"  # 群机器人Webhook
  dry_run: false
  max_message_bytes: 3000              # 中文按字节限制，过长会自动拆分
  max_retries: 2                       # 企业微信返回失败时重试
```

密钥也可用环境变量覆盖（不写入 config.yaml）：

```powershell
$env:ALERT_AI_MONITOR_SID = "你的SID"
$env:ALERT_AI_LLM_API_KEY = "sk-xxx"
$env:ALERT_AI_WECOM_WEBHOOK_URL = "https://..."
```

### 验证运行

```powershell
# dry-run 模式（不推送企业微信）
.\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config config.yaml --dry-run

# 检查配置是否有明显缺项
.\.venv\Scripts\python.exe -m alert_ai_assistant check-config --config config.yaml

# 确认后去掉 --dry-run 真实推送
.\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config config.yaml

# 查看最近一次摘要是否成功发送
.\.venv\Scripts\python.exe -m alert_ai_assistant status --config config.yaml
```

### 配置定时任务（每小时准点自动推送）

```powershell
# 将 HH 改为下一个整点（如当前 10 点则填 11）
schtasks /create /tn "alert-ai-assistant" /tr "完整路径\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config 完整路径\config.yaml" /sc hourly /mo 1 /st HH:00 /f
```

然后在任务计划程序 UI 中设置"起始于(可选)"为项目目录。

## 运行命令

| 命令 | 说明 |
|---|---|
| `run-once --config config.yaml` | 拉取告警 → 摘要 → 推送 |
| `run-once --config config.yaml --dry-run` | 同上，不推送企业微信 |
| `check-config --config config.yaml` | 检查配置缺项 |
| `status --config config.yaml` | 查看最近一次摘要投递状态 |
| `summarize-sample --input 文件.txt` | 解析本地样例文件 |
| `cleanup --config config.yaml` | 清理过期数据 |

## 稳定性设计

- 告警很多时，网管 API 会按 `page_limit` / `max_pages` 分页拉取，避免超过单页上限后漏统计。
- 未处理/处理中按本次摘要窗口拉取；已结束告警会按 `active_lookback_days` 回溯查询后，再按恢复时间过滤到本小时。
- 摘要过长时，会按 UTF-8 字节数自动拆成多条企业微信消息，并添加 `【告警摘要 1/N】` 标题。
- 企业微信返回非 0 错误码时会判定为发送失败，并按 `max_retries` 重试；任务会返回非 0，便于在任务计划程序中发现异常。
- LLM 不可用时自动降级为规则摘要；未处理和已结束告警会全量展示，处理中告警只做统计归类。
- 程序自身运行失败时，会尽量推送“摘要助手运行失败”提示，提醒运维人员回到网管平台和原始企业微信告警确认。

## 数据保存策略

| 数据 | 保存天数 |
|---|---|
| 原始告警 | 5 天 |
| 摘要记录 | 15 天 |
| 运行日志 | 15 天 |

## 环境变量

| 变量 | 覆盖配置项 |
|---|---|
| `ALERT_AI_MONITOR_SID` | `monitor_api.sid` |
| `ALERT_AI_LLM_API_KEY` | `llm.api_key` |
| `ALERT_AI_WECOM_TOKEN` | `wecom.token` |
| `ALERT_AI_WECOM_WEBHOOK_URL` | `wecom.webhook_url` |

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## 摘要模板

每小时推送格式：

```
**总体情况**
- 窗口时间：YYYY-MM-DD HH:00 - HH:59
- 未处理：x条
- 已结束：x条
- 处理中：x条

**未处理（重点）**
- 端口Down
  - IP / 主机 / 时间 / 接口 / 内容 / 负责人
- 链路故障
  - IP / 主机 / 时间 / 接口 / 内容 / 负责人

**已结束**
（全量恢复/结束告警详情或"无"）

**处理中**
（只做数量统计和类型归类，或"无"）
```
