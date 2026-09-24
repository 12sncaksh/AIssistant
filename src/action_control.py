# action_control.py
import json
import os
import re
import sys
import webbrowser
import requests
import platform
import cpuinfo
import psutil
import requests        # 公网IP查询（已有）
import scapy           # 扫描局域网设备（可能需管理员权限）
import socket
import uuid
import pyperclip
import logging
from typing import Any
from datetime import datetime
from urllib.parse import urlparse

from vision_input import VisionInputController
from mcp_client import MCPManager  # 外部 MCP（Model Context Protocol）工具，配置见 mcp.json

try:
    from pynvml import *
    NVML_AVAILABLE = True
except ImportError:
    NVML_AVAILABLE = False

COMMAND_TIMEOUT_SECONDS = 120

class ActionHandler:
    _apps = None          # 缓存 { "应用名称": "启动路径" }
    _base_dir = (
        os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    _config_path = os.path.join(_base_dir, "config", "apps.json")
    _search_config = {
        "searxng_url": "http://localhost:18080",
        "tavily_api_key": ""
    }
    _auth_config_path = os.path.join(_base_dir, "config", "auth.json")
    _auth_config = None   # { "amap_api_key": "...", "tavily_api_key": "..." }
    _agent_workspace = None  # 当前子agent隔离工作区（None=主循环不限制）
    _amap_api_key = ""    # 显式配置（GUI/环境变量），优先于 auth.json
    _timeout_confirm_callback = None  # 命令超时时询问用户是否继续等待的回调

    @classmethod
    def load_apps(cls, config_path=None):
        if config_path:
            if not os.path.isabs(config_path):
                config_path = os.path.join(cls._base_dir, config_path)
            cls._config_path = config_path
        if not os.path.exists(cls._config_path):
            logging.warning("配置文件 %s 不存在，应用列表为空", cls._config_path)
            cls._apps = {}
            return
        with open(cls._config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            apps_list = data.get("apps", [])
            cls._apps = {app["name"]: app["path"] for app in apps_list}
        logging.info("已加载 %s 个应用配置", len(cls._apps))

    @classmethod
    def load_auth_config(cls, config_path=None):
        """从 config/auth.json 读取第三方 API Key（该文件已被 .gitignore 忽略）。"""
        if config_path:
            if not os.path.isabs(config_path):
                config_path = os.path.join(cls._base_dir, config_path)
            cls._auth_config_path = config_path
        cls._auth_config = {}
        try:
            with open(cls._auth_config_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            if isinstance(data, dict):
                cls._auth_config = {
                    "amap_api_key": str(data.get("amap_api_key", "") or "").strip(),
                    "tavily_api_key": str(data.get("tavily_api_key", "") or "").strip()
                }
        except FileNotFoundError:
            pass
        except Exception as e:
            logging.warning("读取 %s 失败: %s", cls._auth_config_path, e)

    @classmethod
    def configure(cls, searxng_url=None, tavily_api_key=None, amap_api_key=None,
                  base_url=None, api_key=None, vision_model=None):
        if searxng_url is not None:
            searxng_url = searxng_url.strip()
            cls._search_config["searxng_url"] = searxng_url.rstrip("/") or "http://localhost:18080"
        if tavily_api_key is not None:
            cls._search_config["tavily_api_key"] = tavily_api_key.strip()
        if amap_api_key is not None:
            cls._amap_api_key = amap_api_key.strip()
        VisionInputController.configure(
            base_url=base_url,
            api_key=api_key,
            vision_model=vision_model,
        )

    @classmethod
    def _get_amap_api_key(cls) -> str:
        """获取高德 API Key：显式配置 > 环境变量 AMAP_API_KEY > auth.json。"""
        if cls._amap_api_key:
            return cls._amap_api_key
        env_key = os.getenv("AMAP_API_KEY", "").strip()
        if env_key:
            return env_key
        cls.load_auth_config()
        return cls._auth_config.get("amap_api_key", "").strip()

    _CITY_ADCODE = {
        # 常用城市 -> adcode 兜底表（高德地理编码对纯城市名可能解析失败）
        "北京": "110000",
        "上海": "310000",
        "天津": "120000",
        "重庆": "500000",
        "广州": "440100",
        "深圳": "440300",
        "成都": "510100",
        "杭州": "330100",
        "武汉": "420100",
        "南京": "320100",
        "西安": "610100",
        "郑州": "410100",
        "济南": "370100",
        "青岛": "370200",
        "沈阳": "210100",
        "大连": "210200",
        "长春": "220100",
        "哈尔滨": "230100",
        "石家庄": "130100",
        "太原": "140100",
        "呼和浩特": "150100",
        "兰州": "620100",
        "西宁": "630100",
        "银川": "640100",
        "乌鲁木齐": "650100",
        "拉萨": "540100",
        "昆明": "530100",
        "贵阳": "520100",
        "南宁": "450100",
        "海口": "460100",
        "福州": "350100",
        "厦门": "350200",
        "南昌": "360100",
        "长沙": "430100",
        "合肥": "340100",
        "苏州": "320500",
        "无锡": "320200",
        "宁波": "330200",
        "温州": "330300",
        "绍兴": "330600",
        "嘉兴": "330400",
        "金华": "330700",
        "台州": "331000",
        "佛山": "440600",
        "东莞": "441900",
        "珠海": "440400",
        "中山": "442000",
        "惠州": "441300",
        "泉州": "350500",
        "烟台": "370600",
        "潍坊": "370700",
        "徐州": "320300",
        "常州": "320400",
        "南通": "320600",
        "扬州": "321000",
        "洛阳": "410300",
        "襄阳": "420600",
        "宜昌": "420500",
        "岳阳": "430600",
        "衡阳": "430400",
        "桂林": "450300",
        "柳州": "450200",
        "三亚": "460200",
        "唐山": "130200",
        "保定": "130600",
        "邯郸": "130400",
        "廊坊": "131000",
        "秦皇岛": "130300",
        "威海": "371000",
        "淄博": "370300",
        "临沂": "371300",
        "包头": "150200",
        "大理": "532900",
        "丽江": "530700",
        "遵义": "520300",
    }


    @staticmethod
    def _city_to_adcode(city, api_key):
        # 将城市名转换为高德 adcode：先地理编码，失败则查内置城市表。
        try:
            geo_url = f"https://restapi.amap.com/v3/geocode/geo?address={city}&output=json&key={api_key}"
            resp = requests.get(geo_url, timeout=5)
            geo_data = resp.json()
            if geo_data.get("status") == "1" and geo_data.get("geocodes"):
                return geo_data["geocodes"][0]["adcode"]
        except Exception:
            pass
        key = city.strip().replace("市", "").replace("省", "")
        return ActionHandler._CITY_ADCODE.get(key, "")

    @classmethod
    def get_available_apps(cls) -> list[str]:
        if cls._apps is None:
            cls.load_apps()
        return list(cls._apps.keys())

    @classmethod
    def get_tool_definitions(cls) -> list[dict[str, Any]]:
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

        # 合并外部 MCP server 提供的工具（还没有 server 就绪时返回空列表，不影响内置工具）
        tools.extend(MCPManager.get_tool_definitions())

        return tools

    # 子agent可用的工具白名单（按 agent_type）
    SUBAGENT_TOOL_WHITELIST = {
        "search": ["search_web", "read_url"],
        "code": ["read_file", "list_dir", "run_command", "write_file", "search_web"],
    }

    @classmethod
    def get_subagent_tool_definitions(cls, agent_type: str) -> list[dict[str, Any]]:
        whitelist = set(cls.SUBAGENT_TOOL_WHITELIST.get(agent_type, []))
        return [t for t in cls.get_tool_definitions() if t["function"]["name"] in whitelist]

    @staticmethod
    def _result(success: bool, message: str, data: Any = None) -> dict[str, Any]:
        payload = {"success": success, "message": message}
        if data is not None:
            payload["data"] = data
        return payload

    # 高风险/破坏性命令黑名单（不区分大小写匹配）
    BLOCKED_COMMAND_PATTERNS = (
        # 递归/强制删除
        (r"Remove-Item\s+.*-(Recurse|R)\b", "命令包含递归删除操作（Remove-Item -Recurse），已阻止。"),
        (r"Remove-Item\s+.*-Force\b", "命令包含强制删除操作（Remove-Item -Force），已阻止。"),
        (r"\|\s*Remove-Item\b", "命令包含管道删除操作（| Remove-Item），已阻止。"),
        (r"rd\s+/s", "命令包含递归删除操作（rd /s），已阻止。"),
        (r"rmdir\s+/s", "命令包含递归删除操作（rmdir /s），已阻止。"),
        (r"\bdel\s+/(?:s|q|f)", "命令包含强制删除操作（del /s /q），已阻止。"),
        (r"\|\s*rm\b", "命令包含管道删除操作（| rm），已阻止。"),
        (r"Clear-RecycleBin", "命令包含清空回收站操作，已阻止。"),
        # 磁盘格式化 / 分区 / 擦除
        (r"Format-(?:Volume|Drive|Partition)\b", "命令包含磁盘格式化操作，已阻止。"),
        (r"\bformat\s+[a-zA-Z]:", "命令包含磁盘格式化操作，已阻止。"),
        (r"Clear-Disk\b", "命令包含磁盘清理操作，已阻止。"),
        (r"Initialize-Disk\b", "命令包含磁盘初始化操作，已阻止。"),
        (r"diskpart\b", "命令包含磁盘分区操作（diskpart），已阻止。"),
        (r"\bclean\s+all\b", "命令包含磁盘擦除操作，已阻止。"),
        (r"cipher\s+/w:", "命令包含磁盘擦除操作，已阻止。"),
        # 关机 / 重启
        (r"\bshutdown\b", "命令包含关机/重启操作，请改用专门的关机、重启工具。"),
        (r"\breboot\b", "命令包含关机/重启操作，请改用专门的关机、重启工具。"),
        (r"\bStop-Computer\b", "命令包含关机操作，请改用专门的关机工具。"),
        (r"\bRestart-Computer\b", "命令包含重启操作，请改用专门的重启工具。"),
        # 注册表
        (r"reg\s+delete\b", "命令包含注册表删除操作，已阻止。"),
        (r"reg\s+import\b", "命令包含注册表导入操作，已阻止。"),
        (r"regedit\s*/s", "命令包含静默导入注册表操作，已阻止。"),
        # 系统关键区域
        (r"takeown\s+/f\s+c:\\", "命令涉及系统关键目录所有权修改，已阻止。"),
        (r"icacls\s+.*/grant:r", "命令包含系统权限重置修改，已阻止。"),
        (r"vssadmin\s+delete\b", "命令包含卷影副本删除操作，已阻止。"),
        (r"bcdedit\b", "命令包含启动配置修改操作，已阻止。"),
        # 混淆 / 绕过
        (r"-EncodedCommand", "命令疑似使用编码混淆，已阻止。"),
    )

    # 禁止写入的受保护路径（不区分大小写，前缀匹配）
    BLOCKED_WRITE_PATHS = (
        "c:\\windows",
        "c:\\program files",
        "c:\\program files (x86)",
        "c:\\programdata",
        "c:\\$recycle.bin",
        "c:\\recovery",
        "c:\\system volume information",
        "c:\\users\\default",
    )

    @classmethod
    def set_agent_workspace(cls, workspace):
        """设置当前子agent的隔离工作区（None 表示主循环，不限制路径）。"""
        cls._agent_workspace = workspace

    @classmethod
    def set_timeout_confirm_callback(cls, callback):
        """设置命令超时续等确认回调：callback(command, elapsed, timeout) -> bool。"""
        cls._timeout_confirm_callback = callback

    @classmethod
    def _confirm_continue_waiting(cls, command, elapsed, timeout):
        """命令超时后询问用户是否继续等待；无回调时按原超时行为处理。"""
        callback = cls._timeout_confirm_callback
        if callback is None:
            return False
        try:
            return bool(callback(command, elapsed, timeout))
        except Exception:
            logging.warning("命令超时续等确认回调失败", exc_info=True)
            return False

    @classmethod
    def _resolve_agent_path(cls, path: str):
        """子agent工作区路径解析：有工作区时相对路径落到工作区内，工作区外绝对路径拒绝。返回 (解析后路径, 错误信息)。"""
        workspace = cls._agent_workspace
        if not workspace:
            return path, None
        path = str(path or "").strip() or "."
        ws_norm = os.path.normpath(workspace)
        if not os.path.isabs(path):
            norm = os.path.normpath(os.path.join(workspace, path))
        else:
            norm = os.path.normpath(path)
        if norm == ws_norm or norm.startswith(ws_norm + os.sep):
            return norm, None
        return path, f"当前子agent的工作区被限制在 {workspace}，禁止访问工作区外的路径：{path}"

    @staticmethod
    def _check_command_blocked(command: str):
        """检查命令是否命中黑名单，命中返回拦截原因，否则返回 None。"""
        lower_cmd = (command or "").lower()
        if "remove-item" in lower_cmd and ("-recurse" in lower_cmd or "-force" in lower_cmd):
            return "命令包含递归/强制删除操作（Remove-Item -Recurse/-Force），已阻止。"
        if re.search(r"\brm\b", lower_cmd) and re.search(r"-(?:rf|fr|r|f|recurse|force)\b", lower_cmd):
            return "命令包含递归/强制删除操作（rm -rf），已阻止。"
        for pattern, reason in ActionHandler.BLOCKED_COMMAND_PATTERNS:
            if re.search(pattern, lower_cmd, re.IGNORECASE):
                return reason
        return None

    @classmethod
    def _find_outside_workspace_paths(cls, command: str, workspace: str) -> list[str]:
        """提取命令中出现在工作区外的字面绝对路径（如 C: 盘符路径），用于限制子agent命令范围。"""
        ws_norm = os.path.normpath(workspace)
        found = []
        for match in re.finditer(r"[A-Za-z]:\\[^\s\"'|&;<>()]*", command or ""):
            raw = match.group(0).rstrip("\\/.,;:")
            if not raw:
                continue
            try:
                norm = os.path.normpath(raw)
            except Exception:
                continue
            if norm == ws_norm or norm.startswith(ws_norm + os.sep):
                continue
            found.append(raw)
        return found

    @staticmethod
    def _run_command(command: str, timeout: int = COMMAND_TIMEOUT_SECONDS, cwd: str | None = None):
        """执行 PowerShell 命令，返回 (成功, 消息, 数据)。带超时和输出截断。"""
        blocked_reason = ActionHandler._check_command_blocked(command)
        if blocked_reason:
            return False, "已阻止危险命令：" + blocked_reason, None
        if cwd:
            outside = ActionHandler._find_outside_workspace_paths(command, cwd)
            if outside:
                return False, (
                    f"命令包含工作区外的路径（{outside[0]}）。子agent 被限制在 {cwd} 内执行命令，"
                    "请改用相对路径或工作区内的路径。"
                ), None
        import subprocess
        try:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            return False, "命令执行失败: {e}".format(e=e), None
        waited = 0
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                break
            except subprocess.TimeoutExpired:
                waited += timeout
                if not ActionHandler._confirm_continue_waiting(command, waited, timeout):
                    try:
                        proc.kill()
                        stdout, stderr = proc.communicate()
                    except Exception:
                        stdout, stderr = b"", b""
                    return False, "命令执行超时（已等待 {waited} 秒且用户选择结束），已终止。".format(waited=waited), None
        raw = (stdout or b"") + ((b"\n" + stderr) if stderr else b"")
        output = None
        for enc in ("utf-8", "gbk"):
            try:
                output = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if output is None:
            output = raw.decode("utf-8", errors="replace")
        output = output.strip()
        if len(output) > 4000:
            output = output[:4000] + "\n" + "...（输出过长已截断）"
        exit_code = getattr(proc, "returncode", -1)
        msg = "命令已执行，退出码 {exit_code}。".format(exit_code=exit_code)
        if output:
            msg += "\n" + output
        else:
            msg += "（无输出）"
        return True, msg, {"exit_code": exit_code, "output": output}

    @classmethod
    def execute_tool(cls, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        if cls._apps is None:
            cls.load_apps()

        try:
            if tool_name == "open_app":
                app_name = str(arguments.get("app_name", "")).strip()
                if not app_name:
                    return cls._result(False, "应用名称不能为空。")
                if app_name not in cls._apps:
                    return cls._result(False, f"未找到应用“{app_name}”，请检查 config/apps.json。")
                path = cls._apps[app_name]
                try:
                    if path.startswith(("http://", "https://")):
                        webbrowser.open(path)
                    elif sys.platform == "win32":
                        os.startfile(path)
                    else:
                        return cls._result(False, f"当前系统不支持打开 {path}")
                    return cls._result(True, f"已打开 {app_name}")
                except Exception as e:
                    return cls._result(False, f"打开 {app_name} 失败: {str(e)}")

            if tool_name == "open_url":
                url = str(arguments.get("url", "")).strip()
                if not url.startswith(("http://", "https://")):
                    return cls._result(False, "仅支持以 http:// 或 https:// 开头的完整网址。")
                try:
                    webbrowser.open(url)
                    return cls._result(True, f"已在默认浏览器中打开 {url}")
                except Exception as e:
                    return cls._result(False, f"打开网址失败: {str(e)}")

            if tool_name == "inspect_screen":
                instruction = str(arguments.get("instruction", "")).strip()
                if not instruction:
                    return cls._result(False, "视觉定位目标不能为空。")
                return VisionInputController.inspect_screen(instruction)

            if tool_name == "inspect_foreground_window":
                return VisionInputController.inspect_foreground_window(arguments.get("instruction", ""))

            if tool_name == "inspect_accessibility":
                return VisionInputController.inspect_accessibility(arguments.get("query", ""))

            if tool_name == "inspect_screen_region":
                return VisionInputController.inspect_screen_region(
                    arguments.get("instruction", ""),
                    arguments.get("x"),
                    arguments.get("y"),
                    arguments.get("width"),
                    arguments.get("height"),
                )

            if tool_name == "mouse_move":
                return VisionInputController.mouse_move(arguments.get("x"), arguments.get("y"))

            if tool_name == "mouse_click":
                return VisionInputController.mouse_click(
                    arguments.get("x"),
                    arguments.get("y"),
                    arguments.get("button", "left"),
                    arguments.get("clicks", 1),
                )

            if tool_name == "verified_mouse_click":
                return VisionInputController.verified_mouse_click(
                    arguments.get("x"),
                    arguments.get("y"),
                    arguments.get("target_label", ""),
                    arguments.get("button", "left"),
                    arguments.get("clicks", 1),
                )

            if tool_name == "keyboard_type":
                return VisionInputController.keyboard_type(arguments.get("text", ""))

            if tool_name == "keyboard_hotkey":
                return VisionInputController.keyboard_hotkey(arguments.get("keys", ""))

            if tool_name == "computer_wait":
                return VisionInputController.wait(arguments.get("seconds", 1))

            if tool_name == "wait_for_user_action":
                return VisionInputController.wait_for_user_action(arguments.get("seconds", 15))

            if tool_name == "get_current_datetime":
                success, msg, data = cls._get_current_datetime()
                return cls._result(success, msg, data)

            if tool_name == "search_web":
                query = str(arguments.get("query", "")).strip()
                user_intent = str(arguments.get("_user_intent", "")).strip()
                if not query:
                    return cls._result(False, "搜索关键词不能为空。")
                return cls._search_web(query, user_intent=user_intent or None)

            if tool_name == "read_url":
                url = str(arguments.get("url", "")).strip()
                if not url.startswith(("http://", "https://")):
                    return cls._result(False, "仅支持读取以 http:// 或 https:// 开头的网址。")
                text = cls._fetch_page_text(url, timeout=10, max_chars=3000)
                if not text:
                    return cls._result(False, f"网页读取失败或内容为空：{url}")
                return cls._result(True, f"已读取网页正文（{len(text)} 字）：\n{text[:3000]}", {"url": url, "content": text[:3000]})

            if tool_name == "get_weather":
                city = str(arguments.get("city", "")).strip()
                if not city:
                    return cls._result(False, "城市名不能为空。")
                success, msg = cls._get_weather(city)
                return cls._result(success, msg)

            if tool_name == "get_forecast":
                city = str(arguments.get("city", "")).strip()
                days = int(arguments.get("days", 4))
                if not city:
                    return cls._result(False, "城市名不能为空。")
                success, msg = cls._get_forecast(city, days)
                return cls._result(success, msg)

            if tool_name == "get_route":
                origin = str(arguments.get("origin", "")).strip()
                destination = str(arguments.get("destination", "")).strip()
                mode = str(arguments.get("mode", "drive")).strip().lower() or "drive"
                if not origin or not destination:
                    return cls._result(False, "路线规划需要起点和终点。")
                success, msg = cls._get_route(origin, destination, mode)
                return cls._result(success, msg)

            if tool_name == "system_status":
                success, msg = cls._get_system_status()
                return cls._result(success, msg)

            if tool_name == "health_check":
                success, msg = cls._get_health_check()
                return cls._result(success, msg)

            if tool_name == "disk_scan":
                success, msg, data = cls._scan_disk_junk()
                return cls._result(success, msg, data)

            if tool_name == "network_info":
                return cls._result(True, cls._network_info())

            if tool_name == "speedtest":
                return cls._result(True, cls._speedtest())

            if tool_name == "ping_host":
                host = str(arguments.get("host", "")).strip() or "8.8.8.8"
                return cls._result(True, cls._ping_host(host))

            if tool_name == "recommend_food_by_city":
                city = str(arguments.get("city", "")).strip()
                keyword = str(arguments.get("keyword", "")).strip() or "美食"
                if not city:
                    return cls._result(False, "城市名不能为空。")
                success, msg = cls._search_food(city=city, keyword=keyword)
                return cls._result(success, msg)

            if tool_name == "recommend_food_by_address":
                address = str(arguments.get("address", "")).strip()
                radius = int(arguments.get("radius", 2000))
                keyword = str(arguments.get("keyword", "")).strip() or "美食"
                if not address:
                    return cls._result(False, "地址不能为空。")
                lng, lat = cls._geocode(address)
                if lng is None or lat is None:
                    return cls._result(False, f"无法解析地址“{address}”，请提供更详细的位置信息。")
                success, msg = cls._search_food(location=f"{lng},{lat}", radius=radius, keyword=keyword)
                return cls._result(success, msg)

            if tool_name == "recommend_food_by_coordinates":
                lng = arguments.get("lng")
                lat = arguments.get("lat")
                radius = int(arguments.get("radius", 2000))
                keyword = str(arguments.get("keyword", "")).strip() or "美食"
                if lng is None or lat is None:
                    return cls._result(False, "经纬度不能为空。")
                success, msg = cls._search_food(location=f"{lng},{lat}", radius=radius, keyword=keyword)
                return cls._result(success, msg)

            if tool_name == "lock_screen":
                success, msg = cls._lock_screen()
                return cls._result(success, msg)

            if tool_name == "shutdown":
                success, msg = cls._shutdown()
                return cls._result(success, msg)

            if tool_name == "reboot":
                success, msg = cls._reboot()
                return cls._result(success, msg)

            if tool_name == "run_command":
                command = str(arguments.get("command", "")).strip()
                if not command:
                    return cls._result(False, "命令不能为空。")
                success, msg, data = cls._run_command(command, COMMAND_TIMEOUT_SECONDS, cwd=cls._agent_workspace or None)
                return cls._result(success, msg, data)

            if tool_name == "read_file":
                path = str(arguments.get("path", "")).strip()
                if not path:
                    return cls._result(False, "文件路径不能为空。")
                path, ws_err = cls._resolve_agent_path(path)
                if ws_err:
                    return cls._result(False, ws_err)
                if os.path.basename(path).lower() == "auth.json":
                    return cls._result(False, "已阻止读取 config/auth.json（该文件包含密钥配置）。")
                if not os.path.isfile(path):
                    return cls._result(False, "文件不存在: {path}".format(path=path))
                try:
                    size = os.path.getsize(path)
                    if size > 512 * 1024:
                        return cls._result(False, "文件过大（{size} 字节），暂不支持读取超过 512KB 的文件。".format(size=size))
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    return cls._result(True, "已读取文件（{size} 字节）。".format(size=size), {"content": content, "path": path})
                except Exception as e:
                    return cls._result(False, "读取文件失败: {e}".format(e=e))

            if tool_name == "write_file":
                path = str(arguments.get("path", "")).strip()
                content = str(arguments.get("content", ""))
                if not path:
                    return cls._result(False, "文件路径不能为空。")
                path, ws_err = cls._resolve_agent_path(path)
                if ws_err:
                    return cls._result(False, ws_err)
                lower_path = os.path.abspath(path).lower()
                for blocked_area in ActionHandler.BLOCKED_WRITE_PATHS:
                    if lower_path.startswith(blocked_area):
                        return cls._result(False, f"已阻止写入受保护路径：{path}（{blocked_area}）。")
                if "\\.git\\" in lower_path or lower_path.endswith("\\.git"):
                    return cls._result(False, "已阻止写入 .git 目录（避免破坏版本库）。")
                if lower_path.endswith("auth.json"):
                    return cls._result(False, "已阻止覆盖 config/auth.json（该文件包含密钥配置）。")
                try:
                    parent = os.path.dirname(os.path.abspath(path))
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                    return cls._result(True, "已写入文件: {path}（{length} 字符）。".format(path=path, length=len(content)))
                except Exception as e:
                    return cls._result(False, "写入文件失败: {e}".format(e=e))

            if tool_name == "list_dir":
                path = str(arguments.get("path", "")).strip() or "."
                path, ws_err = cls._resolve_agent_path(path)
                if ws_err:
                    return cls._result(False, ws_err)
                if not os.path.isdir(path):
                    return cls._result(False, "目录不存在: {path}".format(path=path))
                try:
                    import time
                    entries = []
                    for name in sorted(os.listdir(path))[:200]:
                        full = os.path.join(path, name)
                        is_dir = os.path.isdir(full)
                        try:
                            size = "" if is_dir else os.path.getsize(full)
                            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(full)))
                        except OSError:
                            size, mtime = "", ""
                        entries.append({"name": name, "type": "dir" if is_dir else "file", "size": size, "modified": mtime})
                    return cls._result(True, "目录共 {n} 项（最多显示 200 项）。".format(n=len(entries)), {"entries": entries, "path": path})
                except Exception as e:
                    return cls._result(False, "读取目录失败: {e}".format(e=e))
        except Exception as e:
            return cls._result(False, f"执行工具 {tool_name} 失败: {str(e)}")

        return cls._result(False, f"未知工具: {tool_name}")

#======================================================静态函数=========================================================
        
    @staticmethod
    def _lock_screen():
        """锁屏 (Windows)"""
        if sys.platform == "win32":
            os.system("rundll32.exe user32.dll,LockWorkStation")
            return True, "已锁屏"
        return False, "当前系统不支持锁屏"

    @staticmethod
    def _shutdown():
        """关机，延时10秒，用户可取消"""
        if sys.platform == "win32":
            os.system("shutdown /s /t 10")
            return True, "系统将关机"
        return False, "当前系统不支持关机"

    @staticmethod
    def _reboot():
        """重启，延时10秒，用户可取消"""
        if sys.platform == "win32":
            os.system("shutdown /r /t 10")
            return True, "系统将重启"
        return False, "当前系统不支持重启"

    @staticmethod
    def _get_current_datetime():
        now = datetime.now().astimezone()
        weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday_cn = weekday_names[now.weekday()]
        timezone_name = now.tzname() or "本地时区"
        offset = now.strftime("%z")
        offset_text = f"{offset[:3]}:{offset[3:]}" if offset and len(offset) == 5 else offset
        data = {
            "iso_datetime": now.isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "year": now.year,
            "month": now.month,
            "day": now.day,
            "weekday": weekday_cn,
            "weekday_index": now.weekday(),
            "timezone": timezone_name,
            "utc_offset": offset_text or ""
        }
        message = (
            f"当前本地时间是{now.year}年{now.month}月{now.day}日，"
            f"{weekday_cn}，{now.strftime('%H:%M:%S')}，时区 {timezone_name}"
        )
        if offset_text:
            message += f"（UTC{offset_text}）"
        message += "。"
        return True, message, data
    
    @staticmethod
    def _get_weather(city):
        import requests
        api_key = ActionHandler._get_amap_api_key()
        if not api_key:
            return False, "未配置高德 API Key，请在 config/auth.json 中填写 amap_api_key。"
        # 第一步：地理编码，将城市名转换为城市代码
        city_code = ActionHandler._city_to_adcode(city, api_key)
        if not city_code:
            return False, f"未找到城市“{city}”，请检查名称"
        # 第二步：查询实时天气
        try:
            weather_url = f"https://restapi.amap.com/v3/weather/weatherInfo?city={city_code}&key={api_key}"
            weather_resp = requests.get(weather_url, timeout=5)
            weather_data = weather_resp.json()
            if weather_data.get("status") == "1" and weather_data.get("lives"):
                live = weather_data["lives"][0]
                info = (f"{live['city']}天气：{live['weather']}，"
                        f"气温 {live['temperature']}℃，"
                        f"风向 {live['winddirection']}，风力 {live['windpower']}级，"
                        f"湿度 {live['humidity']}%")
                return True, info
            else:
                return False, f"获取天气失败：{weather_data.get('info', '未知错误')}"
        except Exception as e:
            return False, f"查询天气出错：{str(e)}"
    
    @staticmethod
    def _get_forecast(city, days=4):
        import requests
        api_key = ActionHandler._get_amap_api_key()
        if not api_key:
            return False, "未配置高德 API Key，请在 config/auth.json 中填写 amap_api_key。"
        # 第一步：地理编码，获取城市 adcode
        city_code = ActionHandler._city_to_adcode(city, api_key)
        if not city_code:
            return False, f"未找到城市“{city}”，请检查名称"
        # 第二步：查询天气预报（extensions=all）
        try:
            weather_url = f"https://restapi.amap.com/v3/weather/weatherInfo?city={city_code}&key={api_key}&extensions=all"
            weather_resp = requests.get(weather_url, timeout=5)
            weather_data = weather_resp.json()
            if weather_data.get("status") == "1" and weather_data.get("forecasts"):
                forecast = weather_data["forecasts"][0]
                city_name = forecast["city"]
                casts = forecast["casts"][:days]
                msg = f"{city_name} 未来天气预报：\n"
                for day in casts:
                    date = day["date"]
                    week = day["week"]
                    day_weather = day["dayweather"]
                    night_weather = day["nightweather"]
                    day_temp = day["daytemp"]
                    night_temp = day["nighttemp"]
                    msg += f"{date} 星期{week}：白天{day_weather} {day_temp}℃，夜晚{night_weather} {night_temp}℃\n"
                return True, msg.strip()
            else:
                return False, f"获取天气预报失败：{weather_data.get('info', '未知错误')}"
        except Exception as e:
            return False, f"查询预报出错：{str(e)}"
    

    @staticmethod
    def _geocode(address):
        """调用高德地理编码API，返回 (经度, 纬度)"""
        api_key = ActionHandler._get_amap_api_key()
        if not api_key:
            return None, None
        url = f"https://restapi.amap.com/v3/geocode/geo?address={address}&output=json&key={api_key}"
        try:
            resp = requests.get(url, timeout=5)
            data = resp.json()
            if data.get("status") == "1" and data.get("geocodes"):
                location = data["geocodes"][0]["location"]  # "经度,纬度"
                lng, lat = location.split(',')
                return float(lng), float(lat)
            else:
                return None, None
        except:
            return None, None

    @staticmethod
    def _format_distance(meters):
        meters = float(meters)  # 强制转换为数字
        if meters < 1000:
            return f"{int(meters)}米"
        else:
            return f"{meters/1000:.1f}公里"

    @staticmethod
    def _format_duration(seconds):
        seconds = int(seconds)
        minutes = seconds // 60
        secs = seconds % 60
        if secs > 0:
            return f"{minutes}分{secs}秒"
        else:
            return f"{minutes}分钟"

    @staticmethod
    def _get_route(origin, destination, mode="drive"):
        import requests
        api_key = ActionHandler._get_amap_api_key()
        if not api_key:
            return False, "未配置高德 API Key，请在 config/auth.json 中填写 amap_api_key。"
        # 地理编码
        o_lng, o_lat = ActionHandler._geocode(origin)
        d_lng, d_lat = ActionHandler._geocode(destination)
        if o_lng is None or d_lng is None:
            return False, f"无法解析地名“{origin if o_lng is None else destination}”，请使用更具体的地点名称。"
        
        # 根据方式调用不同API
        if mode == "drive":
            url = f"https://restapi.amap.com/v3/direction/driving?origin={o_lng},{o_lat}&destination={d_lng},{d_lat}&key={api_key}&extensions=all"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if data.get("status") != "1":
                return False, f"驾车路线规划失败：{data.get('info', '未知错误')}"
            path = data["route"]["paths"][0]
            distance = int(path["distance"])
            duration = int(path["duration"])
            tolls = path.get("toll_distance", "0")
            traffic_lights = path.get("traffic_lights", "未知")
            steps = path.get("steps", [])
            road_names = [step.get("road", "") for step in steps[:5] if step.get("road")]
            roads_text = "、".join(road_names) if road_names else "主要道路"
            reply = (f"从{origin}到{destination}，驾车{ActionHandler._format_distance(distance)}，"
                    f"预计{ActionHandler._format_duration(duration)}，途经{roads_text}，"
                    f"红绿灯约{traffic_lights}个。")
            if int(tolls) > 0:
                reply += f" 收费路段{ActionHandler._format_distance(tolls)}。"
            return True, reply
        
        elif mode == "walk":
            url = f"https://restapi.amap.com/v3/direction/walking?origin={o_lng},{o_lat}&destination={d_lng},{d_lat}&key={api_key}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if data.get("status") != "1" or not data.get("route", {}).get("paths"):
                return False, f"步行路线规划失败：{data.get('info', '未知错误')}"
            path = data["route"]["paths"][0]
            distance = int(path["distance"])
            duration = int(path["duration"])
            steps = path.get("steps", [])
            instructions = [step["instruction"] for step in steps[:3]]
            reply = (f"从{origin}步行到{destination}，{ActionHandler._format_distance(distance)}，"
                    f"约{ActionHandler._format_duration(duration)}。")
            if instructions:
                reply += f" 路线建议：{'；'.join(instructions)}。"
            return True, reply
        
        elif mode == "bus":
            url = f"https://restapi.amap.com/v3/direction/transit/integrated?origin={o_lng},{o_lat}&destination={d_lng},{d_lat}&key={api_key}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if data.get("status") != "1" or not data.get("route", {}).get("transits"):
                return False, f"公交路线规划失败：{data.get('info', '未知错误')}"
            transit = data["route"]["transits"][0]
            # 处理总耗时和总花费
            duration = int(transit.get("duration", 0))
            cost_raw = transit.get("cost", 0)
            try:
                cost = float(cost_raw)
            except (ValueError, TypeError):
                cost = 0.0

            steps_desc = []
            step_num = 1
            for seg in transit.get("segments", []):
                # 步行段
                if "walking" in seg and seg["walking"]:
                    walk = seg["walking"]
                    walk_distance = walk.get("distance", 0)
                    # 转换为数值
                    if isinstance(walk_distance, str):
                        walk_distance = int(walk_distance) if walk_distance.isdigit() else 0
                    else:
                        walk_distance = int(walk_distance)
                    if walk_distance > 0:
                        steps_desc.append(f"{step_num}. 步行 {ActionHandler._format_distance(walk_distance)}")
                        step_num += 1
                # 公交/地铁段
                if "bus" in seg and seg["bus"]:
                    bus_obj = seg["bus"]
                    buslines = bus_obj.get("buslines", [])
                    for bl in buslines:
                        name = bl.get("name", "未知线路")
                        start_stop = bl.get("start_stop", "上车点")
                        end_stop = bl.get("end_stop", "下车点")
                        via_num = bl.get("via_num", 0)
                        if isinstance(via_num, str):
                            via_num = int(via_num) if via_num.isdigit() else 0
                        else:
                            via_num = int(via_num)
                        line_type = bl.get("type", "公交")
                        direction = bl.get("direction", "")
                        dir_text = f"（开往 {direction}）" if direction else ""
                        steps_desc.append(f"{step_num}. 乘坐 {line_type}{name}{dir_text}，从 {start_stop} 上车，经过 {via_num} 站，到 {end_stop} 下车")
                        step_num += 1

            # 总步行距离
            total_walk = transit.get("walking_distance", 0)
            if isinstance(total_walk, list):
                total_walk = int(total_walk[0]) if total_walk else 0
            elif isinstance(total_walk, str):
                total_walk = int(total_walk) if total_walk.isdigit() else 0
            else:
                total_walk = int(total_walk) if total_walk else 0

            reply = f"从{origin}到{destination}，推荐公共交通方案（约{ActionHandler._format_duration(duration)}，总步行{ActionHandler._format_distance(total_walk)}，票价约{cost/100:.1f}元）：\n"
            reply += "\n".join(steps_desc)
            return True, reply
        
        elif mode == "bike":
            # 骑行（高德不支持骑行规划？可使用步行代替或提示）
            # 高德目前没有公开的骑行API，可以用步行代替并提示。
            return False, "抱歉，暂不支持骑行路线规划，您可以使用步行或驾车方案。"
        else:
            return False, f"不支持的出行方式: {mode}"
        

    @staticmethod
    def _get_gpu_status():
        """返回 GPU 状态字符串（供合并使用）"""
        if not NVML_AVAILABLE:
            return "未安装 nvidia-ml-py3，无法获取 GPU 信息。"
        try:
            # pynvml 部分版本只认 NVSMI 目录，而驱动把 nvml.dll 装在 System32，需要手动加载
            if sys.platform == "win32":
                system_nvml = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll")
                if os.path.exists(system_nvml):
                    import ctypes
                    import pynvml as _pynvml
                    if getattr(_pynvml, "nvmlLib", None) is None:
                        _pynvml.nvmlLib = ctypes.CDLL(system_nvml)
            nvmlInit()
            device_count = nvmlDeviceGetCount()
            if device_count == 0:
                return "未检测到 NVIDIA GPU。"
            info_lines = []
            for i in range(device_count):
                handle = nvmlDeviceGetHandleByIndex(i)
                name_raw = nvmlDeviceGetName(handle)
                # 兼容 bytes 和 str 类型
                if isinstance(name_raw, bytes):
                    name = name_raw.decode('utf-8')
                else:
                    name = str(name_raw)
                util = nvmlDeviceGetUtilizationRates(handle)
                mem_info = nvmlDeviceGetMemoryInfo(handle)
                try:
                    temp = nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU)
                    temp_str = f"，温度 {temp}°C"
                except:
                    temp_str = ""
                line = (f"{name}：使用率 {util.gpu}%{temp_str}，"
                        f"显存 {mem_info.used // (1024**2)} MB / {mem_info.total // (1024**2)} MB "
                        f"({(mem_info.used/mem_info.total)*100:.1f}%)")
                info_lines.append(line)
            nvmlShutdown()
            return "\n".join(info_lines)
        except Exception:
            return "未检测到 NVIDIA GPU 或驱动异常。"

    @staticmethod
    def _get_system_status():
        """返回包含 CPU、内存、磁盘、GPU 的完整性能信息"""
        try:
            # CPU 型号
            cpu_model = cpuinfo.get_cpu_info().get('brand_raw', '未知型号')
            # 或者使用 platform.processor()，但可能不完整
            cpu_percent = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            info = (
                f"{cpu_model}: 使用率{cpu_percent}%\n"
                f"内存使用: {mem.used // (1024**3)} GB / {mem.total // (1024**3)} GB ({mem.percent}%)\n"
                f"磁盘使用: {disk.used // (1024**3)} GB / {disk.total // (1024**3)} GB ({disk.percent}%)"
            )
            # 附加 GPU 信息
            gpu_info = ActionHandler._get_gpu_status()
            if gpu_info and not gpu_info.startswith("未安装") and not gpu_info.startswith("未检测到"):
                info += "\n" + gpu_info
            elif gpu_info:
                info += f"\n[提示] {gpu_info}"
            return True, info
        except Exception as e:
            return False, f"获取系统状态失败: {str(e)}"

    @staticmethod
    def _get_startup_items():
        """读取 Windows 开机启动项（注册表 Run 键 + 启动文件夹），只读。"""
        items = []
        if sys.platform != "win32":
            return items
        try:
            import winreg
        except ImportError:
            return items
        run_keys = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
        ]
        for hive, key_path in run_keys:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    index = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, index)
                            items.append((name, str(value)))
                            index += 1
                        except OSError:
                            break
            except OSError:
                continue
        startup_dirs = []
        appdata = os.environ.get("APPDATA", "")
        program_data = os.environ.get("PROGRAMDATA", "")
        if appdata:
            startup_dirs.append(os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup"))
        if program_data:
            startup_dirs.append(os.path.join(program_data, r"Microsoft\Windows\Start Menu\Programs\Startup"))
        for startup_dir in startup_dirs:
            try:
                for fname in os.listdir(startup_dir):
                    full = os.path.join(startup_dir, fname)
                    if os.path.isfile(full):
                        items.append((fname, full))
            except OSError:
                continue
        seen = set()
        unique = []
        for name, cmd in items:
            if cmd not in seen:
                seen.add(cmd)
                unique.append((name, cmd))
        return unique

    @staticmethod
    def _get_health_check():
        """电脑体检：汇总 CPU/内存/磁盘/GPU/网络/开机启动项/电量，返回可读报告。"""
        lines = []
        try:
            cpu_model = cpuinfo.get_cpu_info().get('brand_raw', '未知型号')
            cpu_percent = psutil.cpu_percent(interval=0.5)
            lines.append(f"CPU：{cpu_model}")
            lines.append(f"CPU使用率：{cpu_percent}%")
        except Exception as e:
            lines.append(f"CPU信息获取失败：{e}")
        try:
            mem = psutil.virtual_memory()
            lines.append(f"内存：已用 {mem.used // (1024**3)} GB / 共 {mem.total // (1024**3)} GB（{mem.percent}%）")
        except Exception as e:
            lines.append(f"内存信息获取失败：{e}")
        try:
            disk_parts = []
            for part in psutil.disk_partitions():
                if not part.fstype:
                    continue
                if "cdrom" in (part.opts or "").lower():
                    continue
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disk_parts.append(f"{part.device}（{part.mountpoint}）：已用 {usage.used // (1024**3)} GB / 共 {usage.total // (1024**3)} GB（{usage.percent}%）")
                except (PermissionError, OSError):
                    continue
            if disk_parts:
                lines.append("磁盘：" + "；".join(disk_parts))
        except Exception as e:
            lines.append(f"磁盘信息获取失败：{e}")
        gpu_info = ActionHandler._get_gpu_status()
        if gpu_info and not gpu_info.startswith("未安装") and not gpu_info.startswith("未检测到"):
            lines.append("显卡：" + gpu_info)
        elif gpu_info:
            lines.append(f"[提示] {gpu_info}")
        try:
            local_ip = ActionHandler._get_local_ip()
            lines.append(f"内网IP：{local_ip}")
        except Exception:
            pass
        try:
            import socket as net_socket
            probe = net_socket.create_connection(("223.5.5.5", 53), timeout=2)
            probe.close()
            lines.append("网络：已连接互联网")
        except Exception:
            lines.append("网络：未检测到互联网连接")
        startup_items = ActionHandler._get_startup_items()
        lines.append(f"开机启动项：{len(startup_items)} 项")
        for name, cmd in startup_items[:10]:
            lines.append(f"  - {name}：{cmd[:80]}")
        if len(startup_items) > 10:
            lines.append(f"  ……（共 {len(startup_items)} 项）")
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                plugged = "充电中" if battery.power_plugged else "未充电"
                lines.append(f"电量：{battery.percent}%（{plugged}）")
        except Exception:
            pass
        try:
            boot_time = datetime.fromtimestamp(psutil.boot_time())
            lines.append(f"开机时间：{boot_time.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception:
            pass
        return True, "\n".join(lines)

    @staticmethod
    def _scan_disk_junk():
        """只读扫描常见可清理位置，不删除任何文件。返回 (成功, 消息, 数据)。"""
        import time

        def walk_size(path, max_files=50000, time_limit=6.0):
            total = 0
            count = 0
            truncated = False
            start = time.monotonic()
            try:
                for root, _dirs, files in os.walk(path):
                    if time.monotonic() - start > time_limit:
                        truncated = True
                        break
                    for fname in files:
                        count += 1
                        if count > max_files:
                            truncated = True
                            return total, count, truncated
                        try:
                            total += os.path.getsize(os.path.join(root, fname))
                        except OSError:
                            pass
            except OSError:
                pass
            return total, count, truncated

        def fmt_size(size):
            if size >= 1024**3:
                return f"{size / 1024**3:.2f} GB"
            if size >= 1024**2:
                return f"{size / 1024**2:.1f} MB"
            if size >= 1024:
                return f"{size / 1024:.0f} KB"
            return f"{size} B"

        categories = []
        env = os.environ
        local_appdata = env.get("LOCALAPPDATA", "")
        targets = [
            ("用户临时文件", env.get("TEMP", "")),
            ("系统临时目录", r"C:\Windows\Temp"),
            ("Windows更新缓存", r"C:\Windows\SoftwareDistribution\Download"),
            ("浏览器缓存-Chrome", os.path.join(local_appdata, r"Google\Chrome\User Data\Default\Cache") if local_appdata else ""),
            ("浏览器缓存-Edge", os.path.join(local_appdata, r"Microsoft\Edge\User Data\Default\Cache") if local_appdata else ""),
            ("缩略图缓存", os.path.join(local_appdata, r"Microsoft\Windows\Explorer") if local_appdata else ""),
            ("回收站", r"C:\$Recycle.Bin"),
        ]
        for name, path in targets:
            if not path or not os.path.isdir(path):
                continue
            total, count, truncated = walk_size(path)
            if total > 0 or count > 0:
                categories.append({"category": name, "path": path, "size": total, "count": count, "truncated": truncated})

        large_files = []
        user_home = env.get("USERPROFILE", "")
        scan_dirs = []
        for sub in ("Downloads", "Desktop", "Documents", "Videos", "Pictures"):
            if user_home:
                scan_dirs.append(os.path.join(user_home, sub))
        start_all = time.monotonic()
        for base_dir in scan_dirs:
            if time.monotonic() - start_all > 15.0 or not os.path.isdir(base_dir):
                continue
            try:
                for root, dirs, files in os.walk(base_dir):
                    dirs[:] = [d for d in dirs if not d.startswith(".")][:20]
                    if time.monotonic() - start_all > 15.0:
                        break
                    for fname in files:
                        try:
                            fpath = os.path.join(root, fname)
                            fsize = os.path.getsize(fpath)
                            if fsize >= 300 * 1024 * 1024:
                                large_files.append({"path": fpath, "size": fsize})
                        except OSError:
                            pass
            except OSError:
                continue
        large_files.sort(key=lambda x: x["size"], reverse=True)
        large_files = large_files[:10]

        report = ["磁盘扫描结果（只读，未删除任何文件）："]
        if not categories and not large_files:
            report.append("未发现明显的可清理内容。")
        for cat in categories:
            suffix = "（扫描超时，仅部分统计）" if cat["truncated"] else ""
            report.append(f"{cat['category']}：{fmt_size(cat['size'])}，{cat['count']} 个文件{suffix}")
            report.append(f"  位置：{cat['path']}")
        if large_files:
            report.append("大文件（≥300MB）：")
            for item in large_files:
                report.append(f"  - {item['path']}（{fmt_size(item['size'])}）")
        report.append("建议：临时文件、浏览器缓存、回收站属于低风险清理项；删除前请先与用户确认。")
        data = {
            "categories": categories,
            "large_files": large_files,
        }
        return True, "\n".join(report), data
        
    @staticmethod
    def _get_local_ip():
        """获取本机内网 IPv4 地址"""
        try:
            # 方法：连接一个外部地址（不会真正发送数据）来获取本机IP
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            return ip
        except Exception:
            return "127.0.0.1"

    @staticmethod
    def _get_public_ip():
        """通过外部服务获取公网 IP（需要联网）"""
        try:
            import requests
            # 多个备选 API，提高成功率
            apis = [
                "https://api.ipify.org",
                "https://icanhazip.com",
                "https://httpbin.org/ip"
            ]
            for api in apis:
                try:
                    resp = requests.get(api, timeout=5)
                    if api.endswith("ipify.org") or api.endswith("icanhazip.com"):
                        ip = resp.text.strip()
                    elif api.endswith("httpbin.org/ip"):
                        ip = resp.json().get("origin", "")
                    else:
                        ip = ""
                    if ip and ip != "127.0.0.1":
                        return ip
                except:
                    continue
            return "无法获取公网IP"
        except Exception:
            return "未安装 requests，公网IP功能不可用"

    @staticmethod
    def _get_mac_address():
        """获取本机 MAC 地址"""
        mac = uuid.getnode()
        # 格式化为 XX:XX:XX:XX:XX:XX
        mac_hex = ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
        return mac_hex

    @staticmethod
    def _speedtest():
        """简易网速测试（下载速度），使用多个国内备选服务器"""
        import time
        import requests
        # 国内可用的测试文件 URL（建议使用较小的文件，避免长时间等待）
        test_urls = [
            "http://speedtest.21cn.com/speedtest/speedtest1.zip",   # 21CN 测速（约 1MB）
            "http://speedtest.21cn.com/speedtest/speedtest2.zip",   # 备用
            "http://download.windowsupdate.com/windowsupdate/redist/standalone/7.4.7600.226/WindowsUpdateAgent30-x86.exe",  # 微软文件（~5MB）
        ]
        # 文件大小（字节），用于计算速度。如果不确定，可以动态估算
        # 这里我们使用动态估算：根据实际下载字节数除以耗时
        for url in test_urls:
            try:
                start = time.time()
                response = requests.get(url, stream=True, timeout=10)
                if response.status_code != 200:
                    continue
                total_bytes = 0
                for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB块
                    total_bytes += len(chunk)
                    # 最多下载 10 秒，避免耗时过长
                    if time.time() - start > 10:
                        break
                elapsed = time.time() - start
                if elapsed < 0.1:
                    continue
                speed_mbps = (total_bytes * 8) / (elapsed * 1_000_000)
                # 如果测出来结果异常低（<0.1 Mbps）或异常高，继续尝试下一个
                if speed_mbps > 0.1 and speed_mbps < 2000:  # 合理范围
                    return f"{speed_mbps:.2f} Mbps（使用 {url.split('/')[-1]}）"
            except Exception:
                continue
        return "所有测速服务器均无法连接，请检查网络或稍后重试"

    @staticmethod
    def _network_info():
        """组合网络信息：内网IP、公网IP、MAC地址"""
        local_ip = ActionHandler._get_local_ip()
        public_ip = ActionHandler._get_public_ip()
        mac = ActionHandler._get_mac_address()
        return (
            f"内网 IPv4: {local_ip}\n"
            f"公网 IPv4: {public_ip}\n"
            f"MAC 地址: {mac}"
        )

    @staticmethod
    def _ping_host(host="8.8.8.8"):
        """Ping 一个主机，返回丢包率和平均延迟（Windows 下使用 subprocess）"""
        import subprocess
        import re
        try:
            result = subprocess.run(
                ["ping", "-n", "4", host],
                capture_output=True,
                text=True,
                timeout=10
            )
            output = result.stdout
            # 提取统计信息：丢失率、平均时间
            lost_match = re.search(r"丢失 = (\d+)", output)
            avg_match = re.search(r"平均 = (\d+)ms", output)
            lost = lost_match.group(1) if lost_match else "?"
            avg = avg_match.group(1) if avg_match else "?"
            return f"Ping {host}: 丢包 {lost}%，平均延迟 {avg} ms"
        except Exception as e:
            return f"Ping 失败: {str(e)}"
        
    @staticmethod
    def _search_food(city=None, keyword="美食", limit=5, location=None, radius=2000):
        """
        搜索美食餐厅（支持城市级或周边搜索）
        :param city: 城市名（可选，如果传了 location 可以不传城市，但最好仍传城市作为 fallback）
        :param keyword: 搜索关键词
        :param limit: 数量
        :param location: 中心点坐标 "经度,纬度"，例如 "116.397128,39.916527"
        :param radius: 搜索半径（米），默认 2000
        :return: (bool, str)
        """
        import requests
        api_key = ActionHandler._get_amap_api_key()
        if not api_key:
            return False, "未配置高德 API Key，请在 config/auth.json 中填写 amap_api_key。"
        adcode = None

        # 1. 如果有 location，优先使用周边搜索，不需要城市编码
        if location:
            search_url = (
                f"https://restapi.amap.com/v3/place/around?"
                f"keywords={keyword}&types=050000&location={location}&radius={radius}"
                f"&offset={limit}&page=1&extensions=all&sortrule=distance&key={api_key}"
            )
        else:
            # 没有 location，退化为城市级搜索
            if not city:
                return False, "请提供城市名或中心点坐标。"
            # 地理编码：城市 -> adcode
            geo_url = f"https://restapi.amap.com/v3/geocode/geo?address={city}&key={api_key}"
            try:
                resp = requests.get(geo_url, timeout=5)
                geo_data = resp.json()
                if geo_data.get("status") != "1" or not geo_data.get("geocodes"):
                    return False, f"未找到城市“{city}”"
                adcode = geo_data["geocodes"][0]["adcode"]
            except Exception as e:
                return False, f"地理编码失败: {str(e)}"
            search_url = (
                f"https://restapi.amap.com/v3/place/text?"
                f"keywords={keyword}&types=050000&city={adcode}&children=0"
                f"&offset={limit}&page=1&extensions=all&key={api_key}"
            )

        try:
            resp = requests.get(search_url, timeout=8)
            data = resp.json()
            if data.get("status") != "1":
                return False, f"搜索失败: {data.get('info', '未知错误')}"
            pois = data.get("pois", [])
            if not pois:
                if location:
                    return False, f"在您附近没找到{keyword}餐厅，请扩大范围或换个关键词。"
                else:
                    return False, f"在{city}没找到合适的{keyword}餐厅。"

            lines = ["为您找到以下餐厅："]
            for i, poi in enumerate(pois[:limit], 1):
                name = poi.get("name", "无名")
                address = poi.get("address", "地址不详")
                tel = poi.get("tel", "")
                # 如果有距离字段（周边搜索会返回）
                distance = poi.get("distance")
                dist_str = f"，距离约{int(distance)}米" if distance else ""
                line = f"{i}. {name}{dist_str}\n   地址：{address}"
                if tel:
                    line += f"\n   电话：{tel}"
                lines.append(line)
            return True, "\n".join(lines)
        except Exception as e:
            return False, f"请求美食数据失败: {str(e)}"
        
    @staticmethod
    def _search_web(query: str, max_results: int = 5, user_intent: str | None = None) -> dict[str, Any]:
        """联网搜索：Tavily Search 优先（结果自带正文），SearXNG 兜底补充，合并去重后统一打分排序。"""
        effective_query, search_profile = ActionHandler._optimize_search_query(query, user_intent=user_intent)
        sports_mode = search_profile["sports_mode"]
        latest_mode = search_profile["latest_mode"]
        fetch_count = max(max_results * 2, 8)

        tavily_results, tavily_note = ActionHandler._tavily_search(effective_query, max_results=fetch_count)

        searxng_results = []
        searxng_note = ""
        try:
            searxng_results = ActionHandler._search_searxng_results(
                effective_query,
                max_results=fetch_count,
                sports_mode=sports_mode,
                latest_mode=latest_mode
            )
        except requests.exceptions.ConnectionError:
            searxng_note = "SearXNG 服务未启动"
        except Exception as e:
            searxng_note = f"SearXNG 搜索出错：{str(e)}"

        merged = []
        seen_keys = set()
        for item in list(tavily_results) + list(searxng_results):
            url = str(item.get("url", "")).strip()
            key = url.lower() if url else str(item.get("title", "")).strip().lower()
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(item)

        if not merged:
            detail = tavily_note or searxng_note or "未返回任何结果"
            return ActionHandler._result(False, f"搜索失败：{detail}")

        results = ActionHandler._rerank_search_results(
            merged,
            effective_query,
            sports_mode=sports_mode,
            latest_mode=latest_mode
        )[:max_results]

        if not results:
            return ActionHandler._result(True, f"未找到关于「{effective_query}」的相关信息。", {
                "query": query,
                "effective_query": effective_query,
                "results": []
            })

        extract_urls = [
            item["url"] for item in results
            if item.get("engine") != "tavily" and item.get("url")
        ]
        extracted_map, extract_note, extract_provider = {}, "", ""
        if extract_urls:
            extracted_map, extract_note = ActionHandler._tavily_extract(extract_urls[:3], effective_query)
            if extracted_map:
                extract_provider = "Tavily"
            else:
                direct_map, direct_note = ActionHandler._direct_fetch_pages(extract_urls[:3])
                if direct_map:
                    extracted_map = direct_map
                    extract_note = direct_note
                    extract_provider = "direct_fetch"

        normalized_results = []
        for i, item in enumerate(results, 1):
            summary = item.get("content", "") or "无摘要"
            source = "searxng_snippet"
            if item.get("engine") == "tavily":
                source = "tavily_search"
            else:
                extracted_text = extracted_map.get(item.get("url", ""), "")
                if extracted_text:
                    summary = extracted_text
                    source = "tavily_extract" if extract_provider == "Tavily" else "direct_fetch"

            summary = summary.strip()
            if len(summary) > 900:
                summary = summary[:900] + "..."

            normalized_results.append({
                "rank": i,
                "title": item.get("title", "无标题"),
                "url": item.get("url", ""),
                "summary": summary,
                "engine": item.get("engine", ""),
                "source": source
            })

        providers = []
        if tavily_results:
            providers.append("Tavily")
        if searxng_results:
            providers.append("SearXNG")
        search_provider = "+".join(providers) if providers else "None"
        notes = [n for n in (tavily_note, searxng_note, extract_note) if n]
        note = "；".join(notes)

        message = f"已完成联网检索，共整理 {len(normalized_results)} 条结果。"
        data = {
            "query": query,
            "effective_query": effective_query,
            "user_intent": user_intent or "",
            "search_provider": search_provider,
            "extract_provider": extract_provider,
            "note": note,
            "search_profile": search_profile,
            "results": normalized_results
        }
        return ActionHandler._result(True, message, data)

    @staticmethod
    def _search_searxng_results(
        query: str,
        max_results: int = 5,
        sports_mode: bool = False,
        latest_mode: bool = False
    ) -> list[dict[str, Any]]:
        base_url = ActionHandler._search_config.get("searxng_url", "http://localhost:18080").rstrip("/")

        def fetch(categories: str):
            cjk_count = len(re.findall(r"[\u4e00-\u9fff]", query or ""))
            latin_words = len(re.findall(r"[A-Za-z]+", query or ""))
            language = "all" if (cjk_count and latin_words >= 3) else ("zh" if cjk_count else "en")
            params = {
                "q": query,
                "format": "json",
                "language": language,
                "categories": categories,
                "safesearch": "1"
            }
            if latest_mode:
                params["time_range"] = "month"
            resp = requests.get(f"{base_url}/search", params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()

        primary_category = "news" if latest_mode or sports_mode else "general"
        data = fetch(primary_category)
        if not data.get("results") and primary_category != "general":
            data = fetch("general")

        results = []
        for item in data.get("results", [])[:max_results]:
            results.append({
                "title": item.get("title", "无标题"),
                "content": item.get("content", "无摘要"),
                "url": item.get("url", ""),
                "engine": item.get("engine", "")
            })
        return results

    @staticmethod
    def _optimize_search_query(query: str, user_intent: str | None = None) -> tuple[str, dict[str, Any]]:
        normalized_query = " ".join(str(query or "").split())
        normalized_intent = " ".join(str(user_intent or "").split())

        base_query = normalized_query
        if normalized_intent and ActionHandler._is_sports_search(normalized_intent):
            base_query = normalized_intent

        sports_mode = ActionHandler._is_sports_search(base_query)
        latest_mode = ActionHandler._is_latest_search(base_query)

        if sports_mode:
            effective_query = ActionHandler._optimize_sports_search_query(base_query, latest_mode=latest_mode)
        else:
            effective_query = ActionHandler._normalize_latest_query_years(base_query, latest_mode=latest_mode)

        profile = {
            "sports_mode": sports_mode,
            "latest_mode": latest_mode,
            "original_query": normalized_query,
            "base_query": base_query,
            "current_date": datetime.now().strftime("%Y-%m-%d")
        }
        return effective_query, profile

    @staticmethod
    def _is_sports_search(text: str) -> bool:
        if not text:
            return False
        keywords = (
            "世界杯", "世俱杯", "预选赛", "足球", "国足", "欧冠", "英超", "西甲", "德甲",
            "意甲", "法甲", "中超", "NBA", "CBA", "网球", "F1", "奥运", "比赛", "赛况", "比分"
        )
        return any(keyword in text for keyword in keywords)

    @staticmethod
    def _is_latest_search(text: str) -> bool:
        if not text:
            return False
        keywords = (
            "最新", "实时", "今天", "今日", "当前", "刚刚", "赛况", "比分", "赛果",
            "战报", "赛程", "积分榜", "排名", "结果", "动态"
        )
        return any(keyword in text for keyword in keywords)

    @staticmethod
    def _normalize_latest_query_years(query: str, latest_mode: bool = False) -> str:
        query = re.sub(r"\s+", " ", query).strip()
        if not latest_mode:
            return query

        current_year = datetime.now().year
        years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?=年|\D|$)", query)
        unique_years = list(dict.fromkeys(years))
        if str(current_year) in unique_years and len(unique_years) > 1:
            query = re.sub(
                r"((?:19|20)\d{2})年?(?:\d{1,2}月)?",
                lambda match: match.group(0) if match.group(1) == str(current_year) else "",
                query
            )
            query = re.sub(r"\s{2,}", " ", query).strip()
        return query

    @staticmethod
    def _optimize_sports_search_query(query: str, latest_mode: bool = False) -> str:
        query = re.sub(r"[，。！？、】【（）()]", " ", query)
        query = re.sub(r"\s+", " ", query).strip()
        current_year = datetime.now().year
        current_month = datetime.now().month

        years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?=年|\D|$)", query)
        unique_years = list(dict.fromkeys(years))
        if latest_mode and str(current_year) in unique_years and len(unique_years) > 1:
            query = re.sub(
                r"((?:19|20)\d{2})年?(?:\d{1,2}月)?",
                lambda match: match.group(0) if match.group(1) == str(current_year) else "",
                query
            )
        query = re.sub(r"\s{2,}", " ", query).strip()

        if latest_mode and not re.search(r"(?<!\d)(?:19|20)\d{2}(?=年|\D|$)", query):
            query = f"{current_year}年{current_month}月 {query}"
        elif latest_mode and str(current_year) in query and not re.search(r"\d{1,2}月", query):
            query = f"{query} {current_month}月"

        if "赛况" in query and "比分" not in query:
            query += " 比分"
        if latest_mode and not any(keyword in query for keyword in ("比分", "赛果", "战报", "赛程", "积分榜", "结果")):
            query += " 最新赛况 比分 结果"

        return re.sub(r"\s{2,}", " ", query).strip()

    @staticmethod
    def _extract_query_keywords(query: str) -> list[str]:
        """从查询中提取有信息量的关键词（过滤中英文停用词），用于相关性打分与过滤。"""
        stopwords = frozenset({
            "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at", "with", "vs",
            "how", "what", "why", "which", "when", "where", "who", "is", "are", "was", "were", "be",
            "do", "does", "did", "can", "could", "should", "would", "will", "not", "no", "it", "its",
            "我", "你", "他", "她", "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "如何",
            "哪个", "哪些", "为什么", "一下", "帮我", "请", "还是", "以及", "因为", "所以", "可以",
            "没有", "知道", "介绍", "了解", "关于", "来说", "之类", "比较", "对比", "分别", "大家",
            "有没有", "是不是", "怎么用", "怎么样", "哪个好", "哪个更好", "是什么", "有什么", "什么是"
        })
        cjk_stopwords = sorted(
            (s for s in stopwords if re.search(r"[\u4e00-\u9fff]", s)),
            key=len, reverse=True
        )
        text = str(query or "")
        keywords: list[str] = []

        def add(token: str) -> None:
            token_l = token.lower()
            if len(token_l) >= 2 and token_l not in stopwords and token_l not in keywords:
                keywords.append(token_l)

        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-.]{1,}|[\u4e00-\u9fff]{2,}", text):
            if not re.search(r"[\u4e00-\u9fff]", token):
                add(token)
                continue
            parts = [token]
            for sw in cjk_stopwords:
                next_parts = []
                for part in parts:
                    next_parts.extend(sub for sub in part.split(sw) if sub)
                parts = next_parts
            for part in parts:
                if len(part) <= 2:
                    add(part)
                else:
                    for k in range(len(part) - 1):
                        add(part[k:k + 2])
        return keywords
    @staticmethod
    def _score_search_result(item: dict[str, Any], query: str, sports_mode: bool = False, latest_mode: bool = False) -> int:
        title = str(item.get("title", "")).lower()
        content = str(item.get("content", "")).lower()
        url = str(item.get("url", "")).lower()
        hostname = urlparse(url).netloc.lower()
        score = 0

        negative_domains = (
            "baike.baidu.com", "wenku.so.com", "zhidao.baidu.com", "stats.gov.cn",
            "map.worldmap.pro", "360doc.com"
        )
        if any(domain in hostname for domain in negative_domains):
            score -= 8

        if sports_mode:
            positive_domains = (
                "fifa.com", "worldcup.cctv.com", "sports.qq.com", "sports.sina.com.cn",
                "qtx.com", "espn.com", "the-afc.com", "uefa.com", "xinhuanet.com", "sports.163.com"
            )
            if any(domain in hostname for domain in positive_domains):
                score += 6

            for keyword in ("世界杯", "世俱杯", "预选赛", "赛况", "比分", "赛果", "赛程", "战报", "积分榜", "足球"):
                if keyword.lower() in title:
                    score += 2
                elif keyword.lower() in content:
                    score += 1

        if latest_mode:
            current_year = str(datetime.now().year)
            current_month = f"{datetime.now().month}月"
            for marker in (current_year, current_month, "最新", "今日", "今天", "刚刚"):
                if marker.lower() in title:
                    score += 2
                elif marker.lower() in content:
                    score += 1

        compact_query = query.lower().replace(" ", "")
        if compact_query and compact_query[:8] in (title + content).replace(" ", ""):
            score += 2

        keywords = ActionHandler._extract_query_keywords(query)
        for keyword in keywords:
            if keyword in title:
                score += 2
            elif keyword in content:
                score += 1
            if keyword in url:
                score += 1

        return score

    @staticmethod
    def _rerank_search_results(
        results: list[dict[str, Any]],
        query: str,
        sports_mode: bool = False,
        latest_mode: bool = False
    ) -> list[dict[str, Any]]:
        if not results:
            return []

        scored = []
        for item in results:
            score = ActionHandler._score_search_result(item, query, sports_mode=sports_mode, latest_mode=latest_mode)
            enriched = dict(item)
            enriched["_score"] = score
            scored.append(enriched)

        scored.sort(key=lambda item: item.get("_score", 0), reverse=True)

        # 去重：同域名最多保留 2 条，归一化标题相同只保留 1 条
        seen_hosts: dict[str, int] = {}
        seen_titles: set[str] = set()
        deduped = []
        for item in scored:
            hostname = urlparse(str(item.get("url", ""))).netloc.lower()
            title_norm = re.sub(r"\s+", "", str(item.get("title", "")).lower())
            if seen_hosts.get(hostname, 0) >= 2:
                continue
            if title_norm and title_norm in seen_titles:
                continue
            seen_hosts[hostname] = seen_hosts.get(hostname, 0) + 1
            seen_titles.add(title_norm)
            deduped.append(item)

        # 相关性过滤：能提取到关键词时，剔除一个关键词都没命中的结果
        keywords = ActionHandler._extract_query_keywords(query)
        filtered = [item for item in deduped if (not keywords) or item.get("_score", 0) >= 2]
        final_results = filtered if len(filtered) >= 3 else deduped

        for item in final_results:
            item.pop("_score", None)
        return final_results

    @staticmethod
    def _get_tavily_api_key() -> str:
        configured = ActionHandler._search_config.get("tavily_api_key", "").strip()
        if configured:
            return configured
        env_key = os.getenv("TAVILY_API_KEY", "").strip()
        if env_key:
            return env_key
        ActionHandler.load_auth_config()
        return ActionHandler._auth_config.get("tavily_api_key", "").strip()

    @staticmethod
    def _tavily_search(query: str, max_results: int = 8, search_depth: str = "advanced") -> tuple[list[dict[str, Any]], str]:
        """Tavily Search：高质量联网检索，结果自带正文片段，作为搜索主源。失败返回空列表和原因。"""
        api_key = ActionHandler._get_tavily_api_key()
        if not api_key:
            return [], "未配置 Tavily API Key，仅使用 SearXNG。"
        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            resp = requests.post("https://api.tavily.com/search", json=payload, timeout=40)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return [], f"Tavily 搜索失败：{str(e)}"
        results = []
        for item in data.get("results", []):
            results.append({
                "title": item.get("title", "无标题"),
                "content": item.get("content", "") or "无摘要",
                "url": item.get("url", ""),
                "engine": "tavily",
            })
        if not results:
            return [], "Tavily 未返回结果，仅使用 SearXNG。"
        return results, ""

    @staticmethod
    def _tavily_extract(urls: list[str], query: str) -> tuple[dict[str, str], str]:
        api_key = ActionHandler._get_tavily_api_key()
        if not api_key:
            return {}, "未配置 Tavily API Key，已回退为 SearXNG 摘要。"
        if not urls:
            return {}, "搜索结果中没有可抓取的网址。"

        payload = {
            "urls": urls,
            "query": query,
            "api_key": api_key,
            "extract_depth": "basic",
            "include_images": False
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        try:
            resp = requests.post("https://api.tavily.com/extract", headers=headers, json=payload, timeout=40)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return {}, f"Tavily 抽取失败，已回退为 SearXNG 摘要：{str(e)}"

        extracted_map = {}
        for item in data.get("results", []):
            url = item.get("url", "")
            text = item.get("raw_content") or item.get("content") or ""
            if not text:
                continue
            compact = " ".join(str(text).split())
            extracted_map[url] = compact[:1200]

        if not extracted_map:
            return {}, "Tavily 未返回可用正文，已回退为 SearXNG 摘要。"
        return extracted_map, "已结合 Tavily 正文抽取增强搜索结果。"

    @staticmethod
    def _fetch_page_text(url: str, timeout: int = 10, max_chars: int = 3000) -> str:
        """直接抓取网页并提取可读正文；失败或内容为空返回空字符串。"""
        try:
            from bs4 import BeautifulSoup
            headers = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0 Safari/537.36")
            }
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if not resp.encoding or resp.encoding.lower() in {"iso-8859-1", "ascii"}:
                resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "iframe", "svg"]):
                tag.decompose()

            # 1) 优先：文章/主内容区的段落、标题、表格单元格、引用
            container = soup.find("article") or soup.find("main") or soup.body or soup
            lines = []
            for node in container.find_all(["p", "h1", "h2", "h3", "h4", "li", "td", "blockquote"]):
                text = node.get_text(" ", strip=True)
                if text:
                    lines.append(text)
            compact = " ".join("\n".join(lines).split())

            # 2) 兜底：整页可见文本（已去除脚本/导航/页脚等）
            if not compact:
                compact = " ".join(soup.get_text(" ", strip=True).split())

            # 3) 再兜底：meta 描述
            if not compact:
                meta = (soup.find("meta", attrs={"name": "description"})
                        or soup.find("meta", attrs={"property": "og:description"}))
                if meta and meta.get("content"):
                    compact = " ".join(str(meta["content"]).split())

            return compact[:max_chars]
        except Exception:
            return ""

    @staticmethod
    def _direct_fetch_pages(urls: list[str], max_chars: int = 1200) -> tuple[dict[str, str], str]:
        """Tavily 不可用时的免费兜底：直接抓取前几条链接正文。"""
        if not urls:
            return {}, "没有可抓取的网址。"
        extracted_map = {}
        for url in urls[:3]:
            text = ActionHandler._fetch_page_text(url, timeout=10, max_chars=max_chars)
            if text:
                extracted_map[url] = text
        if not extracted_map:
            return {}, "直接抓取网页正文失败，已回退为搜索摘要。"
        return extracted_map, "已通过直接抓取网页正文增强搜索结果。"
