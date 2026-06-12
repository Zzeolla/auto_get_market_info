import os
import time
import json
import logging
from typing import List
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup, Tag

# Selenium + undetected_chromedriver
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# OpenAI
from openai import OpenAI


# ─────────────────────────────────────────────
# 환경 변수
# ─────────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
client = OpenAI(api_key=OPENAI_API_KEY)

BASE_URL      = "https://www.goldmansachs.com"
INSIGHTS_URL  = "https://www.goldmansachs.com/insights"
STATE_FILE    = "gs_insights_state.json"

MAX_TOTAL = 3800

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ─────────────────────────────────────────────
# Selenium 드라이버 초기화 (undetected_chromedriver)
# ─────────────────────────────────────────────
def init_driver():
    options = uc.ChromeOptions()
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    driver = uc.Chrome(options=options)
    driver.set_page_load_timeout(30)
    return driver


driver = init_driver()
wait   = WebDriverWait(driver, 20)


# ─────────────────────────────────────────────
# 상태 파일 로드 및 저장
# ─────────────────────────────────────────────
EXPIRY_SECONDS = 90 * 24 * 3600   # 90일 지난 URL 자동 삭제

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"recent_urls": {}}

def save_state(state: dict):
    state["updated_at"] = time.time()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def is_seen(url: str) -> bool:
    st = load_state()
    return url in st.get("recent_urls", {})

def add_seen(urls: List[str]):
    st   = load_state()
    now  = time.time()
    recent: dict = st.get("recent_urls", {})
    for u in urls:
        recent[u] = now
    # 오래된 URL 정리
    recent = {u: t for u, t in recent.items() if now - t < EXPIRY_SECONDS}
    st["recent_urls"] = recent
    save_state(st)


# ─────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────
def normalize_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return BASE_URL + href

def extract_text(el: Tag | None) -> str:
    if not el:
        return ""
    txt = el.get_text("\n", strip=True)
    return "\n".join(line.strip() for line in txt.splitlines() if line.strip())


# ─────────────────────────────────────────────
# Soup 가져오기
# 목록 페이지 → requests (빠름)
# 상세 페이지 → requests 먼저 시도, 막히면 Selenium 폴백
# ─────────────────────────────────────────────
def get_soup_requests(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")

def get_soup_selenium(url: str, wait_selector: str = None) -> BeautifulSoup:
    driver.get(url)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1.5)
    if wait_selector:
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector)))
        except TimeoutException:
            logging.warning(f"[GS] 로딩 대기 실패: {wait_selector}")
    return BeautifulSoup(driver.page_source, "html.parser")

def get_soup(url: str, wait_selector: str = None) -> BeautifulSoup:
    """requests 먼저 시도 → 403/차단 시 Selenium 폴백"""
    try:
        soup = get_soup_requests(url)
        # GS가 로그인 리다이렉트 등으로 내용 없을 때 체크
        if soup.find("h1"):
            return soup
        logging.info("[GS] requests 응답 비정상 → Selenium 폴백")
    except Exception as e:
        logging.info(f"[GS] requests 실패({e}) → Selenium 폴백")
    return get_soup_selenium(url, wait_selector)


