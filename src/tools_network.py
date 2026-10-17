"""Network information and connectivity tools."""

from __future__ import annotations
import re
import http_client
import socket
import uuid



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
                resp = http_client.session().get(api, timeout=5)
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


def _get_mac_address():
    """获取本机 MAC 地址"""
    mac = uuid.getnode()
    # 格式化为 XX:XX:XX:XX:XX:XX
    mac_hex = ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
    return mac_hex


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
            response = http_client.session().get(url, stream=True, timeout=10)
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


def _network_info(cls):
    """组合网络信息：内网IP、公网IP、MAC地址"""
    local_ip = cls._get_local_ip()
    public_ip = cls._get_public_ip()
    mac = cls._get_mac_address()
    return (
        f"内网 IPv4: {local_ip}\n"
        f"公网 IPv4: {public_ip}\n"
        f"MAC 地址: {mac}"
    )


def _ping_host(host="8.8.8.8"):
    """Ping 一个主机，返回丢包率和平均延迟（Windows 下使用 subprocess）"""
    import subprocess
    import re
    try:
        result = subprocess.run(
            ["ping", "-n", "4", host],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
