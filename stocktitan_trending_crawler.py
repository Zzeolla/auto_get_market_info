# stocktitan_trending_crawler.py
import os
import re
import json
import time
import html
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin
import shutil
import textwrap
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup, Tag

TRENDING_URL = "https://www.stocktitan.net/news/trending.html"
STATE_FILE = "stocktitan_trending_state.json"  # 직전 Top7 기억용(기사 URL 세트 저장)

BASE = "https://www.stocktitan.net"

ARTICLE_RE = re.compile(
    r"^/news/[A-Z0-9\.\-]+/.+\.html$",  # /news/TICKER/slug.html 형태만 허용
)

HUB_PATHS = {
    "/news/trending.html",
    "/news/live.html",
    "/news/crypto.html",
    "/news/ai.html",
    "/news/fda-approvals.html",
    "/news/clinical-trials.html",
    "/news/today",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_TEST_CHANNEL_ID = os.getenv("TELEGRAM_TEST_CHANNEL_ID")

RECENT_SEEN_LIMIT = 100
RECENT_EXPIRE_DAYS = 7  # 7일 동안만 '이미 전송한 URL'로 간주

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s"
)

def normalize_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return urljoin(BASE, href)

def is_article_url(href: str) -> bool:
    """허브/카테고리 페이지 제외하고, 티커 경로가 포함된 개별 기사만 True"""
    try:
        if not href:
            return False
        if not href.startswith("http"):
            # 상대경로도 허용 (정규식은 상대경로 기준으로 맞춤)
            rel = href
        else:
            # 도메인 외 경로만 분리
            rel = re.sub(r"^https?://www\.stocktitan\.net", "", href)
        if rel in HUB_PATHS:
            return False
        if ARTICLE_RE.match(rel):
            return True
        return False
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────────────────────
# 유틸: 저장/불러오기
# ─────────────────────────────────────────────────────────────────────────────
def _load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 하위호환: 이전 버전엔 recent_urls 없을 수 있음
        data.setdefault("recent_urls", [])
        data.setdefault("last_top7_urls", [])
        return data
    except FileNotFoundError:
        return {"recent_urls": [], "last_top7_urls": []}

def _save_state(state: Dict) -> None:
    state["updated_at"] = time.time()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_prev_ids() -> set:
    # 기존 로직과 호환: 지난 사이클의 Top7
    return set(_load_state().get("last_top7_urls", []))

def save_curr_ids(curr_ids: List[str]) -> None:
    st = _load_state()
    st["last_top7_urls"] = curr_ids
    _save_state(st)

def load_recent_seen() -> dict[str, float]:
    """최근 전송한 URL: timestamp 딕셔너리 반환"""
    st = _load_state()
    recent = st.get("recent_urls", {})

    # 혹시 예전 버전(리스트)이 남아있으면 딕셔너리로 변환
    if isinstance(recent, list):
        recent = {url: time.time() for url in recent}
        st["recent_urls"] = recent
        _save_state(st)
    return recent

def add_recent_seen(urls: List[str]) -> None:
    """새로 전송한 URL을 최근 목록에 추가 (기존 있으면 timestamp만 갱신)."""
    st = _load_state()
    recent: dict[str, float] = st.get("recent_urls", {})
    if isinstance(recent, list):
        # 이전 버전 호환 처리
        recent = {url: time.time() for url in recent}

    now = time.time()
    for url in urls:
        recent[url] = now  # 이미 있으면 timestamp 갱신, 없으면 새로 추가

    # 🔹 오래된 항목 정리 (7일 이상 지난 것은 자동 제거)
    cutoff = now - RECENT_EXPIRE_DAYS * 24 * 3600
    recent = {u: ts for u, ts in recent.items() if ts >= cutoff}

    st["recent_urls"] = recent
    _save_state(st)

def get_unseen_items(data: list[dict]) -> list[dict]:
    """
    아직 전송하지 않은(또는 기간 만료로 재전송 허용된) 항목만 필터링.
    """
    recent = load_recent_seen()
    unseen = [d for d in data if d.get("url") not in recent]
    return unseen

