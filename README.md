# AIssistant · 小柚

> Windows 桌面 AI 助手兼桌宠：PyQt6 + Live2D + 大模型聊天 + 结构化工具调用 + MCP 扩展 + 语音交互。

## 功能

- 大模型流式对话（OpenAI 兼容接口）
- 结构化工具调用（function calling）：联网搜索、天气/路线、系统状态、磁盘只读扫描、文件读写、锁屏/关机等
- MCP（Model Context Protocol）扩展：支持 stdio 本地 server 与远程 HTTP/SSE server，工具自动并入工具表
- 语音：sherpa-onnx 唤醒「小柚」、讯飞语音听写、edge-tts 语音回复
- Live2D 桌宠：无边框置顶、拖拽、系统托盘、模型切换
- 电脑管家：健康体检、磁盘只读扫描
- 可打包为免安装 exe

## 环境要求

- Windows 10/11
- Python 3.12
- 依赖见 `requirements.txt`

## 快速开始

```bat
:: 1) 创建环境
conda create -n ai_assistant python=3.12
conda activate ai_assistant
pip install -r requirements.txt

:: 2) 复制配置模板并按需填写
copy config\templates\auth.example.json   config\auth.json
copy config\templates\config.example.json config\config.json
copy config\templates\apps.example.json   config\apps.json
copy config\templates\mcp.example.json    config\mcp.json

:: 3) 运行
scripts\run_debug.bat      :: 前台调试（能看到报错）
scripts\run.bat            :: 隐藏启动
```

## 配置说明

| 文件 | 内容 | 是否入库 |
| --- | --- | --- |
| `config/auth.json` | 密钥（模型 api_key、高德、Tavily、讯飞） | 否（已忽略） |
| `config/config.json` | Base URL、聊天模型、视觉模型、推理强度、SearXNG URL | 否（已忽略） |
| `config/apps.json` | 常用应用快捷方式 | 否（已忽略） |
| `config/mcp.json` | MCP server 配置 | 否（已忽略） |

优先级：设置窗口中已保存的值 → 对应 JSON 文件 → 程序默认值。

## Live2D 资源

本仓库**不包含 Live2D Cubism Core / Framework**（属 Live2D 专有许可，不可随仓库分发）。
要启用 Live2D 桌宠，请自行从 [Live2D 官网](https://www.live2d.com/) 获取 **Cubism SDK for Web**，
把 `Core/` 与 `Framework/` 放到 `assets/web_resources/dist/` 下（前端页面会从该目录加载引擎）。
仓库内仅保留免费模型「大肥鱼」，其版权归原作者[氵六青的个人空间](https://space.bilibili.com/11272072)，请遵守其许可。

## MCP 工具

完整说明见 `docs/MCP_INTEGRATION.md`。查看已接入的工具：

- 菜单「设置 → MCP 工具管理」（只读窗口）
- 或命令行 `python scripts\list_mcp_tools.py`

## 打包免安装版

在 Windows 运行 `scripts\build_portable.bat`，生成 `Aissist_v1.101.5_test_portable.zip`。

## 项目结构

见 `docs/PROJECT_OVERVIEW.md`。

## 许可

- 本项目代码：[MIT License](LICENSE)
- Live2D Cubism Core / Framework 及示例模型版权归 **Live2D Inc.**，不随本仓库分发。
- 「大肥鱼」模型为免费资源，版权归原作者所有。

## 致谢

- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/)
- [Live2D Cubism SDK](https://www.live2d.com/)
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
- [Model Context Protocol](https://modelcontextprotocol.io/)