# Tool Calling 改造说明

## 这次改了什么

这次把原来靠标签字符串驱动的伪 function calling，改成了真正的结构化工具调用。

核心变化：

1. 模型不再拼字符串命令。
2. 客户端通过 `tools` 向模型声明可用工具和参数 schema。
3. 模型先决定是否调用工具，再由程序真实执行。
4. 工具结果回传给模型后，模型再生成最终自然语言答复。
5. 联网搜索改成 `SearXNG 搜索 + Tavily 抓正文` 的两段式流程。
6. 搜索类回复现在会附带来源标题和链接，方便追溯。

## 主要文件

- `src/main.py`
  - `APICallThread` 改成了 tool loop。
  - 新增高风险工具确认流程。
  - 新增 `SearXNG URL` 和 `Tavily Key` 配置项。
  - 系统提示词改成“工具调用”风格，不再要求输出旧标签协议。

- `src/action_control.py`
  - 新增 `ActionHandler.get_tool_definitions()`，按 JSON Schema 暴露工具。
  - 新增 `ActionHandler.execute_tool()`，按 `tool_name + arguments` 执行。
  - 已删除旧的字符串指令兼容层，只保留结构化工具分发。
  - 搜索链路改成 `_search_web()` -> `_search_searxng_results()` -> `_tavily_extract()`。

## 当前工具列表

- `open_app`
- `open_url`
- `search_web`
- `get_current_datetime`
- `get_weather`
- `get_forecast`
- `get_route`
- `system_status`
- `network_info`
- `speedtest`
- `ping_host`
- `recommend_food_by_city`
- `recommend_food_by_address`
- `recommend_food_by_coordinates`
- `lock_screen`
- `shutdown`
- `reboot`

其中 `open_app` 的可选值会从 `config/apps.json` 动态生成，不再只靠 prompt 文本约束。

## 联网搜索现在怎么走

### 1. 召回

先打本地 SearXNG：

- 配置项：`SearXNG URL`
- 默认值：`http://localhost:18080`

### 2. 抽正文

如果配置了 Tavily：

- 配置项：`Tavily Key`
- 也支持环境变量：`TAVILY_API_KEY`

程序会把前几条搜索结果 URL 发给 Tavily `/extract`，拿正文片段增强搜索结果。

### 3. 回退逻辑

如果 Tavily 没配、失败或没返回正文：

- 不会报死
- 自动回退到 SearXNG 自带摘要

## 对话流程现在怎么走

### 普通问答

1. 用户发消息
2. 程序带着 `tools` 调一次 `chat/completions`
3. 如果模型不需要工具，直接返回答复

### 需要工具时

1. 模型返回 `tool_calls`
2. 程序执行对应工具
3. 工具结果以 `role=tool` 回传给模型
4. 模型根据真实工具结果组织最终回复

### 当前日期/时间问题

对于下面这类问题，程序现在会强制注入本机时间工具结果，再让模型回答：

- 今天几号
- 今天星期几
- 现在几点
- 当前日期
- 当前时间

这样这类问题不再依赖模型记忆或联网搜索。

### 高风险操作

以下工具会在执行前弹确认框：

- `shutdown`
- `reboot`

## 为什么比以前稳

旧方案的问题：

- 聊天文本和命令字符串混在一次生成里，互相干扰
- 参数靠字符串格式约定，模型很容易拼错
- 复杂动作被塞进一个命令里，歧义太大
- 搜索结果直接展示，没有回到模型做整理

新方案的改进：

- 参数受 schema 限制
- 应用名用动态 `enum`
- 美食推荐拆成 3 个工具，不再让模型拼多种语法
- 搜索结果先抓正文，再给模型整理
- 工具执行结果是真实回传，不再靠模型猜
- 日期/时间问题强制走本机时间工具
- 最终回复会做日期一致性校验，发现说错会自动重答一次
- 最终回复会清洗 Markdown 符号，减少 `**`、列表符号等格式污染

## 配置方法

在程序里打开：

- `设置 -> API配置`

建议至少填这些：

- `API Key`
- `API Base URL`
- `模型名称`
- `SearXNG URL`
- `Tavily Key`

如果不想在界面里填 Tavily，也可以直接设置环境变量：

```bash
export TAVILY_API_KEY="tvly-xxxx"
```

Windows:

```bat
set TAVILY_API_KEY=tvly-xxxx
```

## 兼容说明

- 旧的标签字符串执行链路已经删除。
- 现在运行时只保留结构化 `tools/function calling` 主流程。
- 如果你的模型或网关不支持 `tools/function calling`，现在会直接提示，而不是继续走一套不稳定的伪协议。

## 已顺手修掉的问题

- 历史消息加载时，原来会重复写入 `conversation_history`，导致上下文重复；这次顺手修了。

## 你体验时建议重点看

- 打开本地应用是否明显比以前稳定
- 联网搜索是否会先查再答，而不是直接胡说
- 参数复杂的功能，比如路线规划和美食推荐，是否比以前少出错
- 关机/重启是否还能正常弹确认框
- 你当前使用的模型或中转是否支持 `tools`

## 目前还没做的

- 没加自动化测试
- 没做工具调用日志面板
- 还没有把搜索结果 source citation 单独渲染到 UI
- 仍然保留了系统提示词编辑入口，但角色切换时会重新生成运行时 prompt

如果你后面体验下来，最值得继续做的下一步通常是：

1. 给工具调用加可视化日志
2. 给搜索结果加来源展示
3. 给不同工具补更细的失败重试和参数澄清策略
