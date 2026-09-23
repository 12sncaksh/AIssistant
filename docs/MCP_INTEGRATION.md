# MCP（Model Context Protocol）集成说明

给 Aissist 接上外部 MCP server：别人写好的工具（文件、浏览器、数据库、公司内部系统……）
不用改这个项目的代码，改一个 json 就能让模型用上。

## 快速上手

1. 把 `config/templates/mcp.example.json` 复制成 `config/mcp.json`（源码运行）；免安装包放在 exe 同级的 `config/mcp.json`。
2. 在 `mcpServers` 里填一个 server。
3. 重启 Aissist，之后随便发一句话，模型就有机会调用这些工具了。
4. 查看已有工具：菜单「设置 → MCP 工具管理」（只读窗口，显示 server 状态、工具名/参数/描述，可打开配置和日志目录）；或命令行 `python scripts\list_mcp_tools.py`。

## mcp.json 格式

和 Claude Desktop 的格式一致，只读 `mcpServers` 这一段：

```json
{
  "mcpServers": {
    "demo": {
      "command": "python",
      "args": ["mcp_servers\\demo_server.py"],
      "enabled": false
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:\\notes"],
      "autoApprove": false
    }
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `command` | 是 | 启动 server 的可执行文件（`npx` / `uvx` / `python` / exe 路径） |
| `args` | 否 | 命令行参数数组 |
| `env` | 否 | 额外环境变量，会叠加在继承来的环境之上（不会清空 PATH） |
| `cwd` | 否 | server 的工作目录 |
| `enabled` | 否 | 写 `false` 就跳过，方便临时关掉某个 server |
| `autoApprove` | 否 | 默认 `false`：每次调用都弹窗让你确认；写 `true` 才免确认 |
| `type` | 否 | 传输方式：省略或 `stdio`（本地子进程）、`http`（Streamable HTTP）、`sse` |
| `url` | 远程必填 | 远程 server 地址；写了 `url` 且没写 `type` 时按 `http` 处理 |
| `headers` | 否 | 远程 server 的 HTTP 头，如 `{"Authorization": "Bearer <token>"}` |

## 传输方式

- **stdio（默认）**：本地子进程，用 `command` + `args` 启动。
- **http（Streamable HTTP）**：远程 server，用 `type: "http"` + `url` + `headers`。
- **sse**：旧版 SSE，用 `type: "sse"` + `url` + `headers`。

远程 server 本地不启进程（`command`/`args`/`env`/`cwd` 仅对 stdio 有效）：

```json
"remote-example": {
  "type": "http",
  "url": "https://api.githubcopilot.com/mcp/",
  "headers": { "Authorization": "Bearer <your-token>" }
}
```

## 工具名规则

外部工具会以 `mcp__<server名>__<工具名>` 的形式出现在工具表里，例如 `mcp__demo__echo`。
加前缀是为了不和内置工具（`read_file`、`search_web`……）撞名。名字里的非法字符会换成 `_`，
超过 64 字符会截断并拼 8 位哈希。

## 权限确认

默认**每个 MCP 工具调用都要你点一次确认**，弹窗里会显示工具名、来源 server 和完整参数。
确认免费的办法是在 `config/mcp.json` 里给信任的 server 写 `"autoApprove": true`。

## 日志与排错

- server 自己的 stderr：`logs/mcp_<server名>.log`
- Aissist 主日志：`logs/app.log`，里面能看到“MCP server xxx 已就绪 / 启动失败”
- server 起不来时工具表里就是不出现它的工具，不会影响内置工具和其他 server
- 常见原因：stdio——`command` 写错、`npx`/`uvx` 没装、args 里的路径不存在；远程——`url` 写错、网络不通、`headers` 里的 token 无效

## 示例 server

`mcp_servers/demo_server.py` 是一个不依赖任何第三方包的最小示例（两个工具：`echo`、`now`），
用来验证链路：把 `mcp.json` 里 demo 那段的 `enabled` 改成 `true`，重启后工具表里应该出现
`mcp__demo__echo` 和 `mcp__demo__now`。

注意：它需要“装过 mcp 包的那个 python”来跑，所以只在源码模式（`scripts/run.bat`）下可用；
打包后的 exe 不带 python 解释器，跑不了这个示例。

## 已知限制

- **支持 stdio（本地子进程）与远程 Streamable HTTP / SSE**；远程用 `type` + `url` + `headers` 配置。
- **改完 `config/mcp.json` 要重启**才生效；程序运行中不会重读。
- **懒启动**：配置了 server 之后，是发第一条消息时才去拉进程，工具表大约 1~2 秒后才补上
  这些工具。没配 server 时不产生任何启动开销（没装 mcp 包也只影响 MCP 功能本身）。
- **子 agent 用不了 MCP 工具**：`SUBAGENT_TOOL_WHITELIST` 是白名单制，只放内置工具。
- 单个 server 最长等 120 秒连上；单次工具调用超时 180 秒。
- 工具返回内容超过 20000 字符会被截断，避免一把塞爆上下文。

## 安全提醒

MCP server 分两类：stdio 是**你本机的普通子进程**，以你的账户权限运行；远程（http/sse）会把请求和数据发到远端地址。
两类能干什么都完全取决于那个 server，只接你信得过的；不确定的就别写 `autoApprove`。
远程 server 的 `headers` 里常含 token，`config/mcp.json` 已被 `.gitignore` 忽略，但也不要外发。
