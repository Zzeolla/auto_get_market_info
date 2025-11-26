# ms_market_trends_crawler.py
import os
import json
import time
import logging
from typing import Dict, List, Optional
from urllib.parse import urljoin
from dotenv import load_dotenv
import re
import requests
from bs4 import BeautifulSoup, Tag
from openai import OpenAI

# ─────────────────────────────────────────
# 환경 변수
# ─────────────────────────────────────────
load_dotenv()

MARKET_TRENDS_URL = "https://www.morganstanley.com/insights/topics/market-trends"
BASE_URL = "https://www.morganstanley.com"
MS_API_URL = "https://www.morganstanley.com/insights/topics/market-trends.insights-automation.json?search=recirculationgrid"

STATE_FILE = "ms_market_trends_state.json"   # 최근 전송 URL 저장용
MAX_TG_CHARS = 3800                          # 텔레그램 메시지 전체 길이 제한
MAX_TOTAL = 3800

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # 필요시 바꿔도 됨

client = OpenAI(api_key=OPENAI_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

API_URL = "https://www.morganstanley.com/insights/topics/market-trends.insights-automation.json?search=recirculationgrid"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s"
)

# ─────────────────────────────────────────
# 상태 저장/로드 (recent_urls)
# ─────────────────────────────────────────
def _load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"recent_urls": {}}

def _save_state(state: Dict) -> None:
    state["updated_at"] = time.time()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_recent_seen() -> Dict[str, float]:
    st = _load_state()
    recent = st.get("recent_urls", {})
    if isinstance(recent, list):
        # 혹시 예전 형식이면 딕셔너리로 변환
        recent = {u: time.time() for u in recent}
        st["recent_urls"] = recent
        _save_state(st)
    return recent

def add_recent_seen(urls: List[str]) -> None:
    st = _load_state()
    recent: Dict[str, float] = st.get("recent_urls", {})
    now = time.time()
    for u in urls:
        recent[u] = now
    st["recent_urls"] = recent
    _save_state(st)

def is_seen(url: str) -> bool:
    recent = load_recent_seen()
    return url in recent

# ─────────────────────────────────────────
# OpenAI 요약 + 번역
# ─────────────────────────────────────────
def summarize_and_translate(text: str, max_chars: int = 3800) -> str:
    """
    기본 max_chars는 호출할 때 사용
    """
    prompt = f"""
        다음 영어(또는 외국어) 텍스트를 한국어로 요약하고 번역해줘.

        요구사항:
        - 핵심 내용만 남기고 군더더기 제거
        - 중요한 숫자, 인물, 이벤트, 날짜는 유지
        - 원문이 기사 형식일 경우 제목은 살려주는 방향으로
        - 최종 결과는 반드시 {max_chars}자 이내로 작성
        - 문어체, 자연스러운 한국어로 작성

        원문:
        {text}
    """

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "너는 전문 번역가이자 요약가야."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    answer = resp.choices[0].message.content.strip()

    # 혹시 모델이 max_chars 넘기면 강제로 잘라주기
    if len(answer) > max_chars:
        answer = answer[:max_chars]

    return answer

def translate_takeaways(takeaways: list[str], max_chars: int = 1200) -> str:
    """
    Key Takeaways용 번역기.
    - bullet 개수와 순서를 그대로 유지
    - 요약/삭제/합치기 금지
    """
    if not takeaways:
        return ""

    src = "\n".join(f"- {t}" for t in takeaways)

    prompt = f"""
        다음은 영어로 된 bullet point 목록이야.

        요구사항:
        - bullet 개수와 순서를 절대 바꾸지 말 것
        - 어떤 항목도 합치거나 삭제하지 말 것
        - 각 줄은 '- '로 시작하는 형태를 유지
        - 오직 한국어 번역만 수행 (요약/설명 추가 금지)
        - 최대 길이는 {max_chars}자 이내

        원문 bullet 목록:
        {src}
    """

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "너는 요약하지 않고 원문 구조를 그대로 유지하는 전문 번역가야."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )

    answer = resp.choices[0].message.content.strip()
    if len(answer) > max_chars:
        answer = answer[:max_chars]
    return answer

