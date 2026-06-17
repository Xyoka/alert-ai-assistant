# 企业微信告警 AI 摘要助手 - 小白部署手册

> 跟着这份手册，一步一步操作，任何人都能部署成功。
> 如有任何步骤报错，截图发给技术人员。

---

## 目录

1. [安装 Python](#1-安装-python)
2. [下载代码](#2-下载代码)
3. [一键安装](#3-一键安装)
4. [填写个人信息](#4-填写个人信息)
5. [测试运行](#5-测试运行)
6. [设置自动推送](#6-设置自动推送)
7. [日常维护](#7-日常维护)

---

## 1. 安装 Python

> Python 是运行本程序所需的软件，需要先安装。

### 1.1 下载 Python

1. 打开浏览器，访问：https://www.python.org/downloads/
2. 点击黄色的 **Download Python 3.11.x**（或更高版本）按钮
3. 等待下载完成

### 1.2 安装 Python

1. 双击下载好的安装文件（如 `python-3.11.9-amd64.exe`）
2. **重要：** 勾选底部 **"Add Python to PATH"**（添加 Python 到环境变量）
3. 点击 **"Install Now"**（立即安装）
4. 等待安装完成，点击 **"Close"**（关闭）

### 1.3 验证安装

1. 按键盘 `Win + R`，输入 `powershell`，回车
2. 在弹出的黑色窗口中输入以下命令，回车：

```powershell
python --version
```

3. 如果显示 `Python 3.11.x` 或类似信息，说明安装成功。

---

## 2. 下载代码

### 方式 A：从 U 盘拷贝（推荐，内网机器）

1. 找一台能上网的电脑，打开 https://github.com/Xyoka/alert-ai-assistant
2. 点击绿色按钮 **"Code"** → **"Download ZIP"**
3. 解压 ZIP 文件，将整个文件夹复制到 U 盘
4. 将 U 盘插到你的办公电脑上
5. 将文件夹复制到 `D:\` 或 `C:\` 根目录（路径中不要有中文）

### 方式 B：从公司共享文件夹拷贝

1. 找技术人员获取项目文件夹
2. 复制到你的电脑，建议放在 `D:\alert-ai-assistant` 

### 方式 C：通过 Git 克隆（技术人员使用）

```powershell
git clone git@github.com:Xyoka/alert-ai-assistant.git
cd alert-ai-assistant
```

---

## 3. 一键安装

### 3.1 打开 PowerShell

1. 在项目文件夹中，按住键盘 **Shift** 键
2. 点击鼠标**右键**，选择 **"在此处打开 PowerShell 窗口"**

### 3.2 执行安装脚本

在 PowerShell 窗口中，**复制粘贴** 以下命令，回车执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
```

### 3.3 等待安装完成

- 脚本会自动创建虚拟环境、安装依赖包、运行测试
- 整个过程约 **1-3 分钟**
- 如果看到 **"=== 部署完成 ==="** 的绿色字，说明安装成功。
- 如果出现红色错误，截屏发给技术人员

---

## 4. 填写个人信息

### 4.1 打开配置文件

在 PowerShell 中执行：

```powershell
notepad config.yaml
```

### 4.2 修改以下配置项

找到下面的内容，按照说明修改（**不要改动冒号和缩进**）：

#### ① 填写你的网管平台 SID（凭证码）

找到：

```yaml
sid: "你的SID"
sid_param_name: "token"
```

把 `你的SID` 替换为技术人员给你的凭证码（SID）。
当前网管接口示例使用 `token=SID`，所以 `sid_param_name` 保持 `token` 即可；如果实际接口字段名不同，再按接口文档调整。

#### ② 填写你的姓名

找到：

```yaml
owner_instance_name: "张晏瑞"
```

如果**你不是张晏瑞**，把 `张晏瑞` 改为**你自己的名字**。

继续往下翻，找到以下三处，也把名字改成你自己的：

```yaml
    unhandled:
      ...
      - attr: instance_name
        search: ["张晏瑞"]     # 改成你的名字
    processing:
      ...
      - attr: instance_name
        search: ["张晏瑞"]     # 改成你的名字
    ended:
      ...
      - attr: instance_name
        search: ["张晏瑞"]     # 改成你的名字
```

#### ③ 填写 LLM API 密钥

找到：

```yaml
llm:
  enabled: true
  base_url: "https://api.deepseek.com"
  api_key: "sk-你的密钥"
  model: "deepseek-chat"
```

把 `sk-你的密钥` 替换为技术人员给你的 API 密钥。

#### ④ 填写企业微信群机器人 Webhook

找到：

```yaml
wecom:
  enabled: true
  webhook_url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key"
  max_message_bytes: 3000
  max_retries: 2
```

把 `你的key` 替换为技术人员给你的 Webhook key。
（获取方式见文末备注）

### 4.3 保存文件

按 `Ctrl + S` 保存，关闭记事本。

---

## 5. 测试运行

### 5.0 检查配置

在 PowerShell 中执行：

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant check-config --config config.yaml
```

如果提示“配置检查通过”，再继续 dry-run 和正式推送。
如果提示缺少 SID、Webhook 或 API Key，先回到 `config.yaml` 修正。

### 5.1 先试 dry-run 模式（不推送消息到企业微信）

在 PowerShell 中执行：

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config config.yaml --dry-run
```

如果看到类似下面的输出（数字可能不同），说明配置正确：

```
Fetched 6 records, inserted 0 new records.
WeCom dry-run summary parts=1
Summary saved. ai_used=True delivered=False dry_run=True error=

**总体情况**
- 窗口时间：2026-05-15 09:00 - 09:59
- 未处理：0条
...
```

### 5.2 正式推送一次（会发送到企业微信群）

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config config.yaml
```

如果看到：

```
WeCom response: {"errcode":0,"errmsg":"ok"}
delivered=True
```

说明推送成功。去企业微信群里查看消息。

如果告警很多，摘要可能会分成多条发送，标题类似：

```text
【告警摘要 1/3】
【告警摘要 2/3】
【告警摘要 3/3】
```

这是正常现象。若只收到部分分段，请运行下面命令查看最近一次投递状态：

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant status --config config.yaml
```

---

## 6. 设置自动推送

设置好后，每天每小时会自动拉取告警并推送到企业微信。

### 6.1 创建定时任务

在 PowerShell 中逐条执行以下命令（一次一条）：

```powershell
# 步骤1：查看当前时间
Get-Date

# 步骤2：获取下一小时的数字（例如当前10点则得到11）
$nextHour = (Get-Date).AddHours(1).Hour
$nextHour
```

记下输出的数字（例如 `11`）。

```powershell
# 步骤3：创建定时任务（将 HH 替换为上面得到的数字）
schtasks /create /tn "alert-ai-assistant" /tr "D:\alert-ai-assistant\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config D:\alert-ai-assistant\config.yaml" /sc hourly /mo 1 /st HH:00 /f
```

**注意：** 如果项目放在其他路径，将 `D:\alert-ai-assistant\` 改为你的实际路径。

### 6.2 设置工作目录

这一步比较关键，否则程序找不到数据文件夹。

1. 按 `Win + R`，输入 `taskschd.msc`，回车打开**任务计划程序**
2. 在左侧点击 **"任务计划程序库"**
3. 在中间列表中找到 **alert-ai-assistant**，双击打开
4. 点击 **"操作"** 选项卡
5. 双击 **"启动程序"**，在 **"起始于(可选)"** 中填写你的项目路径，例如 `D:\alert-ai-assistant`
6. 点击 **确定** 保存

### 6.3 验证定时任务

1. 在任务计划程序中选中 **alert-ai-assistant**
2. 点击右侧的 **"运行"**
3. 等待 1-2 分钟，去企业微信群里查看是否有消息

---

## 7. 日常维护

### 手动运行一次

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant run-once --config config.yaml
```

### 查看最近一次摘要状态

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant status --config config.yaml
```

### 清理过期数据

```powershell
.\.venv\Scripts\python.exe -m alert_ai_assistant cleanup --config config.yaml
```

### 更新代码（当技术人员发布新版本后）

从技术人员处获取最新代码文件夹，覆盖你的项目文件夹即可。

---

---

## 8. 个性化定制（进阶）

以下功能可以根据你的需求自行修改，建议由技术人员操作。

### 8.1 修改摘要格式和内容

摘要由 **AI（DeepSeek）** 生成，提示词（Prompt）决定了摘要的格式和内容。

**AI 系统提示词**（告诉 AI 它的角色定位）：

文件：`alert_ai_assistant/llm.py`，搜索 `"content": "你是网络运维告警摘要助手"`

可以修改的部分举例：
- 要求更长或更短的摘要
- 要求使用更专业或更通俗的语言
- 要求增加或减少某些分析维度

**AI 用户提示词**（告诉 AI 本次数据怎么输出）：

文件：`alert_ai_assistant/summarizer.py`，搜索 `build_llm_prompt` 函数中的 `要求和规矩：`

可以修改的部分举例：
- 四段结构的标题名称（如"未处理（重点）"改为"需要关注的告警"）
- 每条告警的展示字段和顺序
- 分类规则和故障类型列表
- 带宽利用率告警的处理方式

### 8.2 修改低优先级规则

`config.yaml` 中的 `low_priority_keywords` 列表定义了哪些告警属于低优先级：

```yaml
low_priority_keywords:
  - "端口连接状态告警"      # 端口类告警
  - "使用率告警"            # 带宽利用率类
  - "入流量使用率"
  - "出流量使用率"
```

想增加或减少低优先级类别，直接增删关键词即可。

### 8.3 修改数据类型

`config.yaml` 中的 `field_mapping` 定义了 API 字段与程序字段的对应关系：

```yaml
field_mapping:
  device_ip: "ip"                # API 返回的 IP 字段名
  hostname: "instance_name"      # 主机名/负责人字段名
  alarm_time: "create_time"      # 告警时间字段名
  title: "alarm_title"           # 告警标题字段名
  content: "alarm_content"       # 告警内容字段名
  severity: "alarm_level_id"     # 告警级别字段名
  external_id: "id"              # 唯一 ID 字段名
```

如果网管平台 API 字段名有变化，修改此处即可。

### 8.4 修改企业微信消息类型

`config.yaml` 中的 `msg_type` 控制消息格式：

```yaml
wecom:
  msg_type: markdown   # markdown：带格式（加粗、列表），text：纯文本
```

### 8.5 修改数据保存天数

`config.yaml` 中的 `retention` 控制数据保存时长：

```yaml
retention:
  raw_alert_days: 5     # 原始告警保留天数
  summary_days: 15      # 摘要记录保留天数
  log_days: 15          # 运行日志保留天数
```

### 8.6 修改时区

`config.yaml` 中的 `timezone` 控制显示时区：

```yaml
timezone: Asia/Shanghai
```

---

## 备注：获取企业微信群机器人 Webhook

如果你需要推送到**你自己的群**，按以下步骤操作：

1. 打开企业微信，进入你想接收消息的群聊
2. 点击右上角的 **"..."** → **"群机器人"**
3. 点击 **"添加机器人"**
4. 给机器人起个名字（如"告警助手"），点击 **"添加"**
5. 复制 **Webhook 地址**（以 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=` 开头）
6. 打开 `config.yaml`，替换 `webhook_url` 的值
7. 保存文件，重新运行测试
