# AIssistant · 小柚

> Windows 桌面 AI 助手兼桌宠：PyQt6 + Live2D + 大模型聊天 + 结构化工具调用 + MCP 扩展 + 语音交互。

## 功能

- 大模型流式对话（OpenAI 兼容接口）；也可把整轮对话交给本机 DSH 运行时处理，见「DSH 桥接」
- 结构化工具调用（function calling）：联网搜索、天气/路线、系统状态、磁盘只读扫描、文件读写、锁屏/关机等
- MCP（Model Context Protocol）扩展：支持 stdio 本地 server 与远程 HTTP/SSE server，工具自动并入工具表；
  程序自身的系统/电源/桌宠能力也以自带 MCP server 的形式提供
- 语音：sherpa-onnx 唤醒「小柚」、讯飞语音听写、edge-tts 语音回复
- Live2D 桌宠：无边框置顶、拖拽、系统托盘、模型切换、大模型可调的表情/参数层
- 聊天界面：用户与 AI 全局头像更换、Markdown 排版、代码块复制；可通过“视图 → 聊天外观...”统一设置背景、气泡配色与透明度、代码块配色与字体、聊天正文字体
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

:: 可选：Live2D 语义情绪映射（不复制也能跑，程序用内置默认表，也可在菜单里生成模板）
copy config\templates\live2d_emotions.example.json config\live2d_emotions.json

:: 3) 运行
scripts\run_debug.bat      :: 前台调试（能看到报错）
scripts\run.bat            :: 隐藏启动
```

> ⚠️ **请先读「仓库不包含的资源」一节。** Live2D 前端页面与引擎、唤醒词模型、默认头像都没有入库；
> 缺这些资源程序仍能正常启动，但桌宠会空白、语音输入不可用。

## 配置说明

| 文件 | 内容 | 是否入库 |
| --- | --- | --- |
| `config/auth.json` | 密钥（模型 api_key、高德、Tavily、讯飞） | 否（已忽略） |
| `config/config.json` | Base URL、聊天模型、视觉模型、推理强度、SearXNG URL、DSH 桥接 | 否（已忽略） |
| `config/apps.json` | 常用应用快捷方式 | 否（已忽略） |
| `config/mcp.json` | MCP server 配置（自带 server 由程序自动写入） | 否（已忽略） |
| `config/live2d_emotions.json` | Live2D 语义情绪 → 表情/参数映射 | 否（程序可生成模板） |
| `config/dsh_sessions.json` | 聊天会话 ↔ DSH 会话映射 | 否（运行时自动生成） |
| `config/dsh_acp_overlay.yml` | DSH 路由覆盖补丁 | 否（填了 `dsh_provider` 时自动生成） |

优先级：设置窗口中已保存的值 → 对应 JSON 文件 → 程序默认值。头像文件保存在运行时 `data/avatars/`，QSettings 仅保存路径。

其它运行时目录：日志 `logs/app.log`（2MB × 3 滚动）、会话库 `data/chat_history.db`、
AI 生成的聊天背景 `generated/chat_backgrounds/`、DSH 工作区 `agent_workspace/`、
桌宠本地接口端口 `data/runtime_endpoint.json`（Live2D 服务的端口不是固定的，从 8123 起顺延，
由主程序在启动时发布给 `aissistant-pet` server）。

`config/config.json` 除上表字段外还支持这些可选键（不填即关闭）：
`dsh_bridge_enabled`、`dsh_command`、`dsh_profile`、`dsh_provider`、`dsh_model`，
完整模板见 `config/templates/config.example.json`。

## 仓库不包含的资源（重要）

下面这些资源因为再分发许可 / 个人文件 / 体积原因被 `.gitignore` 有意排除，
**只克隆仓库是跑不出完整效果的**；不过程序处处容错：缺资源时会跳过、回落到默认值或只写日志，不会崩溃。

| 缺失内容 | 路径 | 缺失后的表现 |
| --- | --- | --- |
| Live2D 前端页面 | `assets/web_resources/dist/pet.html`、`dist/index.html`、`dist/model-selector.js`、`dist/assets/` | 桌宠窗口空白；本地 HTTP 服务启动失败并在日志里记 `Live2D 页面资源缺失`（主程序继续运行） |
| Cubism 引擎 | `assets/web_resources/dist/Core/`、`dist/Framework/` | 页面无法渲染模型 |
| 唤醒词模型 | `generated/kws/`（3 个 `.onnx` + `tokens.txt` + `keywords.txt`） | 唤醒词不可用；麦克风流随之不启动，**「按住说话」语音输入也一起不可用** |
| 默认头像 | `assets/web_resources/user.png`、`mao.png`、`neuro.png` | 聊天气泡头像为空，可在「聊天外观」里自行上传 |
| 示例模型 | `assets/web_resources/dist/Resources/Hiyori/`、`Resources/Mao/` | 模型列表里只剩「大肥鱼」；一个模型都没有时启动会提示“没有模型” |
| 内部文档 | `docs/`、`工作文档/` | 与仓库无关，本 README 不再引用它们 |

### Live2D 引擎与前端页面

- **引擎**：Cubism Core / Framework 属 Live2D 专有许可，从 [Live2D 官网](https://www.live2d.com/) 获取
  **Cubism SDK for Web**，把 `Core/`（含 `live2dcubismcore.js`）与 `Framework/` 放到
  `assets/web_resources/dist/` 下。
- **页面**：本地 HTTP 服务的根目录就是 `assets/web_resources/dist/`，程序按固定文件名加载——
  `pet.html`（桌宠窗口）、`index.html`（聊天窗口右侧面板），并用查询参数指定模型：
  `?model=<模型目录名>&file=<模型>.model3.json`。`model-selector.js` 负责把页面内部的
  `/Resources/Mao/...` 请求重写到所选模型目录。
- **约定**：程序会向页面注入 JS（情绪层、`set_live2d_param` 参数层），并要求页面暴露
  `window.__petForwardPointer` 之类的交互入口；缺任何一环桌宠都不可用。
- **模型**：`Resources/` 下的每个子目录只要有 `*.model3.json` 且带 `FileReferences.Moc` /
  `Textures`，就会被扫描进模型列表。仓库内仅保留免费模型「大肥鱼」，其版权归原作者
  [氵六青的个人空间](https://space.bilibili.com/11272072)，请遵守其许可。

### 唤醒词模型（sherpa-onnx KWS）

程序写死的文件名是 `encoder/decoder/joiner-epoch-12-avg-2-chunk-16-left-64.onnx`，
正是 sherpa-onnx 官方中文 KWS 模型 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`：

