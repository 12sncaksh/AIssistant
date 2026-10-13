"""System status, health check and power tools."""

from __future__ import annotations
from datetime import datetime
import cpuinfo
import os
import psutil
import sys



def _lock_screen():
    """锁屏 (Windows)"""
    if sys.platform == "win32":
        os.system("rundll32.exe user32.dll,LockWorkStation")
        return True, "已锁屏"
    return False, "当前系统不支持锁屏"


def _shutdown():
    """关机，延时10秒，用户可取消"""
    if sys.platform == "win32":
        os.system("shutdown /s /t 10")
        return True, "系统将关机"
    return False, "当前系统不支持关机"


def _reboot():
    """重启，延时10秒，用户可取消"""
    if sys.platform == "win32":
        os.system("shutdown /r /t 10")
        return True, "系统将重启"
    return False, "当前系统不支持重启"


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
        gpu_info = cls._get_gpu_status()
        if gpu_info and not gpu_info.startswith("未安装") and not gpu_info.startswith("未检测到"):
            info += "\n" + gpu_info
        elif gpu_info:
            info += f"\n[提示] {gpu_info}"
        return True, info
    except Exception as e:
        return False, f"获取系统状态失败: {str(e)}"


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
    gpu_info = cls._get_gpu_status()
    if gpu_info and not gpu_info.startswith("未安装") and not gpu_info.startswith("未检测到"):
        lines.append("显卡：" + gpu_info)
    elif gpu_info:
        lines.append(f"[提示] {gpu_info}")
    try:
        local_ip = cls._get_local_ip()
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
    startup_items = cls._get_startup_items()
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