def translate_title(text: str, max_chars: int = 200) -> str:
    if not text:
        return ""
    prompt = f"""
        다음 영어 제목을 한국어로 번역해줘.

        요구사항:
        - 요약/의역하지 말고 제목 전체를 자연스럽게 번역
        - 문장 수를 늘리지 말고 한 줄로 써줘
        - 최대 {max_chars}자 이내

        제목:
        {text}
    """
    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "너는 제목을 그대로 번역하는 전문 번역가야."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    answer = resp.choices[0].message.content.strip()
    if len(answer) > max_chars:
        answer = answer[:max_chars]
    return answer

# ─────────────────────────────────────────
# 텔레그램 전송
# ─────────────────────────────────────────
def send_to_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logging.warning("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHANNEL_ID가 설정되지 않았습니다.")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHANNEL_ID,
            "text": message,
        }, timeout=20)
        resp.raise_for_status()
        logging.info("✅ 텔레그램 전송 완료")
    except Exception as e:
        logging.exception(f"❌ 텔레그램 전송 실패: {e}")
        logging.error("📦 실패한 메시지 일부: %s", message[:200])

# ─────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────
def normalize_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return urljoin(BASE_URL, href)

def get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")

def fetch_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")

# ─────────────────────────────────────────
# 1) 목록 페이지 파싱
# ─────────────────────────────────────────
def fetch_listing() -> List[Dict]:
    """
    Market Trends 자동화 JSON(API)에서
    카드 리스트를 가져와서 상위 4개만 article/podcast로 반환.

    반환: [{title, url, kind}, ...]
        kind ∈ {"article", "podcast"}
    """
    resp = requests.get(MS_API_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list):
        logging.warning("예상과 다른 JSON 구조입니다: %s", type(data))
        return []

    items: List[Dict] = []

    for row in data:
        if not isinstance(row, dict):
            continue

        page_url = row.get("pageUrl") or ""
        title = (row.get("title") or "").strip()
        media_type = row.get("mediaType") or ""

        if not page_url or not title:
            continue

        # 전체 URL 보정
        url = page_url if page_url.startswith("http") else normalize_url(page_url)

        # article / podcast 판별
        if "/insights/articles/" in url:
            kind = "article"
        elif "/insights/podcasts/" in url or "podcast" in media_type:
            kind = "podcast"
        else:
            # 우리가 원하는 컨텐츠 타입이 아니면 패스
            continue

        items.append({
            "title": title,
            "url": url,
            "kind": kind,
        })

        # ✅ 최대 4개만 사용
        if len(items) >= 4:
            break

    logging.info("목록에서 감지된 항목 수: %d", len(items))
    return items

# ─────────────────────────────────────────
# 2) 상세 페이지 파싱 - Articles
# ─────────────────────────────────────────
def extract_text_from_element(el: Optional[Tag]) -> str:
    if not el:
        return ""
    text = el.get_text("\n", strip=True)
    # 공백 정리
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text

def parse_article_page(url: str) -> Dict[str, str]:
    """
    insights/articles/ 페이지에서
    - key takeaways (ul)
    - 해당 블록 내 기사 본문
    을 추출해서 문자열로 돌려줌.
    """
    soup = get_soup(url)

    # Key Takeaways 블록
    # CSS: #maincontent > div > div.takeaway.colorBar_msBlue... > ... > .cmp-takeaway_description
    takeaways_container = soup.select_one(
        "#maincontent div.takeaway.colorBar_msBlue.aem-GridColumn.aem-GridColumn--default--12 "
        "div.cmp-takeaway_description"
    )
    # 그 안의 <ul> 텍스트
    key_takeaways_text = ""
    if takeaways_container:
        ul = takeaways_container.find("ul")
        if ul:
            key_takeaways_text = extract_text_from_element(ul)
        else:
            key_takeaways_text = extract_text_from_element(takeaways_container)

    # 같은 takeaway 블록 전체를 기사 본문 컨테이너로 사용
    article_block = soup.select_one(
        "#maincontent div.takeaway.colorBar_msBlue.aem-GridColumn.aem-GridColumn--default--12"
    )
    article_body_text = extract_text_from_element(article_block)

    return {
        "key_takeaways": key_takeaways_text,
        "body": article_body_text,
    }

