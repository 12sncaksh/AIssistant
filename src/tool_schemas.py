"""Built-in tool schemas advertised to the model."""

from __future__ import annotations
from typing import Any



def _builtin_tool_definitions(cls) -> list[dict[str, Any]]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_current_datetime",
                "description": "获取当前本机的日期、星期、时间和时区。凡是用户询问今天几号、星期几、现在几点之类的问题，都应优先使用这个工具。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "open_url",
                "description": "仅当用户明确给出完整 http:// 或 https:// 网址时使用，在默认浏览器中打开该网址。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "用户明确提供的完整网址，必须以 http:// 或 https:// 开头。"
                        }
                    },
                    "required": ["url"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "查询实时信息、新闻、最新动态或需要联网检索的事实问题。搜索词必须贴合用户原意，不要随意篡改赛事类型或年份。先用 SearXNG 搜索，再用 Tavily 抓取正文片段。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "适合搜索引擎检索的精确查询词。"
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_url",
                "description": "读取指定网页链接的正文内容，用于深入了解搜索结果或用户提供的链接。只读操作，不会修改任何内容。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "要读取的完整网址，必须以 http:// 或 https:// 开头。"
                        }
                    },
                    "required": ["url"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询某个城市的实时天气。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名，例如北京、上海。"}
                    },
                    "required": ["city"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_forecast",
                "description": "查询某个城市未来几天的天气预报。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名，例如北京、上海。"},
                        "days": {
                            "type": "integer",
                            "description": "预报天数，建议 1 到 7 天。",
                            "minimum": 1,
                            "maximum": 7
                        }
                    },
                    "required": ["city", "days"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_route",
                "description": "查询从起点到终点的路线规划。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin": {"type": "string", "description": "起点名称或地址。"},
                        "destination": {"type": "string", "description": "终点名称或地址。"},
                        "mode": {
                            "type": "string",
                            "description": "路线方式。",
                            "enum": ["drive", "walk", "bus", "bike"]
                        }
                    },
                    "required": ["origin", "destination", "mode"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "system_status",
                "description": "获取当前电脑的 CPU、内存、磁盘、显卡等系统性能信息。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "health_check",
                "description": "电脑体检：一次性获取 CPU、内存、磁盘、显卡、网络、开机启动项、电量等状态，用于判断电脑是否健康、找出卡顿原因。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "disk_scan",
                "description": "只读扫描电脑上的临时文件、浏览器缓存、回收站、Windows更新缓存、下载文件夹大文件等可清理内容，返回清单和建议；本工具绝不删除任何文件。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "network_info",
                "description": "查询本机内网 IP、公网 IP、MAC 地址等网络信息。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "speedtest",
                "description": "测试当前网络的下载、上传速度和延迟。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "ping_host",
                "description": "对指定域名或 IP 发起 ping 测试，检查网络连通性。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "description": "域名或 IP 地址，例如 baidu.com。"}
                    },
                    "required": ["host"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "recommend_food_by_city",
                "description": "按城市和关键词搜索餐厅或美食推荐。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名，例如北京。"},
                        "keyword": {"type": "string", "description": "美食关键词，例如火锅、烧烤。"}
                    },
                    "required": ["city", "keyword"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "recommend_food_by_address",
                "description": "按地址文本、半径和关键词搜索附近餐厅。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "详细地址文本，例如北京市朝阳区望京 SOHO。"},
                        "radius": {
                            "type": "integer",
                            "description": "搜索半径，单位米。",
                            "minimum": 100,
                            "maximum": 10000
                        },
                        "keyword": {"type": "string", "description": "美食关键词，例如火锅、咖啡。"}
                    },
                    "required": ["address", "radius", "keyword"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "recommend_food_by_coordinates",
                "description": "按经纬度、半径和关键词搜索附近餐厅。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lng": {"type": "number", "description": "经度。"},
                        "lat": {"type": "number", "description": "纬度。"},
                        "radius": {
                            "type": "integer",
                            "description": "搜索半径，单位米。",
                            "minimum": 100,
                            "maximum": 10000
                        },
                        "keyword": {"type": "string", "description": "美食关键词，例如火锅、咖啡。"}
                    },
                    "required": ["lng", "lat", "radius", "keyword"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "lock_screen",
                "description": "锁定当前电脑屏幕。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "shutdown",
                "description": "关闭当前电脑。属于高风险操作，仅在用户明确要求关机时使用。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "reboot",
                "description": "重启当前电脑。属于高风险操作，仅在用户明确要求重启时使用。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "在电脑上执行一条 PowerShell 命令并返回输出结果，用于完成需要操作系统级能力的任务。属于高风险操作，执行前会请求用户确认。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的 PowerShell 命令。"}
                    },
                    "required": ["command"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取指定文本文件的内容，用于查看代码、配置、日志等文件。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "要读取的文件完整路径。"}
                    },
                    "required": ["path"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "把内容写入指定文件（会覆盖已有文件，必要时自动创建目录）。属于高风险操作，执行前会请求用户确认。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "要写入的文件完整路径。"},
                        "content": {"type": "string", "description": "要写入的文件内容。"}
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "列出指定目录下的文件和子目录（最多 200 项）。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "要列出的目录路径，默认当前目录。"}
                    },
                    "required": ["path"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_to_agent",
                "description": "当任务需要多步探索或反复尝试，比如查看或修改代码、写脚本、深度联网调研、需要多次搜索交叉验证时，应优先使用本工具把任务委派给专业子agent处理，而不是自己一步步调用大量工具。agent_type：search=联网调研；code=代码修改与脚本编写。单步确定性操作（查天气、开应用、单次搜索、读单个文件）不要使用本工具，直接用具体工具。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_type": {
                            "type": "string",
                            "enum": ["search", "code"],
                            "description": "search=联网调研；code=代码修改。"
                        },
                        "task": {
                            "type": "string",
                            "description": "交给子agent的完整任务描述，包含目标、约束、输出要求。"
                        }
                    },
                    "required": ["agent_type", "task"],
                    "additionalProperties": False
                }
            }
        }
    ]

    tools.extend([
        {
            "type": "function",
            "function": {
                "name": "inspect_screen",
                "description": "截取整个桌面并调用视觉模型定位界面元素。仅在无法使用 inspect_foreground_window 获取前台窗口边界时使用；返回目标的原始桌面坐标。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "description": "要在当前屏幕中寻找的目标，例如：浏览器图标、DeepSeek网页输入框、发送按钮。"
                        }
                    },
                    "required": ["instruction"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "inspect_foreground_window",
                "description": "先读取当前前台窗口的 UI Automation 边界，再只截取该窗口区域调用视觉模型。视觉任务开始定位应用时优先使用，禁止无依据地扫描多个桌面象限。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "instruction": {"type": "string", "description": "要在当前前台窗口中识别的目标或状态。"}
                    },
                    "required": ["instruction"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "inspect_accessibility",
                "description": "读取当前前台窗口的 Windows UI Automation 可访问性树，按名称或控件类型筛选真实控件边界。视觉定位后应优先使用此工具交叉验证。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "要查找的控件名称或类型，例如发送、地址栏、Button。"
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "inspect_screen_region",
                "description": "只截取指定桌面区域并调用视觉模型。首次全屏定位目标后，后续应优先使用此工具读取棋盘、网页局部或按钮区域，以降低延迟。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "instruction": {"type": "string", "description": "要在局部区域中识别的目标或状态。"},
                        "x": {"type": "integer", "description": "区域左上角全局 X 坐标。"},
                        "y": {"type": "integer", "description": "区域左上角全局 Y 坐标。"},
                        "width": {"type": "integer", "description": "区域宽度。"},
                        "height": {"type": "integer", "description": "区域高度。"}
                    },
                    "required": ["instruction", "x", "y", "width", "height"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "mouse_move",
                "description": "把鼠标移动到视觉模型返回的桌面坐标。仅用于视觉键鼠任务。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer", "description": "屏幕全局 X 坐标。"},
                        "y": {"type": "integer", "description": "屏幕全局 Y 坐标。"}
                    },
                    "required": ["x", "y"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "verified_mouse_click",
                "description": "先用视觉模型给出的坐标和 Windows UI Automation 控件名称交叉验证，再点击真实控件中心。UIA 验证失败时拒绝盲点。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer", "description": "视觉模型返回的屏幕全局 X 坐标。"},
                        "y": {"type": "integer", "description": "视觉模型返回的屏幕全局 Y 坐标。"},
                        "target_label": {"type": "string", "description": "视觉模型识别的控件名称，例如发送、地址栏。"},
                        "button": {"type": "string", "enum": ["left", "right", "middle"]},
                        "clicks": {"type": "integer", "enum": [1, 2]}
                    },
                    "required": ["x", "y", "target_label", "button", "clicks"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "mouse_click",
                "description": "在视觉模型返回的桌面坐标点击鼠标。仅用于用户明确要求的视觉键鼠任务。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer", "description": "屏幕全局 X 坐标。"},
                        "y": {"type": "integer", "description": "屏幕全局 Y 坐标。"},
                        "button": {"type": "string", "enum": ["left", "right", "middle"]},
                        "clicks": {"type": "integer", "enum": [1, 2]}
                    },
                    "required": ["x", "y", "button", "clicks"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "keyboard_type",
                "description": "向当前获得焦点的窗口输入文字。中文等 Unicode 文本通过剪贴板粘贴，不使用 PowerShell。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要输入的文字。"}
                    },
                    "required": ["text"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "keyboard_hotkey",
                "description": "执行键盘快捷键，例如 ctrl+l、ctrl+v、enter。仅用于视觉键鼠任务。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keys": {"type": "string", "description": "用加号连接的快捷键，例如 ctrl+l。"}
                    },
                    "required": ["keys"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "computer_wait",
                "description": "等待网页或桌面应用加载完成，然后再截图确认。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {"type": "number", "description": "等待秒数，范围 0.1 到 15。"}
                    },
                    "required": ["seconds"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "wait_for_user_action",
                "description": "检测到外部确认弹窗或需要用户在目标窗口操作时，暂时释放键鼠接管并等待用户处理；等待结束后自动重新接管并继续。不要用聊天回复代替这个工具。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {"type": "number", "description": "等待用户处理的秒数，范围 1 到 60。"}
                    },
                    "required": ["seconds"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "control_pet",
                "description": "控制桌面桌宠（Live2D）做出情绪表情，用于自然地增强互动。在用户夸奖、开玩笑、表达强烈情绪，或你自己想表达态度时主动调用，不要每轮都调用。emotion 可选：happy、sad、angry、surprised、shy、wink、cry、sleepy、love、sparkle、magic、heart、excited、confused、nervous、tongue、neutral；neutral 表示立即收回表情。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "emotion": {
                            "type": "string",
                            "enum": list(cls.PET_EMOTION_NAMES),
                            "description": "要表达的情绪。"
                        },
                        "intensity": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "情绪强度，0 到 1，默认 1。"
                        },
                        "duration_ms": {
                            "type": "integer",
                            "minimum": 500,
                            "maximum": 30000,
                            "description": "持续时间（毫秒），默认 6000。"
                        }
                    },
                    "required": ["emotion", "intensity", "duration_ms"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "set_live2d_param",
                "description": "高级用法：直接设置当前桌宠 Live2D 模型的一个或多个参数值，做语义情绪覆盖不到的表现。参数名必须来自 list_live2d_params；数值越界会自动裁剪，到期后自动回落，不会残留。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "parameters": {
                            "type": "array",
                            "description": "要设置的参数列表。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "Live2D 参数名，例如 ParamAngleZ。"},
                                    "value": {"type": "number", "description": "目标数值。"}
                                },
                                "required": ["id", "value"],
                                "additionalProperties": False
                            }
                        },
                        "duration_ms": {
                            "type": "integer",
                            "minimum": 500,
                            "maximum": 30000,
                            "description": "持续时间（毫秒），默认 6000。"
                        }
                    },
                    "required": ["parameters", "duration_ms"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_live2d_params",
                "description": "列出当前桌宠 Live2D 模型支持的参数名与说明（可按关键词过滤），用于 set_live2d_param。仅在需要直接设置参数时调用。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "过滤关键词，匹配参数名或说明；空字符串表示列出全部。"
                        }
                    },
                    "required": ["keyword"],
                    "additionalProperties": False
                }
            }
        },
    ])

    apps = cls.get_available_apps()
    if apps:
        tools.insert(0, {
            "type": "function",
            "function": {
                "name": "open_app",
                "description": "通过 config/apps.json 中的快捷配置打开本地应用或网址。列表外应用不要调用本工具；应在用户确认后使用 run_command，或使用视觉键鼠通过桌面/开始菜单打开。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app_name": {
                            "type": "string",
                            "description": "要打开的应用名称。",
                            "enum": apps
                        }
                    },
                    "required": ["app_name"],
                    "additionalProperties": False
                }
            }
        })

    # 记忆检索（pull）：让模型自己决定要不要翻历史，而不是系统提前替它筛。
    # 局部导入，避免工具表这个轻量模块在导入期就牵动整个记忆层。
    from tools_memory import memory_tool_definitions
    tools.extend(memory_tool_definitions())

    return tools
