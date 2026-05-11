# 企业微信告警 AI 摘要助手需求文档

## 1. 项目目标

开发一个轻量级“企业微信告警 AI 摘要助手”，用于网络运维场景下，从网管平台获取告警信息，定时生成告警摘要，并通过企业微信智能机器人推送给个人。

目标不是替代网管平台，也不是取消现有企业微信全量告警，而是在旁路增加一条“每小时摘要”能力，帮助运维人员快速判断当前是否有需要马上关注的告警。

GitHub 仓库：

```text
git@github.com:Xyoka/alert-ai-assistant.git
```

当前已完成初始版本：

```text
commit: 28a3982 Initial alert AI assistant
```

## 2. 核心原则

- 保留现有网管平台和企业微信全量告警链路。
- 本程序只读网管平台接口，不反向操作网管平台。
- 不自动认领、关闭、派单或确认告警。
- AI 只做辅助摘要，不做最终处理结论。
- 摘要措辞必须谨慎，例如“疑似”“建议确认”“当前仍在未处理/处理中列表”。
- 不允许输出“无需处理”“可以忽略”“已经确认无风险”等武断表达。
- 第一版保持轻量、好部署、方便在 Windows 办公电脑上运行。

## 3. 目标运行环境

第一阶段部署在 Windows 办公电脑上试运行。

推荐方式：

```text
Python 3.11+
SQLite
Windows 任务计划程序
config.yaml 本地配置
```

不使用 Docker，不注册 Windows 服务，不做前端页面。

## 4. 数据来源

网管平台支持接口查询告警列表。

示例接口：

```text
https://10.50.132.120/api/monitor/alarm/search
```

接口支持条件：

```text
offset
limit
search_unit_list
token / SID
负责人实例名称
开始时间
结束时间
```

需要分别查询三类告警：

```text
未处理告警列表
处理中告警列表
已结束告警列表
```

业务含义：

```text
未处理 + 处理中 = 当前仍需关注的告警
已结束 = 已恢复或已关闭的告警
```

## 5. 当前实现状态

当前仓库已经实现：

- Python 项目骨架。
- CLI 入口：
  - `run-once`
  - `summarize-sample`
  - `cleanup`
- 企业微信复制文本 Mock 数据解析。
- 网管平台 API 数据源骨架。
- SQLite 入库、去重、摘要保存、过期清理。
- 告警摘要统计。
- 下联接口类告警低优先级处理。
- 简单恢复折叠逻辑。
- OpenAI-compatible LLM 客户端。
- AI 失败时规则摘要降级。
- 企业微信智能机器人 HTTP 推送适配器。
- dry-run 模式。
- 锁文件，避免任务计划程序重复运行。
- README 和基础测试。

## 6. 本地运行命令

安装：

```powershell
git clone git@github.com:Xyoka/alert-ai-assistant.git
cd alert-ai-assistant
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
copy config.example.yaml config.yaml
```

样例摘要：

```powershell
python -m alert_ai_assistant summarize-sample --input "企业微信告警消息复制.txt"
```

按配置执行一次：

```powershell
python -m alert_ai_assistant run-once --config config.yaml --dry-run
```

清理过期数据：

```powershell
python -m alert_ai_assistant cleanup --config config.yaml
```

## 7. 配置要求

真实密钥不能提交到 GitHub。

本地使用 `config.yaml`，模板为 `config.example.yaml`。

关键配置：

```yaml
source:
  kind: monitor_api

monitor_api:
  enabled: true
  base_url: "https://10.50.132.120"
  sid: "本地填写"
  owner_instance_name: "负责人实例名称"
  field_mapping:
    device_ip: "实际IP字段"
    hostname: "实际主机名字段"
    alarm_time: "实际告警时间字段"
    title: "实际标题字段"
    content: "实际内容字段"
    severity: "实际级别字段"
    external_id: "实际唯一ID字段"

llm:
  enabled: true
  base_url: "OpenAI-compatible API地址"
  api_key: "本地填写"
  model: "模型名称"

wecom:
  enabled: true
  webhook_url: "企业微信智能机器人API地址"
  token: "如需要则填写"
  target_user: "如需要则填写"
  dry_run: false
```

## 8. 摘要格式

每小时推送一次，7x24 执行。

格式：

```text
【网络告警AI摘要】YYYY-MM-DD HH:00-HH:59

一、总体情况
本小时新增：N 条
当前未处理：N 条
当前处理中：N 条
本小时已结束：N 条
下联接口类告警：N 条，已计入统计，未重点展开

二、建议优先关注
1. IP：x.x.x.x
   主机：xxx
   时间：YYYY-MM-DD HH:mm:ss
   内容：xxx
   状态：当前仍在未处理/处理中列表
   建议：建议确认

三、其他说明
AI 仅作辅助摘要，最终以网管平台状态为准。
```

无告警时也推送简短摘要：

```text
本小时无新增告警。
当前无需要重点关注的未处理/处理中告警。
```

## 9. 数据保存策略

```text
原始告警保存 5 天
摘要保存 15 天
运行日志保存 15 天
```

本地数据库使用 SQLite。

## 10. 下一步开发重点

优先级从高到低：

1. 根据真实网管平台接口返回样例，完善 `field_mapping`。
2. 确认未处理、处理中、已结束三类告警的 `status` 参数值。
3. 联调 `MonitorApiSource`，确保真实接口可拉取数据。
4. 接入真实 LLM，验证摘要质量。
5. 接入企业微信智能机器人，先 dry-run，再真实推送。
6. 在 Windows 任务计划程序中配置每小时执行。
7. 根据真实告警效果优化“重点告警”和“下联接口低优先级”规则。

## 11. 验收标准

- 每小时稳定生成一条摘要。
- 网管接口返回过的告警都能计入统计。
- 未处理和处理中告警能进入当前关注范围。
- 已结束告警能计入恢复/结束统计。
- 下联接口告警计入统计但不重点展开。
- AI 摘要格式稳定、措辞谨慎。
- AI 不可用时仍能推送规则摘要。
- 不提交任何 SID、API Key、企业微信凭证、数据库和日志。