def extract_article_content(url: str) -> tuple[str, list[str], str]:
    """
    Morgan Stanley article 페이지에서
    - title
    - Key Takeaways (영문 리스트)
    - 본문 텍스트 (영문)
    를 뽑는다.
    """
    soup = fetch_soup(url)

    # 제목
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # Key Takeaways 헤더 찾기 (h2/h3/h4 중 'Key Takeaways' 텍스트 포함)
    kt_header = None
    for tag in soup.find_all(["h2", "h3", "h4"]):
        if "key takeaways" in tag.get_text(strip=True).lower():
            kt_header = tag
            break

    takeaways: list[str] = []
    if kt_header:
        ul = kt_header.find_next("ul")
        if ul:
            takeaways = [
                li.get_text(" ", strip=True)
                for li in ul.find_all("li")
                if li.get_text(strip=True)
            ]

    # 본문: Key Takeaways 이후의 p / h3 / h4 들을 다 긁어오기
    body_parts: list[str] = []
    start_node: Tag | None = kt_header or h1

    if start_node:
        for node in start_node.find_all_next():
            if not isinstance(node, Tag):
                continue

            # Footer/뉴스레터 영역에서 멈추기
            txt = node.get_text(" ", strip=True)
            low = txt.lower()
            if any(
                stop in low
                for stop in [
                    "sign up to get morgan stanley",  # 뉴스레터
                    "thank you for subscribing",      # 구독 완료
                    "©",                              # 푸터
                ]
            ):
                break

            if node.name == "p":
                if txt and len(txt) > 20:
                    body_parts.append(txt)

            # 소제목(h3/h4)도 붙여줌 (원하면 주석 처리 가능)
            elif node.name in {"h2", "h3", "h4"}:
                if "key takeaways" in low:
                    continue  # 이미 위에서 쓴 헤더라 스킵
                if txt:
                    body_parts.append(txt)

    body_text = "\n".join(body_parts)

    return title, takeaways, body_text


# ─────────────────────────────────────────
# 3) 상세 페이지 파싱 - Podcasts (Transcript)
# ─────────────────────────────────────────
def parse_podcast_page(url: str) -> str:
    """
    insights/podcasts/ 페이지에서 transcript 텍스트를 추출.
    - 주요 컨테이너: #generic-expandable-content-0 > div
    """
    soup = get_soup(url)

    transcript_div = soup.select_one("#generic-expandable-content-0 > div")
    if not transcript_div:
        # 혹시 구조 변경 대비: id 만으로 한 번 더
        transcript_div = soup.select_one("#generic-expandable-content-0")

    transcript_text = extract_text_from_element(transcript_div)
    return transcript_text

def extract_podcast_transcript(url: str) -> tuple[str, str]:
    """
    Thoughts on the Market 페이지에서
    Featured Episode 제목 + Transcript 본문을 뽑는다.
    """
    soup = fetch_soup(url)

    # 최상단 큰 제목
    h1 = soup.find("h1")
    page_title = h1.get_text(strip=True) if h1 else "Thoughts on the Market Podcast"

    # Featured Episode 제목
    featured_title = None
    for tag in soup.find_all(["h2", "h3"]):
        if "featured episode" in tag.get_text(strip=True).lower():
            # 바로 다음에 오는 h3 안에 에피소드 제목 링크가 있음
            ep_h3 = tag.find_next("h3")
            if ep_h3:
                featured_title = ep_h3.get_text(" ", strip=True)
            break

    if not featured_title:
        featured_title = page_title

    # Transcript 헤더 찾기
    transcript_header = None
    for tag in soup.find_all(["h2", "h3", "h4"]):
        if "transcript" in tag.get_text(strip=True).lower():
            transcript_header = tag
            break

    transcript_parts: list[str] = []
    if transcript_header:
        for node in transcript_header.find_all_next():
            if not isinstance(node, Tag):
                continue

            # 다음 섹션(예: Latest Episodes)에서 멈춤
            if node.name in {"h2", "h3", "h4"}:
                head = node.get_text(strip=True).lower()
                if any(
                    kw in head
                    for kw in ["latest episodes", "more from thoughts", "you might also like"]
                ):
                    break

            if node.name == "p":
                txt = node.get_text(" ", strip=True)
                if txt:
                    transcript_parts.append(txt)

    transcript_text = "\n".join(transcript_parts)

    # 제목 + 본문 합친 텍스트(요약용)
    full_title = f"{page_title} - {featured_title}"
    return full_title, transcript_text