# ─────────────────────────────────────────────────────────────────────────────
# 유틸: 번역 훅 (엔진 붙이기 전까지는 그대로 반환)
#  - 실제 서비스에선 Papago → MS → DeepL 순 fallback 연결 권장(사용자 선호 반영)
# ─────────────────────────────────────────────────────────────────────────────

def translate_with_gpt4omini(text: str, target_lang: str = "ko", source_lang: Optional[str] = None) -> str:
    if not text or text.strip() == "":
        return text
    if not OPENAI_API_KEY:
        return text  # 키 없으면 원문 그대로

    system_msg = (
        "You are a precise translator. Translate ONLY natural language segments into the target language. "
        "STRICTLY preserve as-is: emojis, URLs (https://...), emails, @mentions, #hashtags, $tickers, "
        "any placeholders like [EMOJI_0], {EMOJI_1}, [[EMOJI_2]], code, and original line breaks/spaces. "
        "Do not add extra text. Output only the translation."
    )
    user_msg = (f"Source language: {source_lang}\n" if source_lang else "") + \
               f"Target language: {target_lang}\n\nText:\n{text}"

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        out = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        return out or text
    except Exception:
        return text  # 실패하면 원문 유지

def translate_text(text: str, target_lang: str = "ko") -> str:
    return translate_with_gpt4omini(text, target_lang=target_lang)


