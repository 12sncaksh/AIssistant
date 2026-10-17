"""Weather, forecast, geocoding and route planning tools."""

from __future__ import annotations
import http_client



def _city_to_adcode(cls, city, api_key):
    # 将城市名转换为高德 adcode：先地理编码，失败则查内置城市表。
    try:
        geo_url = f"https://restapi.amap.com/v3/geocode/geo?address={city}&output=json&key={api_key}"
        resp = http_client.session().get(geo_url, timeout=5)
        geo_data = resp.json()
        if geo_data.get("status") == "1" and geo_data.get("geocodes"):
            return geo_data["geocodes"][0]["adcode"]
    except Exception:
        pass
    key = city.strip().replace("市", "").replace("省", "")
    return cls._CITY_ADCODE.get(key, "")


def _get_weather(cls, city):
    import requests
    api_key = cls._get_amap_api_key()
    if not api_key:
        return False, "未配置高德 API Key，请在 config/auth.json 中填写 amap_api_key。"
    # 第一步：地理编码，将城市名转换为城市代码
    city_code = cls._city_to_adcode(city, api_key)
    if not city_code:
        return False, f"未找到城市“{city}”，请检查名称"
    # 第二步：查询实时天气
    try:
        weather_url = f"https://restapi.amap.com/v3/weather/weatherInfo?city={city_code}&key={api_key}"
        weather_resp = http_client.session().get(weather_url, timeout=5)
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


def _get_forecast(cls, city, days=4):
    import requests
    api_key = cls._get_amap_api_key()
    if not api_key:
        return False, "未配置高德 API Key，请在 config/auth.json 中填写 amap_api_key。"
    # 第一步：地理编码，获取城市 adcode
    city_code = cls._city_to_adcode(city, api_key)
    if not city_code:
        return False, f"未找到城市“{city}”，请检查名称"
    # 第二步：查询天气预报（extensions=all）
    try:
        weather_url = f"https://restapi.amap.com/v3/weather/weatherInfo?city={city_code}&key={api_key}&extensions=all"
        weather_resp = http_client.session().get(weather_url, timeout=5)
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


def _geocode(cls, address):
    """调用高德地理编码API，返回 (经度, 纬度)"""
    api_key = cls._get_amap_api_key()
    if not api_key:
        return None, None
    url = f"https://restapi.amap.com/v3/geocode/geo?address={address}&output=json&key={api_key}"
    try:
        resp = http_client.session().get(url, timeout=5)
        data = resp.json()
        if data.get("status") == "1" and data.get("geocodes"):
            location = data["geocodes"][0]["location"]  # "经度,纬度"
            lng, lat = location.split(',')
            return float(lng), float(lat)
        else:
            return None, None
    except:
        return None, None


def _format_distance(meters):
    meters = float(meters)  # 强制转换为数字
    if meters < 1000:
        return f"{int(meters)}米"
    else:
        return f"{meters/1000:.1f}公里"


def _format_duration(seconds):
    seconds = int(seconds)
    minutes = seconds // 60
    secs = seconds % 60
    if secs > 0:
        return f"{minutes}分{secs}秒"
    else:
        return f"{minutes}分钟"


def _get_route(cls, origin, destination, mode="drive"):
    import requests
    api_key = cls._get_amap_api_key()
    if not api_key:
        return False, "未配置高德 API Key，请在 config/auth.json 中填写 amap_api_key。"
    # 地理编码
    o_lng, o_lat = cls._geocode(origin)
    d_lng, d_lat = cls._geocode(destination)
    if o_lng is None or d_lng is None:
        return False, f"无法解析地名“{origin if o_lng is None else destination}”，请使用更具体的地点名称。"

    # 根据方式调用不同API
    if mode == "drive":
        url = f"https://restapi.amap.com/v3/direction/driving?origin={o_lng},{o_lat}&destination={d_lng},{d_lat}&key={api_key}&extensions=all"
        resp = http_client.session().get(url, timeout=10)
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
        reply = (f"从{origin}到{destination}，驾车{cls._format_distance(distance)}，"
                f"预计{cls._format_duration(duration)}，途经{roads_text}，"
                f"红绿灯约{traffic_lights}个。")
        if int(tolls) > 0:
            reply += f" 收费路段{cls._format_distance(tolls)}。"
        return True, reply

    elif mode == "walk":
        url = f"https://restapi.amap.com/v3/direction/walking?origin={o_lng},{o_lat}&destination={d_lng},{d_lat}&key={api_key}"
        resp = http_client.session().get(url, timeout=10)
        data = resp.json()
        if data.get("status") != "1" or not data.get("route", {}).get("paths"):
            return False, f"步行路线规划失败：{data.get('info', '未知错误')}"
        path = data["route"]["paths"][0]
        distance = int(path["distance"])
        duration = int(path["duration"])
        steps = path.get("steps", [])
        instructions = [step["instruction"] for step in steps[:3]]
        reply = (f"从{origin}步行到{destination}，{cls._format_distance(distance)}，"
                f"约{cls._format_duration(duration)}。")
        if instructions:
            reply += f" 路线建议：{'；'.join(instructions)}。"
        return True, reply

    elif mode == "bus":
        url = f"https://restapi.amap.com/v3/direction/transit/integrated?origin={o_lng},{o_lat}&destination={d_lng},{d_lat}&key={api_key}"
        resp = http_client.session().get(url, timeout=10)
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
                    steps_desc.append(f"{step_num}. 步行 {cls._format_distance(walk_distance)}")
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

        reply = f"从{origin}到{destination}，推荐公共交通方案（约{cls._format_duration(duration)}，总步行{cls._format_distance(total_walk)}，票价约{cost/100:.1f}元）：\n"
        reply += "\n".join(steps_desc)
        return True, reply

    elif mode == "bike":
        # 骑行（高德不支持骑行规划？可使用步行代替或提示）
        # 高德目前没有公开的骑行API，可以用步行代替并提示。
        return False, "抱歉，暂不支持骑行路线规划，您可以使用步行或驾车方案。"
    else:
        return False, f"不支持的出行方式: {mode}"
