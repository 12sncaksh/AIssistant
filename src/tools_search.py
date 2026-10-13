"""Web search, query shaping and result ranking."""

from __future__ import annotations
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
import re
import requests
import http_client



def _search_web(query: str, max_results: int = 5, user_intent: str | None = None) -> dict[str, Any]:
    """联网搜索：Tavily Search 优先（结果自带正文），SearXNG 兜底补充，合并去重后统一打分排序。"""
    effective_query, search_profile = cls._optimize_search_query(query, user_intent=user_intent)
    sports_mode = search_profile["sports_mode"]
    latest_mode = search_profile["latest_mode"]
    fetch_count = max(max_results * 2, 8)

    tavily_results, tavily_note = cls._tavily_search(effective_query, max_results=fetch_count)

    searxng_results = []
    searxng_note = ""
    try:
        searxng_results = cls._search_searxng_results(
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
        return cls._result(False, f"搜索失败：{detail}")

    results = cls._rerank_search_results(
        merged,
        effective_query,
        sports_mode=sports_mode,
        latest_mode=latest_mode
    )[:max_results]

    if not results:
        return cls._result(True, f"未找到关于「{effective_query}」的相关信息。", {
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
        extracted_map, extract_note = cls._tavily_extract(extract_urls[:3], effective_query)
        if extracted_map:
            extract_provider = "Tavily"
        else:
            direct_map, direct_note = cls._direct_fetch_pages(extract_urls[:3])
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
    return cls._result(True, message, data)


def _search_searxng_results(
    query: str,
    max_results: int = 5,
    sports_mode: bool = False,
    latest_mode: bool = False
) -> list[dict[str, Any]]:
    base_url = cls._search_config.get("searxng_url", "http://localhost:18080").rstrip("/")

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
        resp = http_client.session().get(f"{base_url}/search", params=params, timeout=30)
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


def _optimize_search_query(query: str, user_intent: str | None = None) -> tuple[str, dict[str, Any]]:
    normalized_query = " ".join(str(query or "").split())
    normalized_intent = " ".join(str(user_intent or "").split())

    base_query = normalized_query
    if normalized_intent and cls._is_sports_search(normalized_intent):
        base_query = normalized_intent

    sports_mode = cls._is_sports_search(base_query)
    latest_mode = cls._is_latest_search(base_query)

    if sports_mode:
        effective_query = cls._optimize_sports_search_query(base_query, latest_mode=latest_mode)
    else:
        effective_query = cls._normalize_latest_query_years(base_query, latest_mode=latest_mode)

    profile = {
        "sports_mode": sports_mode,
        "latest_mode": latest_mode,
        "original_query": normalized_query,
        "base_query": base_query,
        "current_date": datetime.now().strftime("%Y-%m-%d")
    }
    return effective_query, profile


def _is_sports_search(text: str) -> bool:
    if not text:
        return False
    keywords = (
        "世界杯", "世俱杯", "预选赛", "足球", "国足", "欧冠", "英超", "西甲", "德甲",
        "意甲", "法甲", "中超", "NBA", "CBA", "网球", "F1", "奥运", "比赛", "赛况", "比分"
    )
    return any(keyword in text for keyword in keywords)


def _is_latest_search(text: str) -> bool:
    if not text:
        return False
    keywords = (
        "最新", "实时", "今天", "今日", "当前", "刚刚", "赛况", "比分", "赛果",
        "战报", "赛程", "积分榜", "排名", "结果", "动态"
    )
    return any(keyword in text for keyword in keywords)


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

    keywords = cls._extract_query_keywords(query)
    for keyword in keywords:
        if keyword in title:
            score += 2
        elif keyword in content:
            score += 1
        if keyword in url:
            score += 1

    return score


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
        score = cls._score_search_result(item, query, sports_mode=sports_mode, latest_mode=latest_mode)
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
    keywords = cls._extract_query_keywords(query)
    filtered = [item for item in deduped if (not keywords) or item.get("_score", 0) >= 2]
    final_results = filtered if len(filtered) >= 3 else deduped

    for item in final_results:
        item.pop("_score", None)
    return final_results