# ─────────────────────────────────────────────────────────────────────────────
# 1) 트렌딩 Top7 파싱
#    - rank/title/ticker/url + (가능하면) 트렌딩 카드에 있는 rhea 요약/긍/부정
# ─────────────────────────────────────────────────────────────────────────────
def fetch_trending_top7() -> List[Dict]:
    resp = requests.get(TRENDING_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items: List[Dict] = []
    seen = set()
    rank = 0

    # 우선 카드 영역부터 좁혀보기 (있으면 가장 정확)
    containers = soup.select(
        ".trending-news, #trending-news, .news-list, .cards, .container"
    )
    search_scopes = containers if containers else [soup]

    for scope in search_scopes:
        # 카드 내 대표 기사 링크: “/news/TICKER/slug.html” 만
        for a in scope.select("a[href]"):
            href = a.get("href") or ""
            if not is_article_url(href):
                continue
            url = normalize_url(href)
            if url in seen:
                continue

            title = a.get_text(strip=True) or ""
            if not title:
                # 제목이 a가 아닌 상위에 있을 수도 → 주변에서 보강
                parent = a
                for _ in range(3):
                    parent = parent.parent
                    if not parent:
                        break
                    h = parent.find(["h2", "h3"])
                    if h and h.get_text(strip=True):
                        title = h.get_text(strip=True)
                        break
            # 티커 추출
            m = re.search(r"/news/([A-Z0-9\.\-]+)/", url)
            ticker = m.group(1) if m else None

            # ⭐ 카드 블록에서 요약/긍정/부정을 함께 추출
            card_block = a.parent
            trending_summary = extract_rhea_summary_from_block(card_block) or None
            tpos, tneg = extract_pos_neg_from_block(card_block)

            rank += 1
            items.append({
                "rank": rank,
                "title": title or "(No title)",
                "ticker": ticker,
                "url": url,
                # 트렌딩 카드의 Rhea 요약/긍/부정은 필요 시 아래 함수로 시도
                "trending_summary": trending_summary,
                "trending_positive": tpos,
                "trending_negative": tneg,
            })
            seen.add(url)
            if rank >= 7:
                break
        if rank >= 7:
            break

    return items[:7]

def _truncate(s: str, n: int = 400) -> str:
    # s = (s or "").strip()
    # return (s[:n-1] + "…") if len(s) > n else s
    return (s or "").strip()

def _print_bullets(lines, max_items: int = 10, prefix: str = "   • "):
    if not lines:
        return
    for i, line in enumerate(lines[:max_items], 1):
        print(f"{prefix}{line}")

def _bullets(lines: list[str]) -> str:
    return "\n".join(f"   • {x}" for x in lines)

def extract_rhea_summary_from_block(block: Tag) -> Optional[str]:
    """
    트렌딩 카드 블록에서 'Rhea-AI Summary' 텍스트 덩어리 추출 시도.
    """
    if not isinstance(block, Tag):
        return None

    # 헤더 텍스트로 구분
    candidates = []
    for lab in block.find_all(string=True):
        t = (lab or "").strip()
        if not t:
            continue
        # 레이블 앞뒤로 구분되는 구조라면 주변 텍스트를 뽑아보자
        if re.search(r"Rhea[- ]?AI Summary", t, re.I):
            # 이 노드 기준으로 다음 텍스트 추린다
            parent = lab.parent
            # 요약 본문 후보: 형제 요소, 다음 요소 등 폭넓게 찾기
            # 너무 길면 적당히 잘라 사용
            summary = collect_following_text(parent, stop_labels=["Positive", "Negative", "Insights"], max_chars=1800)
            if summary:
                candidates.append(summary)

    # 가장 긴 후보를 선택(잡음 대비)
    if candidates:
        return max(candidates, key=len)
    return None

def extract_rhea_from_detail(soup: BeautifulSoup) -> Dict:
    """
    상세 페이지에서 .article-rhea-tools 기준으로
    summary/positive/negative/insights를 구조적으로 추출.
    summary는 한국어(.summary-ko) 우선, 없으면 영어→번역 훅.
    """
    tools = soup.select_one(".article-rhea-tools, #article-rhea-tools") or soup  # ← 상세 블록 없을 때도 soup에서 탐색

    # --- Summary ---
    summary_ko = None
    summary_en = None
    summary_box = tools.select_one(
        ".news-card-summary.mb-2, .news-card-summary, .rhea-summary, #news-card-summary"
    )
    if summary_box:
        ko = summary_box.select_one(".summary-ko, .ko, [lang='ko'], #id-summary-ko")
        if ko and ko.get_text(strip=True):
            summary_ko = {"lang": "ko", "text": ko.get_text(" ", strip=True)}
        en = summary_box.select_one("#summary, .summary-en, .en, [lang='en'], #id-summary-en")
        if en and en.get_text(strip=True):
            summary_en = {"lang": "en", "text": en.get_text(" ", strip=True)}
        # else:
        #     en = summary_box.select_one("#summary, .summary-en, .en, [lang='en'], #id-summary-en")
        #     zh = summary_box.select_one(".summary-zh, .zh, [lang='zh'], #id-summary-zh")
        #     ja = summary_box.select_one(".summary-ja, .ja, [lang='ja'], #id-summary-ja")
        #     cand = next((el for el in [en, zh, ja] if el and el.get_text(strip=True)), None)
        #     if cand:
        #         summary = {"lang": "ko", "text": translate_text(cand.get_text(" ", strip=True), "ko")}

    # --- Positive/Negative/Insights ---
    # ✅ 스크린샷 구조 반영 (news-card-positive/negative, experts-container)
    positive_sel = (
        ".positive-points li, .rhea-positive li, .news-card-pros li, "
        "#news-card-positive li, .news-card-positive li"
    )
    negative_sel = (
        ".negative-points li, .rhea-negative li, .news-card-cons li, "
        "#news-card-negative li, .news-card-negative li"
    )
    insights_li_sel = ".insights li, .key-insights li, .takeaways li"
    insights_p_sel  = "#experts-container .accordion-body p, .insights p, .key-insights p, .takeaways p"

    positives = [li.get_text(" ", strip=True) for li in tools.select(positive_sel)]
    negatives = [li.get_text(" ", strip=True) for li in tools.select(negative_sel)]

    # Insights는 li도 있고 p도 있어 둘 다 수집
    insights = [li.get_text(" ", strip=True) for li in tools.select(insights_li_sel)]
    insights += [p.get_text(" ", strip=True) for p in tools.select(insights_p_sel)]

    # 중복 제거
    def dedup(xs):
        out, seen = [], set()
        for x in xs:
            k = x.strip().lower()
            if k and k not in seen:
                out.append(x.strip())
                seen.add(k)
        return out

    return {
        "summary_ko": summary_ko,
        "summary_en": summary_en,
        "positive": dedup(positives),
        "negative": dedup(negatives),
        "insights": dedup(insights),
    }


def extract_pos_neg_from_block(block: Tag) -> Tuple[List[str], List[str]]:
    """
    트렌딩 카드에서 Positive/Negative 항목(불릿/문장 리스트) 추출.
    """
    positives, negatives = [], []
    if not isinstance(block, Tag):
        return positives, negatives

    sections = {
        "positive": ["positive", "positives", "pros", "bull", "bullish", "pro:"],
        "negative": ["negative", "negatives", "cons", "bear", "bearish", "con:"],
    }

    # 라벨 텍스트 매칭 후, 이어지는 bullets/문장 수집
    texts = block.find_all(string=True)
    for i, txt in enumerate(texts):
        t = (txt or "").strip()
        if not t:
            continue
        lower = t.lower()
        if any(key in lower for key in sections["positive"]):
            content = collect_following_text(txt.parent, stop_labels=["Negative", "Insights"], max_chars=1200)
            bullets = split_to_bullets(content)
            positives.extend(bullets)
        if any(key in lower for key in sections["negative"]):
            content = collect_following_text(txt.parent, stop_labels=["Positive", "Insights"], max_chars=1200)
            bullets = split_to_bullets(content)
            negatives.extend(bullets)

    return dedup_list(positives), dedup_list(negatives)


def collect_following_text(start_node: Tag, stop_labels: List[str], max_chars: int = 2000) -> str:
    """
    특정 레이블(예: 'Rhea-AI Summary')를 기준으로 이후 형제/다음 노드들의
    텍스트를 이어 붙이다가 stop_labels 중 하나를 만나면 멈춤.
    """
    if not isinstance(start_node, Tag):
        start_node = start_node.parent if hasattr(start_node, "parent") else None
    if not isinstance(start_node, Tag):
        return ""

    texts = []
    node = start_node
    char_count = 0
    for _ in range(200):  # 안전 장치
        node = node.find_next() if hasattr(node, "find_next") else None
        if not node or not isinstance(node, Tag):
            break
        # 멈춤 레이블 도달?
        label = node.get_text(strip=True)
        if any(lbl.lower() in label.lower() for lbl in stop_labels):
            break
        # 링크/버튼/아이콘 등은 제외
        if node.name in {"a", "button", "svg", "img", "script", "style"}:
            continue
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        texts.append(text)
        char_count += len(text)
        if char_count >= max_chars:
            break

    joined = " ".join(texts)
    # 공백 정리
    joined = re.sub(r"\s{2,}", " ", joined).strip()
    return joined


def split_to_bullets(text: str) -> List[str]:
    if not text:
        return []
    # 점/하이픈 목록 분리 시도
    parts = re.split(r"(?:\n|\r|•|-|\u2022)\s*", text)
    parts = [p.strip(" -•\t\r\n") for p in parts if p and len(p.strip()) > 1]
    return parts[:8]  # 너무 많으면 상위 몇 개만


def dedup_list(xs: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in xs:
        k = x.lower()
        if k not in seen:
            out.append(x)
            seen.add(k)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 2) 기사 상세 파싱
#    - 상세에 Rhea-AI Summary/Positive/Negative/Insights가 있으면 그걸 우선 사용
#    - 없으면 트렌딩 카드에서 가져온 값으로 fallback
#    - 기사 본문을 섹션/헤더(굵게/제목) 포함하여 구조적으로 추출
# ─────────────────────────────────────────────────────────────────────────────
def parse_article_detail(url: str) -> Dict:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # 메타
    title = (soup.select_one("h1") or soup.select_one("title"))
    title_text = title.get_text(strip=True) if title else ""

    published_at = extract_published_at(soup)
    source_url = extract_source_url(soup)

    # ✅ 상세 페이지의 Rhea-AI 블록(구조 기반) 파싱
    rhea = extract_rhea_from_detail(soup)

    # 기사 본문(헤더/볼드 감지 포함) — 기존 함수 유지
    body_sections = extract_article_body_sections(soup)

    return {
        "title": title_text,
        "published_at": published_at,
        "source_url": source_url,
        "detail": {
            "summary_ko": rhea["summary_ko"],     # {"lang":"ko","text":"..."} or None
            "summary_en": rhea["summary_en"],
            "positive": rhea["positive"],    # list[str]
            "negative": rhea["negative"],    # list[str]
            "insights": rhea["insights"],    # list[str]
        },
        "body": body_sections,
    }


def extract_published_at(soup: BeautifulSoup) -> Optional[str]:
    # 날짜 포맷이 기사마다 달라서 여러 후보를 탐색
    # common patterns: time[datetime], meta[property='article:published_time'], 'Published' 텍스트 근처 등
    meta = soup.select_one("meta[property='article:published_time'], meta[name='pubdate'], time[datetime]")
    if meta:
        dt = meta.get("content") or meta.get("datetime")
        if dt:
            return dt
    # 백업: 페이지 내 텍스트 스캔
    for el in soup.find_all(string=True):
        t = (el or "").strip()
        if re.search(r"\b(Published|Updated)\b", t, re.I) and len(t) < 120:
            return t
    return None


def extract_source_url(soup: BeautifulSoup) -> Optional[str]:
    # “View source version on …” 링크를 찾기
    for a in soup.select("a[href]"):
        label = a.get_text(" ", strip=True).lower()
        if "view source" in label or "source version" in label:
            href = a.get("href")
            if href and href.startswith("http"):
                return href
    return None

def collect_multilang_summary(container: Tag) -> Dict[str, str]:
    """
    한 컨테이너 내 다국어 요약이 있는 경우(예: 영어/한국어/중국어 탭),
    언어 라벨을 휴리스틱으로 감지해 {lang_code: text} 로 반환.
    """
    text_map: Dict[str, str] = {}
    # 흔한 언어 라벨 텍스트
    lang_aliases = {
        "ko": ["korean", "한국어", "ko"],
        "en": ["english", "영어", "en"],
        "zh": ["chinese", "中文", "zh"],
        "ja": ["japanese", "日本語", "ja"],
        "es": ["spanish", "español", "es"],
    }

    # 컨테이너 인근에서 언어 라벨 + 본문 텍스트를 함께 수집
    # 구조가 제각각이라, 라벨 후보를 먼저 찾고 뒤따르는 텍스트를 묶는다
    labels = container.find_all(string=True)
    for i, raw in enumerate(labels):
        t = (raw or "").strip()
        if not t:
            continue
        lower = t.lower()
        for lang, keys in lang_aliases.items():
            if any(k in lower for k in keys):
                # 이 라벨 이후 텍스트 블록 모으기
                txt = collect_following_text(raw.parent, stop_labels=list(sum(lang_aliases.values(), [])), max_chars=1800)
                if txt:
                    text_map[lang] = txt
    # 언어 라벨이 안 보이면, 컨테이너 자체 텍스트를 통으로 취함(영어 가정)
    if not text_map:
        bulk = container.get_text(" ", strip=True)
        bulk = re.sub(r"\s{2,}", " ", bulk).strip()
        if bulk:
            text_map["en"] = bulk
    return text_map

def extract_article_body_sections(soup: BeautifulSoup) -> List[Dict]:
    """
    기사 본문을 '헤더/문단' 단위로 추출.
    - 헤더: h1~h4, strong/b(볼드) → type='header', level=1~4 또는 5
    - 문단/리스트: type='paragraph' / 'list_item'
    """
    container = find_main_article_container(soup) or soup

    sections: List[Dict] = []

    # 제목 계층
    for el in container.find_all(["h1", "h2", "h3", "h4", "p", "li", "strong", "b"]):
        name = el.name.lower()
        text = el.get_text(" ", strip=True)
        if not text or len(text) < 2:
            continue

        if name in {"h1", "h2", "h3", "h4"}:
            level = int(name[1])
            sections.append({"type": "header", "level": level, "text": text})
        elif name in {"strong", "b"}:
            # 강한 강조를 5레벨 헤더처럼 취급 (중복 방지)
            if not ends_with_punctuation(text):
                sections.append({"type": "header", "level": 5, "text": text})
        elif name == "li":
            sections.append({"type": "list_item", "text": text})
        else:
            sections.append({"type": "paragraph", "text": text})

    # 인접한 중복/노이즈 정리
    cleaned: List[Dict] = []
    prev = None
    for s in sections:
        if prev and s == prev:
            continue
        cleaned.append(s)
        prev = s

    return cleaned


def find_main_article_container(soup: BeautifulSoup) -> Optional[Tag]:
    # 흔한 본문 컨테이너 후보들
    selectors = [
        "article", ".article", ".post", ".news", "#content", ".content"
    ]
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            return node
    # 못 찾으면 None
    return None


def ends_with_punctuation(text: str) -> bool:
    return bool(re.search(r"[.!?…]$", text))


# ─────────────────────────────────────────────────────────────────────────────
# 실행 플로우(샘플)
# ─────────────────────────────────────────────────────────────────────────────
def run_once() -> List[Dict]:
    trending = fetch_trending_top7()
    curr_ids = [item["url"] for item in trending]
    prev_ids = load_prev_ids()

    new_ids = set(curr_ids) - prev_ids
    logging.info(f"Top7 total: {len(curr_ids)}, new_in_rank: {len(new_ids)}")

    results = []
    for item in trending:
        url = item["url"]
        detail = parse_article_detail(url)

        # Rhea-AI 우선순위: 상세 → 트렌딩 fallback
        summary_ko = detail["detail"]["summary_ko"]
        summary_en = detail["detail"]["summary_en"]
        # if not summary_ko and item.get("trending_summary"):
        #     summary_ko = {"lang": "ko", "text": translate_text(item["trending_summary"], "ko")}

        positives = detail["detail"]["positive"] or []
        # if not positives and item.get("trending_positive"):
        #     positives = [translate_text(x, "ko") for x in item["trending_positive"]]

        negatives = detail["detail"]["negative"] or []
        # if not negatives and item.get("trending_negative"):
        #     negatives = [translate_text(x, "ko") for x in item["trending_negative"]]

        insights = detail["detail"]["insights"] or []

        def _tko(s: Optional[str]) -> str:
            return translate_text(s, "ko") if s else ""
        
        if not summary_ko and summary_en and summary_en.get("text"):
            summary_ko = {"lang": "ko", "text": _tko(summary_en["text"])}
        
        positives_ko = [_tko(x) for x in positives] if positives else []
        negatives_ko = [_tko(x) for x in negatives] if negatives else []
        insights_ko = [_tko(x) for x in insights] if insights else []

        results.append({
            "rank": item["rank"],
            "ticker": item["ticker"],
            "title": detail["title"] or item["title"],
            "url": url,
            "published_at": detail["published_at"],
            "source_url": detail["source_url"],
            # 원문/영문 기반 블록
            "rhea_ai": {
                "summary": summary_en,          # {"lang":"en","text":...} or None
                "positive": positives,          # list[str] (영문/원문)
                "negative": negatives,          # list[str]
                "insights": insights            # list[str]
            },

            # 한국어 블록 (요약 + 불릿 전부 번역/보완)
            "rhea_ai_ko": {
                "summary":  summary_ko,         # {"lang":"ko","text":...} or None
                "positive": positives_ko,       # list[str] (ko)
                "negative": negatives_ko,       # list[str] (ko)
                "insights": insights_ko         # list[str] (ko)
            },

            "body": detail["body"],               # 섹션 리스트
            "is_new_in_rank": url in new_ids,     # 이번 주기에서 새로 진입했는가?
            "captured_at": datetime.now(timezone.utc).isoformat()
        })

    # 마지막에 Top7 URL 세트 갱신
    save_curr_ids(curr_ids)
    return results

def build_tg_message(d: Dict) -> str:
    lines = []
    ticker = d.get("ticker") or "-"
    title = (d.get("title") or "").strip()

    lines.append(f"🆕 [{ticker}] {title}")

def send_to_telegram(message: str):
    try:
        send_text_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

        response = requests.post(send_text_url, data={
            "chat_id": TELEGRAM_TEST_CHANNEL_ID,
            "text": message
        })
        response.raise_for_status()
        print("✅ 텍스트 전송 완료")

    except Exception as e:
        print("❌ 전송 실패:", e)
        print("📦 실패한 메시지:", message)

if __name__ == "__main__":

    while True:
        try:
            data = run_once()

            new_items = get_unseen_items(data)

            # 🔇 새 진입이 없으면 아무것도 출력하지 않고 다음 사이클로
            if not new_items:
                # time.sleep(600) 전에 바로 넘어감 (출력 없음)
                time.sleep(600)
                continue

            sent_urls_batch = []  # 이번 사이클에 실제 전송된 URL 누적

            # 콘솔 출력(요약)
            for d in new_items:

                ticker = d.get("ticker") or "-"
                title = (d.get("title") or "").strip()
                rs = (d.get("rhea_ai") or {}).get("summary") or {}
                summary_text = _truncate(rs.get("text", ""))
                positives = (d.get("rhea_ai") or {}).get("positive") or []
                negatives = (d.get("rhea_ai") or {}).get("negative") or []
                insights = (d.get("rhea_ai") or {}).get("insights") or []

                msg = (
                    f"🐦 원문\n\n"
                    f"🆕 [{ticker}] {title}\n"
                    f"📝 Summary\n"
                    f"   {summary_text}"
                    f"\n🟢 Positive\n"
                    f"{_bullets(positives)}"
                    f"\n🔴 Negative\n"
                    f"{_bullets(negatives)}"
                    f"\n💡 Insights\n"
                    f"{_bullets(insights)}"
                )

                title_ko = translate_text(title, "ko")
                rs_ko = (d.get("rhea_ai_ko") or {}).get("summary") or {}
                summary_text_ko = _truncate(rs_ko.get("text", ""))
                positives_ko = (d.get("rhea_ai_ko") or {}).get("positive") or []
                negatives_ko = (d.get("rhea_ai_ko") or {}).get("negative") or []
                insights_ko = (d.get("rhea_ai_ko") or {}).get("insights") or []

                msg_ko = (
                    f"🌐 번역\n\n"
                    f"🆕 [{ticker}] {title_ko}\n"
                    f"📝 Summary\n"
                    f"   {summary_text_ko}"
                    f"\n🟢 Positive\n"
                    f"{_bullets(positives_ko)}"
                    f"\n🔴 Negative\n"
                    f"{_bullets(negatives_ko)}"
                    f"\n💡 Insights\n"
                    f"{_bullets(insights_ko)}"
                    f"\n\n🔗 URL\n"
                    f"{d['url']}"
                )

                # combined = f"{msg}\n\n{'─'*24}\n\n{msg_ko}"

                send_to_telegram(msg_ko)

                #send_to_telegram(msg)

                # ③ 전송 성공한 URL을 배치에 모아둠
                sent_urls_batch.append(d["url"])

            # ④ 한 번에 recent_urls 업데이트(중복 방지, 최대 100)
            if sent_urls_batch:
                add_recent_seen(sent_urls_batch)
                
                # # ─────────────────────────────
                # # ① 제목
                # # ─────────────────────────────
                # print(f"🆕 [{ticker}] {title}")

                # # ─────────────────────────────
                # # ② Summary
                # # ─────────────────────────────
                # if rs and rs.get("text"):
                #     print("📝 Summary:")
                #     print("   " + _truncate(rs["text"]))

                # # ─────────────────────────────
                # # ③ Positive (🟢)
                # # ─────────────────────────────
                # if positives:
                #     print(f"🟢 Positive:")
                #     _print_bullets(positives)

                # # ─────────────────────────────
                # # ④ Negative (🔴)
                # # ─────────────────────────────
                # if negatives:
                #     print(f"🔴 Negative:")
                #     _print_bullets(negatives)

                # # ─────────────────────────────
                # # ⑤ Insights (💡)
                # # ─────────────────────────────
                # if insights:
                #     print(f"💡 Insights:")
                #     _print_bullets(insights)

                # # ─────────────────────────────
                # # ⑥ Links
                # # ─────────────────────────────
                # print()
                # if d.get("url"):
                #     print(f"🔗 {d['url']}")

                # # ─────────────────────────────
                # # 기사 간 구분선
                # # ─────────────────────────────
                # print()

        except KeyboardInterrupt:
            print("\n⏹️ Stopped by user.")
            break
        except Exception as e:
            logging.exception("cycle error")   # 전체 스택 출력
            # 에러 시도 조용히 대기 후 재시도 (원하면 로그로 바꿔도 됨)
            # print(f"[WARN] cycle error: {e}")
            time.sleep(600)
            continue

        # 다음 사이클까지 10분 대기
        time.sleep(600)