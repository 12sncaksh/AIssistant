"""Current date-time tool."""

from __future__ import annotations
from datetime import datetime



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