# ─────────────────────────────────────────────
# 1) 목록 파싱 - "The Latest" 섹션
# ─────────────────────────────────────────────
def fetch_listing() -> List[dict]:
    """
    goldmansachs.com/insights 의 'The Latest' 섹션에서
    article / podcast 링크 최대 5개 반환.

    반환: [{title, url, category, kind, date}, ...]
    """
    try:
        soup = get_soup(INSIGHTS_URL)
    except Exception as e:
        logging.error(f"[GS] 목록 페이지 로드 실패: {e}")
        return []

    items  = []
    seen   = set()

    # GS Insights 페이지 구조:
    # "The Latest" 텍스트 근처 링크들이 /insights/articles/ 또는
    # /insights/goldman-sachs-exchanges/ 등 경로를 가짐
    # hero-card-grid 텍스트 패턴: [category][title][Podcast|날짜]

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue

        # GS Insights 내부 링크만 (talks-at-gs는 금융/경제 무관 콘텐츠 제외)
        if not (
            "/insights/articles/" in href
            or "/insights/goldman-sachs-exchanges/" in href
            or "/insights/the-markets/" in href
        ):
            continue

        url = normalize_url(href)
        if url in seen:
            continue

        full_text = a.get_text(" ", strip=True)
        if len(full_text) < 10:
            continue

        # kind 판단
        # exchanges/the-markets 경로는 팟캐스트, 텍스트에 "Podcast" 있어도 팟캐스트
        if (
            "/insights/goldman-sachs-exchanges/" in href
            or "/insights/the-markets/" in href
            or "Podcast" in full_text
        ):
            kind = "podcast"
        else:
            kind = "article"

        # 날짜 추출 (예: "Apr 30, 2026")
        import re
        date_match = re.search(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", full_text)
        date = date_match.group(0) if date_match else ""

        # 카테고리 + 제목 분리
        # full_text 예시: "Artificial Intelligence Robotaxis Are Forecast... Apr 30, 2026"
        # 날짜/Podcast 제거 후 남은 텍스트에서 제목 추출
        clean = full_text
        if date:
            clean = clean.replace(date, "").strip()
        clean = clean.replace("Podcast|", "").replace("Podcast", "").strip()

        # 첫 줄 = 카테고리, 나머지 = 제목 (보통)
        lines = [l.strip() for l in clean.split("  ") if l.strip()]
        if len(lines) >= 2:
            category = lines[0]
            title    = " ".join(lines[1:]).strip()
        else:
            category = ""
            title    = clean

        if not title:
            continue

        items.append({
            "title":    title,
            "url":      url,
            "category": category,
            "kind":     kind,
            "date":     date,
        })
        seen.add(url)

        if len(items) >= 5:
            break

    logging.info(f"[GS] 목록 감지: {len(items)}개")
    for i, it in enumerate(items, 1):
        logging.info(f"[GS] #{i} [{it['kind']}] {it['title']} | {it['url']}")

    return items


# ─────────────────────────────────────────────
# 2) 상세 페이지 파싱
# ─────────────────────────────────────────────
def extract_article(url: str) -> tuple[str, str, list[str], str]:
    """
    반환: (title, date, takeaways, body_text)
    """
    soup = get_soup(url, wait_selector="h1")

    # 제목
    h1    = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # 날짜
    import re
    date = ""
    for tag in soup.find_all(["p", "span", "div", "time"]):
        txt = tag.get_text(strip=True)
        if re.match(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", txt):
            date = txt
            break

    # Key Takeaways
    # GS 구조: h1 → img → ul(takeaways) 순서
    # 단, 네비게이션 ul과 구분하기 위해 h1 이후 최초 li 개수 3개 이상인 ul만 사용
    takeaways: list[str] = []
    if h1:
        for ul in h1.find_all_next("ul"):
            lis = [li.get_text(" ", strip=True) for li in ul.find_all("li", recursive=False) if li.get_text(strip=True)]
            # 네비게이션 ul은 항목이 매우 많음(10개+), takeaways는 보통 3~6개
            if 2 <= len(lis) <= 8:
                takeaways = lis
                break

    # 본문: h1 이후 <p> 태그 수집
    # stop 조건을 본문 끝 명확한 패턴으로만 한정
    body_parts: list[str] = []
    if h1:
        for node in h1.find_all_next():
            if not isinstance(node, Tag):
                continue

            txt = node.get_text(" ", strip=True)
            low = txt.lower()

            # 푸터/뉴스레터 영역에서 중단 (본문과 겹치지 않는 패턴만)
            if any(stop in low for stop in [
                "subscribe to briefings",
                "© 2026 goldman sachs",
                "related tags",
            ]):
                break

            if node.name == "p" and len(txt) > 40:
                # 면책 조항 문단은 제외
                if "being provided for educational purposes only" in low:
                    continue
                if "does not constitute a recommendation" in low:
                    continue
                body_parts.append(txt)
            elif node.name in {"h2", "h3", "h4"} and txt:
                body_parts.append(f"\n[{txt}]")

    body_text = "\n".join(body_parts)
    return title, date, takeaways, body_text


def extract_podcast(url: str) -> tuple[str, str, str]:
    """
    Podcast 페이지에서 반환: (title, date, transcript)
    """
    soup = get_soup(url, wait_selector="h1")

    h1    = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    import re
    date = ""
    for tag in soup.find_all(["p", "span", "div", "time"]):
        txt = tag.get_text(strip=True)
        if re.match(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", txt):
            date = txt
            break

    # Transcript 섹션
    transcript_parts: list[str] = []
    transcript_header = None
    for tag in soup.find_all(["h2", "h3", "h4"]):
        if "transcript" in tag.get_text(strip=True).lower():
            transcript_header = tag
            break

    if transcript_header:
        for node in transcript_header.find_all_next():
            if not isinstance(node, Tag):
                continue
            txt = node.get_text(" ", strip=True)
            low = txt.lower()
            if any(kw in low for kw in [
                "subscribe to briefings", "© 2026", "privacy policy"
            ]):
                break
            if node.name == "p" and txt:
                transcript_parts.append(txt)

    transcript = "\n".join(transcript_parts)
    return title, date, transcript


# ─────────────────────────────────────────────
# GPT 번역/요약
# ─────────────────────────────────────────────
def translate_title(text: str) -> str:
    if not text:
        return ""
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.1,
        messages=[
            {"role": "system", "content": "너는 제목을 그대로 번역하는 전문 번역가야."},
            {"role": "user", "content": f"다음 영어 제목을 한국어로 자연스럽게 번역해줘. 한 줄로.\n\n{text}"},
        ]
    )
    return resp.choices[0].message.content.strip()

def translate_takeaways(takeaways: list[str]) -> str:
    if not takeaways:
        return ""
    src = "\n".join(f"- {t}" for t in takeaways)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.1,
        messages=[
            {"role": "system", "content": "너는 bullet 구조를 그대로 유지하는 번역가야."},
            {"role": "user", "content": f"다음 bullet 목록을 한국어로 번역해줘. 개수·순서 유지, '- '형식 유지.\n\n{src}"},
        ]
    )
    return resp.choices[0].message.content.strip()

def summarize_ko(text: str, max_chars: int = 2000) -> str:
    if not text:
        return "[본문 없음]"
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[
            {"role": "system", "content": "너는 금융·경제 기사 한국어 요약 전문가야."},
            {"role": "user", "content": (
                f"다음 영어 텍스트를 한국어로 요약 번역해줘.\n"
                f"- 핵심 내용, 숫자, 인물, 날짜 유지\n"
                f"- {max_chars}자 이내\n"
                f"- 자연스러운 문어체\n\n{text}"
            )},
        ]
    )
    out = resp.choices[0].message.content.strip()
    return out[:max_chars] if len(out) > max_chars else out


