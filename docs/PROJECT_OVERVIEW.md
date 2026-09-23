# Aissist 项目框架速览

> 用途：给后续切换的 agent 快速了解本项目结构。行号为近似值，改动后可能漂移，请以代码为准。
> 项目目录：`Aissist_v1.101.4_test`（源码运行）或解压后的免安装目录（打包运行）。

## 0. 目录结构

```
Aissist_v1.101.4_test/
├── src/                       # Python 源码
│   ├── main.py                # 主程序（UI、线程、语音、API 调用）
│   ├── action_control.py      # ActionHandler：内置工具 schema + 执行 + 安全黑名单
│   ├── mcp_client.py          # 外部 MCP 工具客户端（MCPManager）
│   ├── vision_input.py        # 视觉键鼠控制器
│   ├── input_guard.py         # 键鼠接管钩子
│   ├── accessibility_input.py # UI Automation 读取
│   └── test.py                # PyQt6 冒烟测试
├── config/                    # 配置（运行时读写）
│   ├── config.json            # 非敏感 API 配置（不入库）
│   ├── auth.json              # 密钥（不入库）
│   ├── apps.json              # 常用应用快捷方式
│   ├── mcp.json               # 外部 MCP server 配置（不入库）
│   └── templates/             # 配置模板（随包发布）
│       ├── config.example.json
│       ├── auth.example.json
│       ├── apps.example.json
│       └── mcp.example.json
├── scripts/                   # 启动与打包脚本
│   ├── run.bat                # 隐藏启动
│   ├── run_debug.bat          # 前台调试启动
│   ├── run_hidden.vbs         # wscript 隐藏窗口
│   ├── SearXNG.bat            # 启动本地 SearXNG docker
│   ├── build_portable.bat     # PyInstaller 免环境打包
│   └── Aissist.spec           # PyInstaller 配置
├── assets/                    # 静态资源
│   ├── Aissist.ico
│   └── web_resources/         # Live2D 前端 + Cubism + 模型
├── mcp_servers/               # MCP server 示例
│   └── demo_server.py
├── generated/                 # 运行时生成（不入库）：chat_backgrounds/、kws/
├── data/                      # 运行时数据：chat_history.db
├── logs/                      # 运行日志：app.log、mcp_<server>.log
├── agent_workspace/           # 子 agent 隔离工作区（运行时生成）
├── docs/                      # 文档
│   ├── PROJECT_OVERVIEW.md    # 本文档
│   ├── MCP_INTEGRATION.md
│   ├── README_PORTABLE.md
│   └── TOOL_CALLING_UPGRADE.md
├── archive/                   # 历史调试产物（可删）
├── requirements.txt
└── .gitignore
```

**路径解析规则**：`main.get_runtime_dir()` 返回根目录——打包后是 exe 所在目录，源码运行时是 `src/` 的上一级（项目根）。配置文件在 `<root>/config/`，资源在 `<root>/assets/`，日志在 `<root>/logs/`，数据在 `<root>/data/`。

## 1. 项目是什么

桌面 AI 助手（桌宠，名叫「小柚」）：PyQt6 桌面应用 + Live2D 桌宠 + 大模型 API 聊天 + 结构化工具调用（function calling）+ 语音唤醒/语音输入/语音回复 + 系统托盘 + 电脑管家功能（健康体检、磁盘只读扫描）。

## 2. 怎么跑起来

- `scripts/run.bat`：隐藏启动（wscript 调 `run_hidden.vbs`），内部 `conda activate ai_assistant` 后 `python src\main.py`。
- `scripts/run_debug.bat`：前台运行 + `pause`，调试用。
- 依赖：`requirements.txt`（PyQt6 / PyQt6-WebEngine / requests / edge-tts / pygame / psutil / sounddevice / sherpa-onnx / mcp 等）。
- 密钥：`config/auth.json`（不入库，`.gitignore` 已忽略）；模板见 `config/templates/auth.example.json`。
- 非敏感 API 配置：`config/config.json`（不入库）；模板见 `config/templates/config.example.json`。
- 日志：`logs/app.log`（2MB × 3 滚动），由 `setup_logging()` 初始化，含全局 `sys.excepthook`。
- 本地搜索服务：`scripts/SearXNG.bat`（docker 启动 searxng 容器，默认 `http://localhost:18080`）。
- 免环境打包：在 Windows 上运行 `scripts/build_portable.bat`，生成 `Aissist_v1.101.4_test_portable.zip`。

## 3. 文件资产清单

