"""Tavily and direct page fetching."""

from __future__ import annotations
from typing import Any
import os
import http_client



def _get_tavily_api_key(cls) -> str:
    configured = cls._search_config.get("tavily_api_key", "").strip()
    if configured:
        return configured
    env_key = os.getenv("TAVILY_API_KEY", "").strip()
    if env_key:
        return env_key
    cls.load_auth_config()
    return cls._auth_config.get("tavily_api_key", "").strip()


def _tavily_search(cls, query: str, max_results: int = 8, search_depth: str = "advanced") -> tuple[list[dict[str, Any]], str]:
    """Tavily Search：高质量联网检索，结果自带正文片段，作为搜索主源。失败返回空列表和原因。"""
    api_key = cls._get_tavily_api_key()
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
        resp = http_client.session().post("https://api.tavily.com/search", json=payload, timeout=40)
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


def _tavily_extract(cls, urls: list[str], query: str) -> tuple[dict[str, str], str]:
    api_key = cls._get_tavily_api_key()
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
        resp = http_client.session().post("https://api.tavily.com/extract", headers=headers, json=payload, timeout=40)
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


def _fetch_page_text(url: str, timeout: int = 10, max_chars: int = 3000) -> str:
    """直接抓取网页并提取可读正文；失败或内容为空返回空字符串。"""
    try:
        from bs4 import BeautifulSoup
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0 Safari/537.36")
        }
        resp = http_client.session().get(url, headers=headers, timeout=timeout)
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


def _direct_fetch_pages(cls, urls: list[str], max_chars: int = 1200) -> tuple[dict[str, str], str]:
    """Tavily 不可用时的免费兜底：直接抓取前几条链接正文。"""
    if not urls:
        return {}, "没有可抓取的网址。"
    extracted_map = {}
    for url in urls[:3]:
        text = cls._fetch_page_text(url, timeout=10, max_chars=max_chars)
        if text:
            extracted_map[url] = text
    if not extracted_map:
        return {}, "直接抓取网页正文失败，已回退为搜索摘要。"
    return extracted_map, "已通过直接抓取网页正文增强搜索结果。"