# ─────────────────────────────────────────────
# 텔레그램 전송
# ─────────────────────────────────────────────
def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logging.warning("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHANNEL_ID 미설정")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHANNEL_ID, "text": msg},
            timeout=15,
        )
        resp.raise_for_status()
        logging.info("✅ 텔레그램 전송 완료")
    except Exception as e:
        logging.error(f"❌ 텔레그램 전송 실패: {e}")
        logging.error("실패 메시지 일부: %s", msg[:200])


# ─────────────────────────────────────────────
# 메시지 생성
# ─────────────────────────────────────────────
def build_article_message(item: dict) -> str:
    url      = item["url"]
    category = item.get("category", "")
    date     = item.get("date", "")

    title_en, date_parsed, takeaways_en, body_en = extract_article(url)

    # 파싱된 날짜 우선, 없으면 목록에서 가져온 날짜
    date = date_parsed or date
    if not title_en:
        title_en = item.get("title", "")

    title_ko     = translate_title(title_en)
    takeaways_ko = translate_takeaways(takeaways_en)

    header = "📈 Goldman Sachs Insights\n\n"
    if category:
        header += f"📂 {category}\n"
    header += f"{title_ko}\n{title_en}\n"
    if date:
        header += f"📅 {date}\n"
    header += "\n"

    tail   = f"\n\n🔗 {url}\n"

    # takeaways 최대 600자로 제한
    if len(takeaways_ko) > 600:
        takeaways_ko = takeaways_ko[:600] + "..."

    # 요약은 최대 800자로 고정
    summary_ko = summarize_ko(body_en, max_chars=800)

    sections = ""
    if takeaways_ko:
        sections += f"📌 Key Takeaways\n{takeaways_ko}\n\n"
    sections += f"📝 요약\n{summary_ko}"

    msg = header + sections + tail
    if len(msg) > MAX_TOTAL:
        msg = msg[:MAX_TOTAL - 3] + "..."
    return msg


def build_podcast_message(item: dict) -> str:
    url      = item["url"]
    category = item.get("category", "")
    date     = item.get("date", "")

    title_en, date_parsed, transcript_en = extract_podcast(url)

    date = date_parsed or date
    if not title_en:
        title_en = item.get("title", "")

    title_ko = translate_title(title_en)

    header = "📈 Goldman Sachs Insights\n\n"
    if category:
        header += f"📂 {category} (Podcast)\n"
    header += f"{title_ko}\n{title_en}\n"
    if date:
        header += f"📅 {date}\n"
    header += "\n"

    tail   = f"\n\n🔗 {url}\n"
    # 요약은 최대 800자로 고정
    summary_ko = summarize_ko(transcript_en, max_chars=800)

    msg = header + f"📝 요약\n{summary_ko}" + tail
    if len(msg) > MAX_TOTAL:
        msg = msg[:MAX_TOTAL - 3] + "..."
    return msg


# ─────────────────────────────────────────────
# 한 번 실행 (새 기사만 전송)
# ─────────────────────────────────────────────
def run_once():
    items = fetch_listing()
    if not items:
        logging.info("[GS] 목록 비어 있음")
        return

    first_run = not os.path.exists(STATE_FILE)
    if first_run:
        items = items[:3]
        logging.info(f"[GS] 첫 실행: 최신 {len(items)}개만 전송")

    for item in reversed(items):   # 오래된 글부터 전송
        url = item["url"]

        if (not first_run) and is_seen(url):
            continue

        logging.info(f"[GS] 새 항목: [{item['kind']}] {url}")

        try:
            if item["kind"] == "podcast":
                msg = build_podcast_message(item)
            else:
                msg = build_article_message(item)

            logging.info(f"[GS] 메시지 길이: {len(msg)}자")
            send_telegram(msg)
            add_seen([url])
            time.sleep(3)

        except Exception as e:
            logging.error(f"[GS] 기사 처리 오류: {e}")


# ─────────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logging.info("Goldman Sachs Insights 크롤러 시작")

    try:
        while True:
            try:
                run_once()
            except Exception as e:
                logging.error(f"주기 실행 오류: {e}")

            logging.info("다음 실행까지 1시간 대기...")
            time.sleep(3600)

    finally:
        try:
            driver.quit()
        except Exception:
            pass