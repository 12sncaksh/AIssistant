# action_control.py
import os
import sys
import logging
from typing import Any

from mcp_client import MCPManager  # 外部 MCP（Model Context Protocol）工具，配置见 mcp.json

try:
    from pynvml import *
    NVML_AVAILABLE = True
except ImportError:
    NVML_AVAILABLE = False


# ---- ActionHandler method groups moved into their own modules ----
from tool_schemas import (  # noqa: F401
    _builtin_tool_definitions,
)
from tool_config import (  # noqa: F401
    load_apps,
    load_auth_config,
    configure,
    get_available_apps,
    _get_amap_api_key,
)
from tools_live2d import (  # noqa: F401
    set_pet_command_callback,
    set_pet_param_catalog,
    _handle_control_pet,
    _handle_set_live2d_param,
    _handle_list_live2d_params,
)
from tools_datetime import (  # noqa: F401
    _get_current_datetime,
)
from tools_weather import (  # noqa: F401
    _city_to_adcode,
    _get_weather,
    _get_forecast,
    _geocode,
    _format_distance,
    _format_duration,
    _get_route,
)
from tools_system import (  # noqa: F401
    _lock_screen,
    _shutdown,
    _reboot,
    _get_gpu_status,
    _get_system_status,
    _get_startup_items,
    _get_health_check,
    _scan_disk_junk,
)
from tools_network import (  # noqa: F401
    _get_local_ip,
    _get_public_ip,
    _get_mac_address,
    _speedtest,
    _network_info,
    _ping_host,
)
from tools_food import (  # noqa: F401
    _search_food,
)
from tools_search import (  # noqa: F401
    _search_web,
    _search_searxng_results,
    _optimize_search_query,
    _is_sports_search,
    _is_latest_search,
    _normalize_latest_query_years,
    _optimize_sports_search_query,
    _extract_query_keywords,
    _score_search_result,
    _rerank_search_results,
)
from tools_web_fetch import (  # noqa: F401
    _get_tavily_api_key,
    _tavily_search,
    _tavily_extract,
    _fetch_page_text,
    _direct_fetch_pages,
)
from tool_exec import (  # noqa: F401
    set_agent_workspace,
    set_timeout_confirm_callback,
    _confirm_continue_waiting,
    _run_command,
)
from tool_exec import COMMAND_TIMEOUT_SECONDS  # noqa: F401
import tool_permissions as permissions
from tool_dispatch import TOOL_DISPATCH  # noqa: F401

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
    _pet_command_callback = None      # 桌宠 Live2D 控制回调（由 GUI 主线程注册，线程安全队列转发）
    _pet_param_catalog = []           # 当前桌宠模型的 Live2D 参数表 [{id,name,group}]，供大模型查询
    _sandbox_mode = "workspace-write"  # 权限预设对应的沙箱模式（read-only / workspace-write / danger-full-access）

    # 权限策略的唯一实现在 tool_permissions 里（纯函数）；这里保留同名类属性，
    # 让 cls.SANDBOX_MODES / cls.READ_ONLY_BLOCKED_TOOLS 等既有引用继续可用。
    SANDBOX_MODES = permissions.SANDBOX_MODES
    READ_ONLY_BLOCKED_TOOLS = permissions.READ_ONLY_BLOCKED_TOOLS
    READ_ONLY_REJECT_MESSAGE = permissions.READ_ONLY_REJECT_MESSAGE

    PET_EMOTION_NAMES = (
        "happy", "sad", "angry", "surprised", "shy", "wink", "cry", "sleepy",
        "love", "sparkle", "magic", "heart", "excited", "confused", "nervous",
        "tongue", "neutral",
    )



    @classmethod
    def set_sandbox_mode(cls, mode):
        """设置沙箱模式（对应权限预设）：read-only / workspace-write / danger-full-access。"""
        cls._sandbox_mode = permissions.normalize_mode(mode)
        return cls._sandbox_mode

    @classmethod
    def get_sandbox_mode(cls):
        return cls._sandbox_mode

    @classmethod
    def is_full_access(cls):
        """完全权限模式：放开沙箱边界（工作区外的读写范围、子agent的工作区路径限制）。

        策略实现在 tool_permissions.is_full_access；破坏性命令黑名单与受保护写入路径
        不随权限模式放开。
        """
        return permissions.is_full_access(cls._sandbox_mode)

    @classmethod
    def is_read_only(cls):
        return permissions.is_read_only(cls._sandbox_mode)





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




    # 已迁到本地 MCP server 的工具：本程序自己的工具表里不再重复提供，
    # 由 mcp_servers/aissistant_*.py 通过 MCP 暴露（本地直连和 DSH 桥接都走 MCP）。
    # search_memory 是唯一一个"数据在库里、但每个进程都要能读"的工具：桥接把它交给
    # DSH 之后，调用发生在 MCP 子进程里；靠 memory_meta 里落库的当前会话找对会话。
    MCP_MIGRATED_TOOLS = frozenset(('system_status', 'health_check', 'disk_scan', 'network_info', 'speedtest', 'ping_host', 'get_weather', 'get_forecast', 'get_route', 'recommend_food_by_address', 'recommend_food_by_city', 'recommend_food_by_coordinates', 'open_app', 'open_url', 'lock_screen', 'shutdown', 'reboot', 'control_pet', 'set_live2d_param', 'list_live2d_params', 'search_memory'))


    @classmethod
    def get_mcp_tool_definitions(cls) -> list[dict[str, Any]]:
        """只返回已迁到 MCP 的那部分工具定义，供 mcp_servers/ 下的 server 复用。

        刻意不碰 MCPManager：本方法必须无任何启动副作用，否则 MCP server
        自己调它会递归拉起一套 MCP server。
        """
        return [
            tool for tool in cls._builtin_tool_definitions()
            if tool.get("function", {}).get("name") in cls.MCP_MIGRATED_TOOLS
        ]

    @classmethod
    def _mcp_covered_tools(cls, mcp_tools: list) -> set:
        """mcp_tools 里实际提供了哪些「已迁移」工具（工具名形如 mcp__<server>__<tool>）。"""
        covered = set()
        for tool in mcp_tools:
            name = str(tool.get("function", {}).get("name") or "")
            if not name.startswith("mcp__"):
                continue
            suffix = name.rsplit("__", 1)[-1]
            if suffix in cls.MCP_MIGRATED_TOOLS:
                covered.add(suffix)
        return covered

    @classmethod
    def get_tool_definitions(cls) -> list[dict[str, Any]]:
        """本程序自己的工具表：让位给已就绪的 MCP 工具，再合并 MCP server 的工具。

        「让位」必须是有条件的：免安装版（PyInstaller）里既没有 Python 解释器也没有
        mcp_servers/，自带 server 起不来；那时若无条件剔除，这些工具会静默消失。
        所以只在 MCP 侧确实报出同名工具时才摘掉内置定义。
        """
        mcp_tools = MCPManager.get_tool_definitions()
        covered = cls._mcp_covered_tools(mcp_tools)
        tools = [
            tool for tool in cls._builtin_tool_definitions()
            if tool.get("function", {}).get("name") not in covered
        ]
        # 合并外部 MCP server 提供的工具（还没有 server 就绪时返回空列表，不影响内置工具）
        tools.extend(mcp_tools)

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

    # 黑名单实现在 tool_permissions 里；保留同名类属性以兼容 cls.BLOCKED_* 引用
    BLOCKED_COMMAND_PATTERNS = permissions.BLOCKED_COMMAND_PATTERNS
    BLOCKED_WRITE_PATHS = permissions.BLOCKED_WRITE_PATHS




    @classmethod
    def _resolve_agent_path(cls, path: str):
        """子agent工作区路径解析（策略见 tool_permissions.resolve_agent_path）。"""
        return permissions.resolve_agent_path(path, cls._agent_workspace, cls.is_full_access())

    @staticmethod
    def _check_command_blocked(command: str):
        """检查命令是否命中黑名单，命中返回拦截原因，否则返回 None。"""
        return permissions.command_blocked(command)

    @classmethod
    def _find_outside_workspace_paths(cls, command: str, workspace: str) -> list[str]:
        """提取命令中出现在工作区外的字面绝对路径，用于限制子agent命令范围。"""
        return permissions.outside_workspace_paths(command, workspace)


    @classmethod
    def execute_tool(cls, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        # 只读沙箱：状态改变类工具在执行前直接拒绝（权限门只此一处）
        if permissions.read_only_blocks_tool(cls._sandbox_mode, tool_name):
            logging.warning("只读权限模式拒绝了工具 %s", tool_name)
            return cls._result(False, permissions.read_only_rejection(tool_name))
        if cls._apps is None:
            cls.load_apps()

        handler = TOOL_DISPATCH.get(tool_name)
        if handler is None:
            return cls._result(False, f"未知工具: {tool_name}")
        try:
            return handler(cls, arguments)
        except Exception as e:
            return cls._result(False, f"执行工具 {tool_name} 失败: {str(e)}")

#======================================================静态函数=========================================================
        



    
    
    




        





        





        
        


# Bind the moved methods back with their original decorator, so every
# call site and every cls.<state> access behaves exactly as before.
ActionHandler._builtin_tool_definitions = classmethod(_builtin_tool_definitions)
ActionHandler.load_apps = classmethod(load_apps)
ActionHandler.load_auth_config = classmethod(load_auth_config)
ActionHandler.configure = classmethod(configure)
ActionHandler.get_available_apps = classmethod(get_available_apps)
ActionHandler._get_amap_api_key = classmethod(_get_amap_api_key)
ActionHandler.set_pet_command_callback = classmethod(set_pet_command_callback)
ActionHandler.set_pet_param_catalog = classmethod(set_pet_param_catalog)
ActionHandler._handle_control_pet = classmethod(_handle_control_pet)
ActionHandler._handle_set_live2d_param = classmethod(_handle_set_live2d_param)
ActionHandler._handle_list_live2d_params = classmethod(_handle_list_live2d_params)
ActionHandler._get_current_datetime = staticmethod(_get_current_datetime)
ActionHandler._city_to_adcode = classmethod(_city_to_adcode)
ActionHandler._get_weather = classmethod(_get_weather)
ActionHandler._get_forecast = classmethod(_get_forecast)
ActionHandler._geocode = classmethod(_geocode)
ActionHandler._format_distance = staticmethod(_format_distance)
ActionHandler._format_duration = staticmethod(_format_duration)
ActionHandler._get_route = classmethod(_get_route)
ActionHandler._lock_screen = staticmethod(_lock_screen)
ActionHandler._shutdown = staticmethod(_shutdown)
ActionHandler._reboot = staticmethod(_reboot)
ActionHandler._get_gpu_status = staticmethod(_get_gpu_status)
ActionHandler._get_system_status = classmethod(_get_system_status)
ActionHandler._get_startup_items = staticmethod(_get_startup_items)
ActionHandler._get_health_check = classmethod(_get_health_check)
ActionHandler._scan_disk_junk = staticmethod(_scan_disk_junk)
ActionHandler._get_local_ip = staticmethod(_get_local_ip)
ActionHandler._get_public_ip = staticmethod(_get_public_ip)
ActionHandler._get_mac_address = staticmethod(_get_mac_address)
ActionHandler._speedtest = staticmethod(_speedtest)
ActionHandler._network_info = classmethod(_network_info)
ActionHandler._ping_host = staticmethod(_ping_host)
ActionHandler._search_food = classmethod(_search_food)
ActionHandler._search_web = classmethod(_search_web)
ActionHandler._search_searxng_results = classmethod(_search_searxng_results)
ActionHandler._optimize_search_query = classmethod(_optimize_search_query)
ActionHandler._is_sports_search = staticmethod(_is_sports_search)
ActionHandler._is_latest_search = staticmethod(_is_latest_search)
ActionHandler._normalize_latest_query_years = staticmethod(_normalize_latest_query_years)
ActionHandler._optimize_sports_search_query = staticmethod(_optimize_sports_search_query)
ActionHandler._extract_query_keywords = staticmethod(_extract_query_keywords)
ActionHandler._score_search_result = classmethod(_score_search_result)
ActionHandler._rerank_search_results = classmethod(_rerank_search_results)
ActionHandler._get_tavily_api_key = classmethod(_get_tavily_api_key)
ActionHandler._tavily_search = classmethod(_tavily_search)
ActionHandler._tavily_extract = classmethod(_tavily_extract)
ActionHandler._fetch_page_text = staticmethod(_fetch_page_text)
ActionHandler._direct_fetch_pages = classmethod(_direct_fetch_pages)
ActionHandler.set_agent_workspace = classmethod(set_agent_workspace)
ActionHandler.set_timeout_confirm_callback = classmethod(set_timeout_confirm_callback)
ActionHandler._confirm_continue_waiting = classmethod(_confirm_continue_waiting)
ActionHandler._run_command = classmethod(_run_command)