# ─────────────────────────────────────────
# 4) 메시지 생성 & 길이 제어
# ─────────────────────────────────────────
def build_message(title: str, summary_ko: str, url: str) -> str:
    """
    제목 + 요약 + URL을 합쳐 텔레그램 메시지 생성.
    전체 길이가 MAX_TG_CHARS를 넘으면 summary 부분을 잘라준다.
    """
    header = f"📈 Morgan Stanley Market Trends\n\n{title.strip()}\n\n"
    footer = f"\n\n🔗 {url}"

    # summary 여유 공간 계산
    max_summary_len = MAX_TG_CHARS - len(header) - len(footer)
    if max_summary_len <= 0:
        # 극단적인 경우지만, 제목/링크만 보내기
        logging.warning("요약을 넣을 공간이 없습니다. 제목과 링크만 전송합니다.")
        return (header + footer)[:MAX_TG_CHARS]

    if len(summary_ko) > max_summary_len:
        summary_ko = summary_ko[: max_summary_len - 3] + "..."

    message = header + summary_ko + footer
    # 혹시라도 계산 오차로 넘칠 수 있으니 마지막 방어
    if len(message) > MAX_TG_CHARS:
        message = message[:MAX_TG_CHARS]
    return message

def build_article_message(url: str) -> str:
    title_en, takeaways_en, body_en = extract_article_content(url)

    # 🔹 제목 한국어 번역
    title_ko = translate_title(title_en)

    # 🔹 Key Takeaways는 요약 없이 그대로 번역
    takeaways_ko = translate_takeaways(takeaways_en, max_chars=1200)

    header = (
        "📈 Morgan Stanley Market Trends\n\n"
        f"{title_ko}\n"
        f"{title_en}\n\n"
    )
    tail = f"\n\n🔗 {url}\n"

    # 이 둘을 제외하고 요약에 쓸 수 있는 최대 길이
    remain_for_summary = MAX_TOTAL - len(header) - len(tail) - len(takeaways_ko) - 20
    if remain_for_summary < 600:
        remain_for_summary = 600  # 최소 요약 분량 확보

    # 2) 본문 요약
    summary_ko = summarize_and_translate(body_en, max_chars=remain_for_summary)

    msg = header + "📌 Key Takeaways\n" + takeaways_ko + "\n\n📝 Summary\n" + summary_ko + tail

    # 혹시라도 3800 넘겼으면 한 번 더 자르기
    if len(msg) > MAX_TOTAL:
        msg = msg[:MAX_TOTAL - 3] + "..."

    return msg


def build_podcast_message(url: str) -> str:
    title_en, transcript_en = extract_podcast_transcript(url)

    # 🔹 제목 한국어 번역
    title_ko = translate_title(title_en)

    # ✅ 헤더: 한글 제목 + 영어 제목
    header = (
        "📈 Morgan Stanley Market Trends\n\n"
        f"{title_ko}\n"
        f"{title_en}\n\n"
    )
    tail = f"\n\n🔗 {url}\n"

    remain_for_summary = MAX_TOTAL - len(header) - len(tail) - 20
    if remain_for_summary < 600:
        remain_for_summary = 600

    summary_ko = summarize_and_translate(transcript_en, max_chars=remain_for_summary)

    msg = header + summary_ko + tail
    if len(msg) > MAX_TOTAL:
        msg = msg[:MAX_TOTAL - 3] + "..."
    return msg


# ─────────────────────────────────────────
# 5) 한 번 실행 (새 글만 처리)
# ─────────────────────────────────────────
def run_once() -> None:
    items = fetch_listing()
    if not items:
        logging.info("목록에서 아무것도 찾지 못했습니다.")
        return

    for item in reversed(items):
        url = item["url"]
        kind = item["kind"]

        if is_seen(url):
            continue  # 이미 전송한 URL

        logging.info("새 항목 감지: [%s] %s", kind, url)

        try:
            if kind == "article":
                msg = build_article_message(url)

            elif kind == "podcast":
                msg = build_podcast_message(url)

            else:
                # 현재는 article/podcast 외엔 없음
                continue

            logging.info("메시지 최종 길이: %d chars", len(msg))

            print(msg)

            # send_to_telegram(msg)
            add_recent_seen([url])

            # 너무 잦은 요청을 피하기 위해 항목 사이 약간 쉬어가기
            time.sleep(3)

        except Exception:
            logging.exception("항목 처리 중 오류: %s", url)
            # 오류 나도 다른 항목은 계속

# ─────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────
if __name__ == "__main__":
    logging.info("Morgan Stanley Market Trends 크롤러 시작")

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            logging.info("사용자에 의해 중단되었습니다.")
            break
        except Exception:
            logging.exception("주기 실행 중 오류 발생")

        # 1시간 대기 후 다시
        logging.info("다음 체크까지 1시간 대기...")
        time.sleep(3600)