```bat
:: 下载后把 3 个 .onnx 与 tokens.txt 放进 generated\kws\
curl -L -o kws.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
tar xf kws.tar.bz2
```

`keywords.txt` 是 sherpa-onnx 的关键词格式（拼音音素 + `@显示词`），唤醒词「小柚」是：

```text
x iǎo y òu @小柚
```

要改唤醒词，先写 `keywords_raw.txt`（每行 `中文 @中文`），再用 sherpa-onnx 的转换工具生成：

```bat
sherpa-onnx-cli text2token --tokens generated\kws\tokens.txt --tokens-type ppinyin keywords_raw.txt keywords.txt
```

## MCP 工具

### 自带的 MCP server

程序自身的一部分能力已经搬到 `mcp_servers/` 下的三个本地 MCP server。它们的配置
**由程序在启动时按当前机器的解释器和项目路径自动写入 `config/mcp.json`**，不需要手动填：

| server | 工具 | 是否免确认 |
| --- | --- | --- |
| `aissistant` | `system_status`、`health_check`、`disk_scan`、`network_info`、`speedtest`、`ping_host`、`get_weather`、`get_forecast`、`get_route`、`recommend_food_by_*`、`open_app`、`open_url` | 是（`autoApprove: true`） |
| `aissistant-power` | `lock_screen`、`shutdown`、`reboot` | **否，每次都要确认** |
| `aissistant-pet` | `control_pet`、`set_live2d_param`、`list_live2d_params` | 是（`autoApprove: true`） |

- 工具的 `description` 与参数 schema 直接取自 `ActionHandler`，执行也走同一个
  `ActionHandler.execute_tool()`，所以经 MCP 调用和内置调用行为逐字一致。
- 自动配置只校正 `command` / `args`，**不会改 `enabled`**：在「MCP 工具管理」里关掉的
  server 不会被重新打开。这三个名字由程序占用，同名条目会被校正成自带 server。
- **免安装版例外**：PyInstaller 包里既没有 Python 解释器也没有 `mcp_servers/`，这三个
  server 起不来。此时上表工具会回落到内置工具表继续可用（不会消失），
  代价是工具名不带 `mcp__` 前缀。

### 外部 MCP server

外部 MCP server 同样写在 `config/mcp.json` 的 `mcpServers` 段里（模板见 `config/templates/mcp.example.json`），两类写法：

```json
{
  "mcpServers": {
    "demo": { "command": "python", "args": ["mcp_servers\\demo_server.py"], "enabled": false },
    "remote-example": {
      "type": "http",
      "url": "https://example.com/mcp/",
      "headers": { "Authorization": "Bearer <your-token>" },
      "enabled": false
    }
  }
}
```