| 文件/目录 | 职责 |
| --- | --- |
| `src/main.py` | 主程序：全部 UI、线程、语音、API 调用逻辑 |
| `src/action_control.py` | `ActionHandler`：内置工具的 schema 声明 + 真实执行 + 安全黑名单 |
| `src/mcp_client.py` | `MCPManager`：外部 MCP server 生命周期、工具表合并、调用分发 |
| `config/auth.json` | 密钥（模型 api_key、高德、Tavily、讯飞三件套） |
| `config/config.json` | 非敏感配置（API Base URL、聊天模型、视觉模型、推理强度、SearXNG URL） |
| `config/templates/*.example.json` | 配置字段模板（随包发布） |
| `config/apps.json` | 常用应用快捷访问清单（name + path/url）；列表外应用可经确认后用命令或视觉键鼠打开 |
| `config/mcp.json` | 外部 MCP server 配置；见 `docs/MCP_INTEGRATION.md` |
| `data/chat_history.db` | SQLite 会话记录（messages 表） |
| `requirements.txt` | Python 依赖（含视觉截图、虚拟键鼠和 UI Automation） |
| `scripts/run.bat` / `run_debug.bat` / `run_hidden.vbs` | 启动脚本 |
| `scripts/Aissist.spec` / `build_portable.bat` | PyInstaller 免环境打包配置与构建脚本 |
| `scripts/SearXNG.bat` | 启动本地 SearXNG docker 容器 |
| `docs/README_PORTABLE.md` | 面向最终用户的免安装包说明 |
| `docs/TOOL_CALLING_UPGRADE.md` | 工具调用改造说明（旧文档，仍有效） |
| `docs/MCP_INTEGRATION.md` | MCP 集成说明 |
| `docs/PROJECT_OVERVIEW.md` | 本文档 |
| `src/test.py` | PyQt6 冒烟测试 |
| `logs/` | 运行日志 |
| `generated/` | 运行时生成：`chat_backgrounds/`（AI 生成的聊天背景图）、`kws/`（唤醒词 sherpa-onnx 模型 + tokens/keywords） |
| `assets/Aissist.ico` | 应用图标 |
| `assets/web_resources/` | Live2D 相关：`dist/`（Pixi 前端 + Live2D Cubism Core/Framework + 模型 Resources）、`js/`、`live2d_display.html` |
| `mcp_servers/demo_server.py` | 最小 MCP server 示例（验证链路用） |
| `archive/` | 历史调试产物，可删 |

## 4. 核心对象（src/main.py）

| 类/函数 | 职责 |
| --- | --- |
| 模块级文本工具 | `sanitize_reply_text`、kaomoji 标签提取、正文/来源拆分、任务状态工具 |
| `get_runtime_dir()` / `get_config_dir()` / `get_resource_path()` | 根目录、配置目录、资源路径解析（见第 0 节规则） |
| `setup_logging()` / `_log_safe_args()` | 运行日志初始化；工具参数脱敏（command/content 截断 200 字符） |
| `ConfigManager` | 配置读写：QSettings 用户覆盖、`config/config.json` 非敏感配置回退、`config/auth.json` 密钥回退 |
| `TextToSpeechThread(QThread)` | edge-tts 分段合成 mp3 → pygame.mixer 按页播放；页事件驱动桌宠气泡；`stop()` 可打断 |
| `XfyunSTTThread(QThread)` | 讯飞语音听写（流式版）：WebSocket 鉴权 URL 生成、分帧上传、结果解析 |
| `WakeWordThread(QThread)` | sherpa-onnx 关键词唤醒，监听“小柚”；模型在 `generated/kws/` |
| `MessageDatabase` | SQLite 会话存储，默认库文件 `data/chat_history.db` |
| `APICallThread(QThread)` | 核心 API 线程：SSE 流式请求、工具调用循环、确认弹窗请求 |
| `ConfigDialog(QDialog)` | 设置分区对话框：API 配置、系统提示词、讯飞识别、TTS 开关与音色 |
| `ChatMessageWidget(QWidget)` | 聊天气泡：自适应宽度、半透明背景、来源链接渲染、右键菜单 |
| `TaskProgressWidget(QWidget)` | 任务进度面板：分析需求 → 执行工具 → 失败原因 |
| `LocalHTTPServer` | 本地静态文件服务器（http.server），供 Live2D 页面加载 |
| `CropImageLabel` / `ImageCropDialog` | 聊天背景图裁剪工具 |
| `PetWebView(QWebEngineView)` | 加载 Live2D 页面（`live2d_display.html` / `dist/`） |
| `PetInteractionOverlay(QWidget)` | 桌宠鼠标交互层：拖拽、点击、事件透传 |
| `DesktopPetWindow(QWidget)` | 桌宠窗口：无边框置顶、拖拽、右键菜单、系统托盘 |
| `MainWindow(QMainWindow)` | 主聊天窗口：消息列表、输入框、菜单、会话管理、语音按钮、附件上传、背景设置、角色切换 |
| `main()` | 入口：初始化日志 → QApplication → MainWindow + DesktopPetWindow → 主循环 |

