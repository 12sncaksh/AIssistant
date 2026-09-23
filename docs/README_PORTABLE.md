# Aissist Windows 免安装版

## 使用方法

1. 解压整个文件夹。
2. 双击 `Aissist.exe`。
3. 在桌宠上点击鼠标右键，选择打开聊天窗口。
4. 打开“设置 → 配置”，填写自己的 API Key、API Base URL、聊天模型、视觉模型和推理强度。

程序不需要安装 Python、Conda 或虚拟环境。首次启动可能需要几秒钟加载 Live2D 和语音组件。

## API 配置

API Key 不随压缩包提供，需要由使用者自行申请和填写。API 请求可能产生费用，具体以所使用的服务商为准。

实验室内网 API 只能在对应网络环境中访问。其他用户应使用自己可以访问的 OpenAI 兼容 API，并填写服务商提供的实际模型 ID。

也可以在程序目录中创建：

- `config/auth.json`：保存 API 密钥；
- `config/config.json`：保存 Base URL、聊天模型、视觉模型、推理强度和 SearXNG URL。
- `config/apps.json`：保存常用应用快捷方式；列表外应用可在确认后通过命令或视觉键鼠打开。

配置文件模板位于 `config/templates/`（`auth.example.json`、`config.example.json`、`apps.example.json`、`mcp.example.json`）。程序优先使用设置窗口中保存的值，再回退到对应 JSON 文件。

## 隐私说明

聊天内容会发送到配置的 API 服务。对话记录保存在本机，API Key 不应发送给他人，也不要将 `config/auth.json` 分享或上传。

## 常见操作

- 右键桌宠：打开聊天窗口、隐藏桌宠或退出程序。
- “视图 → 桌宠模型大小”：调整桌宠大小。
- “设置 → 语音”：配置讯飞语音识别和语音回复。
- “帮助 → 配置文件说明”：查看配置文件优先级。
- 输入区加号菜单支持图片和文本文件附件；图片需要配置支持图片输入的视觉模型。
- 输入区可以快捷切换默认/自由角色扮演模式和推理强度。

## 文件结构

`Aissist.exe` 和 `_internal` 是程序运行环境，不要删除 `_internal`。`assets/web_resources` 是 Live2D 引擎和模型资源，`generated/kws` 是唤醒词模型，二者可以独立更新。

## 资源许可

发布前请确认 `assets/web_resources` 中 Live2D 模型、Cubism SDK 和音频资源的再分发许可。不同模型的授权条件可能不同。
