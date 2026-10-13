"""Screen capture and the vision model request."""

from __future__ import annotations
import base64
import http_client
import io
import json
import logging



def _capture_screen(cls, region=None):
    try:
        import mss
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("缺少 mss 或 Pillow，请先安装视觉截图依赖。") from exc

    with mss.mss() as capture:
        monitor = capture.monitors[0]
        if region:
            monitor_left = int(monitor.get("left", 0))
            monitor_top = int(monitor.get("top", 0))
            monitor_right = monitor_left + int(monitor.get("width", 0))
            monitor_bottom = monitor_top + int(monitor.get("height", 0))
            left = max(monitor_left, int(region.get("x", monitor_left)))
            top = max(monitor_top, int(region.get("y", monitor_top)))
            right = min(monitor_right, left + int(region.get("width", 0)))
            bottom = min(monitor_bottom, top + int(region.get("height", 0)))
            if right <= left or bottom <= top:
                raise ValueError("截图区域无效或超出当前桌面范围。")
            capture_area = {
                "left": left,
                "top": top,
                "width": right - left,
                "height": bottom - top,
            }
        else:
            capture_area = monitor
        raw = capture.grab(capture_area)
        image = Image.frombytes("RGB", raw.size, raw.rgb)
        original_width, original_height = image.size
        max_dimension = 1280 if region else 1600
        scale = min(1.0, max_dimension / max(original_width, original_height))
        if scale < 1.0:
            image = image.resize(
                (round(original_width * scale), round(original_height * scale)),
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=82, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        screen_info = {
            "width": original_width,
            "height": original_height,
            "origin_x": capture_area.get("left", 0),
            "origin_y": capture_area.get("top", 0),
            "image_width": image.width,
            "image_height": image.height,
            "scale": scale,
            "region": bool(region),
        }
        return f"data:image/jpeg;base64,{encoded}", screen_info


def _screen_bounds(cls):
    if cls._screen_bounds_cache:
        return cls._screen_bounds_cache
    try:
        import mss
        with mss.mss() as capture:
            monitor = capture.monitors[0]
            bounds = (
                int(monitor.get("left", 0)),
                int(monitor.get("top", 0)),
                int(monitor.get("width", 0)),
                int(monitor.get("height", 0)),
            )
            cls._screen_bounds_cache = bounds
            return bounds
    except ImportError as exc:
        raise RuntimeError("缺少 mss，请先安装视觉截图依赖。") from exc


def _request_vision_json(cls, payload, read_timeout=60):
    """调用视觉模型；用流式分块读取，让长按 Q 的取消能真正中断请求。

    原来把请求丢进额外的 daemon 线程再轮询队列，取消只是「不等结果」，
    线程仍占着连接直到超时。现在在本线程分块读取，每个分块检查一次取消标志：
    取消后最多等一个分块的时间，并立刻 close() 释放连接。
    对外返回语义不变：(data, None) 成功 / (None, 错误) 失败 / (None, "cancelled") 取消。
    """
    response = None
    raw_text = ""
    try:
        response = http_client.session().post(
            f"{cls._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {cls._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=(10, read_timeout),
            stream=True,
        )
        chunks = []
        for chunk in response.iter_content(chunk_size=32768):
            if cls.is_cancelled():
                return None, "cancelled"
            if chunk:
                chunks.append(chunk)
        raw_text = b"".join(chunks).decode("utf-8", errors="replace")[:12000]
    except Exception as exc:
        return None, exc
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                logging.debug("关闭视觉响应失败", exc_info=True)

    if response.status_code != 200:
        return None, f"HTTP {response.status_code} - {raw_text[:1000]}"
    try:
        return json.loads(raw_text), None
    except json.JSONDecodeError as exc:
        return None, f"视觉模型返回了无效 JSON：{exc}"
