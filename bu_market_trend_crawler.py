import os
import re
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

BASE_URL     = "https://home.barclays"
LISTING_URL  = "https://home.barclays/insights/uk-unlocked/"
STATE_FILE   = "barclays_ukunlocked_state.json"

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
# Selenium 드라이버 초기화
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
# 상태 파일 (중복 전송 방지)
# ─────────────────────────────────────────────
EXPIRY_SECONDS = 90 * 24 * 3600

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
    return url in load_state().get("recent_urls", {})

def add_seen(urls: List[str]):
    st    = load_state()
    now   = time.time()
    recent: dict = st.get("recent_urls", {})
    for u in urls:
        recent[u] = now
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
# requests 먼저 → 봇 감지 시 Selenium 폴백
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
            logging.warning(f"[Barclays] 로딩 대기 실패: {wait_selector}")
    return BeautifulSoup(driver.page_source, "html.parser")

def get_soup(url: str, wait_selector: str = None) -> BeautifulSoup:
    """requests 먼저 시도 → 실패 시 Selenium 폴백"""
    try:
        soup = get_soup_requests(url)
        if soup.find("h1"):
            return soup
        logging.info("[Barclays] requests 응답 비정상 → Selenium 폴백")
    except Exception as e:
        logging.info(f"[Barclays] requests 실패({e}) → Selenium 폴백")
    return get_soup_selenium(url, wait_selector)


