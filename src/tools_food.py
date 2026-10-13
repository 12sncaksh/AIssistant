"""Nearby restaurant search tool."""

from __future__ import annotations
import http_client



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
    api_key = cls._get_amap_api_key()
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
            resp = http_client.session().get(geo_url, timeout=5)
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
        resp = http_client.session().get(search_url, timeout=8)
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