## 5. 工具执行模块（src/action_control.py）

`ActionHandler` 是纯静态工具层，不依赖 UI：

- `get_tool_definitions()`：返回内置工具的 JSON Schema，供 API `tools` 参数；末尾合并 MCP 工具。
- `execute_tool(tool_name, arguments)`：分发执行。
- `configure(...)`：注入 SearXNG URL / Tavily Key / 高德 Key。
- `load_apps()`：读 `config/apps.json`；`load_auth_config()`：读 `config/auth.json`。

内置工具覆盖：日期时间、打开网址、联网搜索、天气/预报、路线、系统状态、健康体检、磁盘扫描、网络信息/测速/连通、美食推荐、锁屏/关机/重启、命令执行、文件读写、打开应用等。

安全机制（三层）：
1. Prompt 层：`src/main.py` 的 `SYSTEM_SAFETY_RULES` 运行时附加到系统提示词。
2. 代码层：`_check_command_blocked()` 黑名单；文件工具保护关键路径（阻止 `auth.json`、`.git` 等）。
3. 交互层：`APICallThread._request_confirmation()` → 主窗口确认弹窗；外部 MCP 工具默认每次确认。

## 6. 关键数据流

文字聊天：`MainWindow.send_message()` → 更新 `conversation_history` → 起 `APICallThread` → 工具调用循环：

1. 注入强制日期上下文（仅针对“今天几号”这类问题）。
2. 等待完整调用 `/chat/completions` 返回（tools 传入）：每轮最多 8 轮。
3. 有 tool_calls → 执行工具 → 结果拼回 messages → 下一轮；无 tool_calls → 直接返回最终回答。
4. 8 轮用完仍调工具 → 追加 system 提示后强制收尾。
5. 回答经 `split_reply_and_sources` 拆正文与来源 → 气泡显示；`ensure_datetime_consistency` 校验时间一致性。

语音输入：`WakeWordThread` 唤醒“小柚” → 录音 → `XfyunSTTThread` 流式识别 → 文本进输入框/直接发送。
语音回复：回答完成后 `TextToSpeechThread`（edge-tts → pygame 播放）。
会话持久化：每轮消息存 `data/chat_history.db`（`MessageDatabase`）。
附件上传：文本类文件（≤200KB、最多 5 个）→ 发送时拼给模型；气泡显示压缩为 `📎 附件：文件名`。

## 7. 配置体系

- `QSettings`（Windows 注册表）：界面中保存的用户覆盖、系统提示词、TTS 开关/音色、透明度等。
- `config/config.json`：Base URL、聊天模型、视觉模型、推理强度和 SearXNG URL。
- `config/auth.json`：模型、高德、Tavily、讯飞密钥。
- 优先级：QSettings 已保存值 → 对应 JSON 文件 → 程序默认值；按字段单独回退。
- `ConfigManager` 是唯一读写入口；`config/templates/` 是字段模板。
- 免安装包从 exe 同级 `config/` 读取可选的 `auth.json` / `config.json` / `mcp.json`；源码运行时从项目根 `config/` 读取。
- 免安装包从 exe 同级读取外置的 `assets/web_resources`、`generated/kws` 和可选 `config/apps.json`。

## 8. 开发注意事项（给 agent）

- 改文件前必须先征得用户确认（项目约定）。用户同意后再写。
- `src/main.py`、`src/action_control.py` 为 **UTF-8、CRLF**；其余 `.py` 多为 LF。改 CRLF 文件用临时 `.py` 补丁脚本读写（按二进制/`newline=''` 处理），避免行尾被改乱和管道中文乱码；替换锚点需先验证唯一。
- 新增/修改工具：同步改 `get_tool_definitions()` 的 schema 与 `execute_tool()` 的分发；高风险工具记得走确认弹窗。
- 测试：QWebEngine 在沙箱崩溃，测试时用假类替换 `PetWebView`/`PetInteractionOverlay`；可设 `QT_QPA_PLATFORM=offscreen`。
- 真实运行环境是系统 Python 3.12；`scripts/run.bat` 走 conda 环境 `ai_assistant`。
- 密钥不要写进代码、`config/config.json`、日志或提交；`config/auth.json`、`config/config.json`、`config/mcp.json` 已被 `.gitignore` 忽略。
- 打包后 exe 目录结构：`Aissist.exe` + `_internal/` + `assets/` + `config/templates/` + `generated/kws/` + `README.md`；改资源布局时同步改 `scripts/build_portable.bat`。