# ─────────────────────────────────────────────
# 1) 목록 파싱 - "Latest insights" 섹션
# ─────────────────────────────────────────────
def fetch_listing() -> List[dict]:
    """
    /insights/uk-unlocked/ 의 'Latest insights' 섹션에서
    기사 카드를 파싱해 최대 6개 반환.

    반환: [{title, url, description, category}, ...]
    """
    try:
        soup = get_soup(LISTING_URL)
    except Exception as e:
        logging.error(f"[Barclays] 목록 페이지 로드 실패: {e}")
        return []

    items = []
    seen  = set()

    # "Latest insights" h2/h3 찾기
    latest_header = None
    for tag in soup.find_all(["h2", "h3"]):
        if "latest insights" in tag.get_text(strip=True).lower():
            latest_header = tag
            break

    if not latest_header:
        logging.warning("[Barclays] 'Latest insights' 섹션을 찾지 못했습니다.")
        # 헤더 못 찾아도 전체에서 /insights/YYYY/ 패턴 링크 수집
        search_root = soup
    else:
        search_root = latest_header

    for a in search_root.find_all_next("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue

        # Barclays 아티클 URL 패턴: /insights/YYYY/MM/기사제목/
        if not re.search(r"/insights/\d{4}/\d{2}/", href):
            continue

        url = normalize_url(href)
        if url in seen:
            continue

        # 카드 컨테이너 찾기 (a 태그 부모 탐색)
        card = a.find_parent(["article", "div", "li", "section"])

        # 제목
        title = ""
        if card:
            h_tag = card.find(["h2", "h3", "h4"])
            if h_tag:
                title = h_tag.get_text(strip=True)
        if not title:
            title = a.get_text(strip=True)
        if not title or len(title) < 5:
            continue

        # 설명문
        description = ""
        if card:
            p_tag = card.find("p")
            if p_tag:
                description = p_tag.get_text(strip=True)

        # 카테고리 (예: CONSUMER SPEND, EXPERT INSIGHTS)
        category = ""
        if card:
            for span in card.find_all(["span", "div", "p"]):
                txt = span.get_text(strip=True)
                if txt.isupper() and 3 < len(txt) < 40:
                    category = txt
                    break

        items.append({
            "title":       title,
            "url":         url,
            "description": description,
            "category":    category,
        })
        seen.add(url)

        if len(items) >= 6:
            break

    logging.info(f"[Barclays] 목록 감지: {len(items)}개")
    for i, it in enumerate(items, 1):
        logging.info(f"[Barclays] #{i} {it['title']} | {it['url']}")

    return items


# ─────────────────────────────────────────────
# 2) 상세 페이지 파싱
# ─────────────────────────────────────────────
def extract_article(url: str) -> tuple[str, str, str, str]:
    """
    반환: (title, date, author, body_text)
    """
    soup = get_soup(url, wait_selector="h1")

    # 제목
    h1    = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # 날짜 (예: "12 June 2026" 또는 "June 12, 2026")
    date = ""
    date_pattern = re.compile(
        r"\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}"
    )
    for tag in soup.find_all(["p", "span", "div", "time"]):
        txt = tag.get_text(strip=True)
        if date_pattern.match(txt) and len(txt) < 30:
            date = txt
            break

    # 저자
    author = ""
    for tag in soup.find_all(["p", "span", "div"]):
        txt = tag.get_text(strip=True)
        # "Name, Title at Barclays" 패턴
        if "barclays" in txt.lower() and "," in txt and len(txt) < 120:
            author = txt
            break

    # 본문
    body_parts: list[str] = []
    start = h1 or soup.find("body")

    if start:
        for node in start.find_all_next():
            if not isinstance(node, Tag):
                continue

            txt = node.get_text(" ", strip=True)
            low = txt.lower()

            # 푸터/관련기사 영역에서 중단
            if any(stop in low for stop in [
                "©barclays",
                "© barclays",
                "related insights",
                "read more articles",
                "you might also like",
                "sign up",
                "subscribe",
            ]):
                break

            if node.name == "p" and len(txt) > 40:
                body_parts.append(txt)
            elif node.name in {"h2", "h3", "h4"} and txt:
                body_parts.append(f"\n[{txt}]")

    body_text = "\n".join(body_parts)
    return title, date, author, body_text


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

def summarize_ko(text: str, max_chars: int = 800) -> str:
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
def build_message(item: dict) -> str:
    url = item["url"]

    title_en, date, author, body_en = extract_article(url)

    # 파싱 실패 시 목록에서 가져온 정보로 보완
    if not title_en:
        title_en = item.get("title", "")

    title_ko = translate_title(title_en)

    header = "🏦 Barclays UK Unlocked\n\n"
    if item.get("category"):
        header += f"📂 {item['category']}\n"
    header += f"{title_ko}\n{title_en}\n"
    if date:
        header += f"📅 {date}\n"
    if author:
        header += f"👤 {author}\n"
    header += "\n"

    tail = f"\n\n🔗 {url}\n"

    remain = MAX_TOTAL - len(header) - len(tail) - 20
    if remain < 400:
        remain = 400

    # 본문 파싱 실패 시 목록의 description으로 보완
    source_text = body_en if body_en else item.get("description", "")
    summary_ko  = summarize_ko(source_text, max_chars=min(remain, 800))

    msg = header + "📝 요약\n" + summary_ko + tail
    if len(msg) > MAX_TOTAL:
        msg = msg[:MAX_TOTAL - 3] + "..."
    return msg


# ─────────────────────────────────────────────
# 한 번 실행 (새 기사만 전송)
# ─────────────────────────────────────────────
def run_once():
    items = fetch_listing()
    if not items:
        logging.info("[Barclays] 목록 비어 있음")
        return

    first_run = not os.path.exists(STATE_FILE)
    if first_run:
        items = items[:3]
        logging.info(f"[Barclays] 첫 실행: 최신 {len(items)}개만 전송")

    for item in reversed(items):
        url = item["url"]

        if (not first_run) and is_seen(url):
            continue

        logging.info(f"[Barclays] 새 항목: {url}")

        try:
            msg = build_message(item)
            logging.info(f"[Barclays] 메시지 길이: {len(msg)}자")
            send_telegram(msg)
            add_seen([url])
            time.sleep(3)
        except Exception as e:
            logging.error(f"[Barclays] 기사 처리 오류: {e}")


# ─────────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logging.info("Barclays UK Unlocked 크롤러 시작")

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