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
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# OpenAI
from openai import OpenAI


# ─────────────────────────────────────────────
# 환경 변수
# ─────────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_TEST_CHANNEL_ID")
client = OpenAI(api_key=OPENAI_API_KEY)

BASE_URL = "https://www.ctee.com.tw"
CTEE_TECH_URL = "https://www.ctee.com.tw/industry/tech"
STATE_FILE = "ctee_tech_state.json"

MAX_TG_CHARS = 3800
MAX_TOTAL = 3800

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s"
)


# ─────────────────────────────────────────────
# Selenium 드라이버 초기화 (undetected_chromedriver)
# ─────────────────────────────────────────────
def init_driver():
    # uc에서 쓰는 옵션 객체
    options = uc.ChromeOptions()

    # 디버깅할 땐 False로 두고 실제 브라우저 띄워서 확인
    # options.headless = False
    # 잘 동작하는 것 확인되면 True로 바꿔서 완전 headless로 돌려도 됨
    # options.headless = True

    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # ★ 여기에서 현재 크롬 메이저 버전(142)을 지정해 준다
    driver = uc.Chrome(
        version_main=142,   # ← 현재 크롬 메이저 버전
        options=options,
    )
    driver.set_page_load_timeout(30)
    return driver



driver = init_driver()
wait = WebDriverWait(driver, 20)


# ─────────────────────────────────────────────
# 상태 파일 로드 및 저장
# ─────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"recent_urls": []}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_seen(url: str) -> bool:
    st = load_state()
    return url in st.get("recent_urls", [])


def add_seen(urls: List[str]):
    st = load_state()
    recent = st.get("recent_urls", [])
    for u in urls:
        if u not in recent:
            recent.append(u)
    st["recent_urls"] = recent
    save_state(st)


# ─────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────
def normalize_ctee_url(url: str) -> str:
    """?utm= 같은 쿼리 파라미터 제거해서 동일 기사 URL 통일"""
    return (url or "").split("?")[0].strip()


# ─────────────────────────────────────────────
# Selenium으로 HTML 로딩 → BeautifulSoup 변환
# ─────────────────────────────────────────────
def get_soup(url: str, wait_selector: str = None) -> BeautifulSoup:
    # 상세 페이지에서 브라우저가 차단 페이지로 튕기는지 확인할 때도 이 함수 한 군데만 보면 됨
    driver.get(url)

    # lazy load 대비 스크롤
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1.2)

    if wait_selector:
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector)))
        except TimeoutException:
            logging.warning(f"[CTEE] 로딩 대기 실패: {wait_selector}")

    html = driver.page_source
    return BeautifulSoup(html, "html.parser")


def extract_text(el: Tag | None) -> str:
    if not el:
        return ""
    txt = el.get_text("\n", strip=True)
    txt = "\n".join(line.strip() for line in txt.splitlines() if line.strip())
    return txt


# ─────────────────────────────────────────────
# 목록 파싱 (/industry/tech에서 /news/ 링크 긁기)
# ─────────────────────────────────────────────
def fetch_listing():
    try:
        soup = get_soup(CTEE_TECH_URL)
    except Exception as e:
        logging.error(f"[CTEE] 목록 페이지 로드 실패: {e}")
        return []

    items = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href:
            continue

        if href.startswith("http"):
            url = href
        else:
            if not href.startswith("/"):
                continue
            url = BASE_URL + href

        # 뉴스 기사만
        if BASE_URL not in url:
            continue
        if "/news/" not in url:
            continue

        clean_url = normalize_ctee_url(url)
        if clean_url in seen:
            continue

        title = a.get_text(strip=True)
        if not title or len(title) < 6:
            continue

        items.append({"title": title, "url": clean_url})
        seen.add(clean_url)

        if len(items) >= 30:
            break

    logging.info(f"[CTEE] 목록 기사 감지: {len(items)}개")
    for i, it in enumerate(items[:5], start=1):
        logging.info(f"[CTEE] #{i} {it['title']} | {it['url']}")

    return items