- 工具名统一为 `mcp__<server>__<tool>`，会自动并入工具表。
- `enabled: false` 表示不启用；默认启用。
- 每个 MCP 工具**默认都要在界面上确认**，某个 server 想免确认就加 `"autoApprove": true`。
- `"dsh": true` 表示启用「DSH 桥接」时也把这个 server 一并转交给 DSH 运行时（见下节）；
  没写这个键的只在本程序内生效。ACP 侧要求 `command` 是**存在的绝对路径**，解析不出来的
  server 会被自动跳过，以免拖垮整个桥接会话。
- 仓库自带最小示例 `mcp_servers/demo_server.py`（stdio，只需 `mcp` 包，源码运行时可用）。

查看已接入的工具：

- 菜单「设置 → MCP 工具管理」（查看状态并即时启用/禁用 server）
- 或命令行 `python scripts\list_mcp_tools.py`

## DSH 桥接（可选）

`src/dsh_acp_client.py` 可以把对话整体交给本机已安装的 **DSH（DeepSeek Harness）** 运行时：
在「设置 → 配置」里勾选 **启用 DSH 桥接** 后，模型、API Key、会话与工具执行都由 DSH 自己的
`dsh --profile acp` 进程负责（Agent Client Protocol，JSON-RPC over stdio），
本程序只负责桌面呈现（桌宠 / Live2D / TTS / 气泡）；此时本地的 Base URL / API Key / 模型会被禁用。

- **依赖**：本机可执行的 `dsh` 命令与 `acp` profile；没有就报 `无法启动 DSH（dsh）`。
- **配置键**：`dsh_bridge_enabled`、`dsh_command`（默认 `dsh`）、`dsh_profile`（默认 `acp`）、
  `dsh_provider`、`dsh_model`；写进 `config/config.json` 或在设置窗口里改都行。
- **运行时生成**：`agent_workspace/`（DSH 的工作目录）、`config/dsh_sessions.json`（会话映射）、
  以及仅在填了 `dsh_provider` 时才生成的 `config/dsh_acp_overlay.yml`。
- **自检**：`python scripts\test_dsh_bridge.py "随便说一句话"`。

## 打包免安装版

在 Windows 运行 `scripts\build_portable.bat`，生成 `Aissistant_v1.102.6_test_portable.zip`。

- 脚本先检查 `generated\kws\` 与 `assets\web_resources\dist\pet.html`：缺了会打印下载与放置说明并中止
  （免安装包必须自带 Live2D 页面与唤醒词模型，所以要先把「仓库不包含的资源」补齐）。
- 打包会把 `assets/web_resources`、`generated/kws`、`config/templates`（含 `live2d_emotions.example.json`）
  复制进包里，并删除 + 校验包内不含聊天记录数据库。
- `docs/` 不入库，所以包里的 `README.md` 会退回使用仓库根 README，并把这一步的失败当错误处理。

## 项目结构

```text
src/                     # 全部 Python 源码
  main.py                # 主程序：UI、线程、语音、API 调用、Live2D 页面与参数层
  action_control.py      # ActionHandler：内置工具 schema + 执行 + 安全黑名单
  mcp_client.py          # MCPManager：外部 MCP server 生命周期与调用分发
  dsh_acp_client.py      # DSH ACP 桥接客户端（可选功能）
  vision_input.py        # 视觉键鼠控制器（mss + Pillow + pyautogui）
  accessibility_input.py # UI Automation 读取
  input_guard.py         # 键鼠接管钩子
config/templates/        # 配置模板（随包发布）
scripts/                 # 启动 / 打包 / 自检脚本（run*.bat、build_portable.bat、Aissistant.spec 等）
assets/                  # 图标与 web_resources（Live2D 页面、引擎、模型，多为本地资源）
mcp_servers/             # 自带 MCP server（系统/电源/桌宠）+ demo 示例
generated/               # 运行时生成：kws 唤醒词模型、chat_backgrounds
data/  logs/             # 运行时数据与日志（不入库）
```

## 许可

- 本项目代码：[MIT License](LICENSE)
- Live2D Cubism Core / Framework、Live2D 前端页面与个人资源不随本仓库分发，
  版权归 **Live2D Inc.** 及各自作者所有。
- 「大肥鱼」模型为免费资源，版权归原作者所有。

## 致谢

- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/)
- [Live2D Cubism SDK](https://www.live2d.com/)
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
- [Model Context Protocol](https://modelcontextprotocol.io/)