# ─────────────────────────────────────────────
# 상세 페이지 파싱
# ─────────────────────────────────────────────
def extract_article(url: str):
    soup = get_soup(url, wait_selector="h1.main-title")

    title_el = soup.find("h1", class_="main-title")
    title_zh = title_el.get_text(strip=True) if title_el else ""

    article_el = soup.select_one("main#main div.content_body div.article-wrap article")
    if not article_el:
        article_el = soup.find("article") or soup.find("div", class_="content_body")

    body_zh = extract_text(article_el)
    return title_zh, body_zh


# ─────────────────────────────────────────────
# GPT 번역
# ─────────────────────────────────────────────
def translate_title_ko(title_zh: str) -> str:
    if not title_zh:
        return ""

    prompt = f"""
다음 번체 중국어 기사 제목을 한국어로 번역해줘.
- 한 줄 제목 유지
- 과한 의역 금지
- 자연스러운 뉴스 제목 스타일

제목:
{title_zh}
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.1,
        messages=[
            {"role": "system", "content": "너는 경제·기술 기사 제목 번역 전문가야."},
            {"role": "user", "content": prompt},
        ]
    )
    return resp.choices[0].message.content.strip()


def summarize_ko(body_zh: str, max_chars=3000) -> str:
    if not body_zh:
        return "[본문 없음]"

    prompt = f"""
        다음은 대만 경제지(工商時報) 기사 전문이야.

        요구:
        - 한국어로 자연스럽게 요약 번역
        - 핵심 정보만 남기기
        - 회사명, 숫자, 일정은 반드시 유지
        - 3~6문단 정도
        - {max_chars}자 이내

        원문:
        {body_zh}
    """

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[
            {"role": "system", "content": "너는 대만 경제 기사 한국어 요약 전문가야."},
            {"role": "user", "content": prompt},
        ]
    )
    out = resp.choices[0].message.content.strip()
    if len(out) > max_chars:
        out = out[:max_chars]
    return out


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
            data={"chat_id": TELEGRAM_CHANNEL_ID, "text": msg}
        )
        resp.raise_for_status()
        logging.info("텔레그램 전송 완료")
    except Exception as e:
        logging.error(f"텔레그램 전송 실패: {e}")
        logging.error("실패 메시지 일부: %s", msg[:200])


# ─────────────────────────────────────────────
# 메시지 생성
# ─────────────────────────────────────────────
def build_message(url: str):
    title_zh, body_zh = extract_article(url)
    title_ko = translate_title_ko(title_zh)

    header = (
        "📰 工商時報(CTEE) 기술·산업 뉴스\n\n"
        f"{title_ko}\n"
        f"{title_zh}\n\n"
    )
    tail = f"\n\n🔗 원문: {url}"

    remain = MAX_TOTAL - len(header) - len(tail) - 50
    if remain < 600:
        remain = 600

    summary_ko = summarize_ko(body_zh, max_chars=remain)

    msg = header + summary_ko + tail
    if len(msg) > MAX_TOTAL:
        msg = msg[:MAX_TOTAL - 3] + "..."

    return msg


# ─────────────────────────────────────────────
# 한 번 실행 (새 기사만 전송)
# ─────────────────────────────────────────────
def run_once():
    items = fetch_listing()
    if not items:
        logging.info("목록 비어 있음")
        return

    first_run = not os.path.exists(STATE_FILE)

    if first_run:
        items = items[:5]
        logging.info(f"첫 실행: 최신 {len(items)}개 기사만 전송")

    for item in reversed(items):
        raw_url = item["url"]
        url = normalize_ctee_url(raw_url)

        if (not first_run) and is_seen(url):
            continue

        logging.info(f"새 기사 발견: {url}")

        try:
            msg = build_message(url)
            # print(msg)
            send_telegram(msg)
            add_seen([url])
            time.sleep(3)
        except Exception as e:
            logging.error(f"기사 처리 중 오류: {e}")


# ─────────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logging.info("CTEE Tech 크롤러 시작")

    try:
        while True:
            try:
                run_once()
            except Exception as e:
                logging.error(f"주기 실행 오류: {e}")

            logging.info("다음 실행까지 30분 대기…")
            time.sleep(1800)
    finally:
        # 종료 시 드라이버 정리
        try:
            driver.quit()
        except Exception:
            pass
