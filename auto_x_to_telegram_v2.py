import os
from dotenv import load_dotenv
import time
import html
import tweepy
from tweepy.errors import TweepyException, TooManyRequests, HTTPException as TweepyHTTPException
from typing import Optional, List
import requests
from requests.exceptions import RequestException, Timeout
import re
# from googletrans import Translator
from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException
import random
import psutil
import gc
import feedparser
from email.utils import parsedate_to_datetime
from datetime import timezone, datetime
import json

load_dotenv()
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
TWITTER_USERNAMES = os.getenv("TWITTER_USERNAMES", "").split(",")
TWITTER_USER_IDS = os.getenv("TWITTER_USER_IDS", "").split(",")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
NAVER_INVOKE_URL = os.getenv("NAVER_INVOKE_URL")
MS_TRANSLATOR_KEY = os.getenv("MS_TRANSLATOR_KEY")
MS_TRANSLATOR_REGION = os.getenv("MS_TRANSLATOR_REGION")
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMOJI_PATTERN = re.compile("[\U0001F300-\U0001FAFF\U00002700-\U000027BF]+", flags=re.UNICODE)
URL_PATTERN = re.compile(r"https?://[^\s\)\]\}]+", re.IGNORECASE)
NL_TOKEN = "[[NL]]"

# 초기 설정
client = tweepy.Client(
    bearer_token=TWITTER_BEARER_TOKEN,
    wait_on_rate_limit=True  # 429일 때 자동 대기
)
_gpt_client = OpenAI(api_key=OPENAI_API_KEY)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHECK_INTERVAL_SECONDS = 1000
MAX_CAPTION_LENGTH = 1000  # 텔레그램 안전 범위
TEXT_LENGTH_THRESHOLD = 250  # 크롤링을 시작할 텍스트 길이 임계값
LAST_ID_JSON_PATH = os.path.join(BASE_DIR, "x_last_ids.json")
HTTP_TIMEOUT = 10          # MyMemory, MS, DeepL, 텔레그램 등에 쓸 기본 HTTP 타임아웃
TELEGRAM_TIMEOUT = 10      # 텔레그램 전송용
OPENAI_TIMEOUT = 20        # GPT 번역용

# 특정 유저의 quoted 트윗은 제외할 때 쓰는 리스트
EXCLUDE_QUOTE_USERS = [
    "105353526",            # markminervini
    "25073877",             # realDonaldTrump
    "1406461126917849096",  # mmsbml
]

# TruthSocialTrump 추가
RSS_URL = "https://trumpstruth.org/feed"
TRUMP_STATE_FILE = "trump_truth_last_ts.txt"
TRUMP_USERNAME = "TruthSocial_Trump"

def _load_last_ids() -> dict:
    """x_last_ids.json에서 전체 매핑 불러오기"""
    try:
        with open(LAST_ID_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"⚠️ x_last_ids.json 로드 오류: {e}")
    return {}

def _save_last_ids(data: dict):
    """전체 매핑을 x_last_ids.json에 저장"""
    try:
        with open(LAST_ID_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"⚠️ last_ids.json 저장 오류: {e}")

def get_last_id(user_id: str) -> Optional[int]:
    """
    x_last_ids.json에서 해당 user_id의 last_id를 가져온다.
    없으면 None.
    """
    data = _load_last_ids()
    value = data.get(user_id)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def save_last_id(user_id: str, tweet_id: int):
    """
    x_last_ids.json에 user_id -> tweet_id 매핑을 저장.
    기존 값은 덮어씀.
    """
    data = _load_last_ids()
    data[user_id] = int(tweet_id)
    _save_last_ids(data)

def mask_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", NL_TOKEN)

def restore_newlines(text: str) -> str:
    import re
    return re.sub(r"\[\[nl\]\]", "\n", text, flags=re.IGNORECASE)

class TwitterCrawler:
    def __init__(self):
        self.setup_driver()
        
    def setup_driver(self):
        chrome_options = Options()
        
        # 기본 설정
        chrome_options.add_argument("--headless=new")           # 최신 헤드리스
        chrome_options.add_argument("--disable-gpu")            # GPU 비활성 (윈도우/가상환경 필수)
        chrome_options.add_argument("--use-gl=swiftshader")     # 소프트웨어 렌더러
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1920,1080")
        
        # 봇 감지 방지 설정
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        
        # 다양한 User-Agent 랜덤 선택
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
        ]
        selected_ua = random.choice(user_agents)
        chrome_options.add_argument(f"--user-agent={selected_ua}")
        
        # 추가 봇 감지 방지
        chrome_options.add_argument("--disable-web-security")
        chrome_options.add_argument("--allow-running-insecure-content")
        chrome_options.add_argument("--disable-features=VizDisplayCompositor")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-plugins")
        
        self.driver = webdriver.Chrome(options=chrome_options)
        
        # JavaScript로 봇 감지 방지
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.driver.execute_script("Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]})")
        self.driver.execute_script("Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']})")
        
        self.wait = WebDriverWait(self.driver, 15)  # 대기 시간 증가
    

    def crawl_full_tweet_text(self, tweet_id, username):
        try:
            url = f"https://x.com/{username}/status/{tweet_id}"
            print(f"🔍 크롤링 시작: {url}")
            time.sleep(random.uniform(1.5, 3.5))
            self.driver.get(url)

            # 본문이 보일 때까지 대기
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="tweetText"]')))

            # 사람처럼 스크롤
            self.simulate_human_behavior()

            full_text = self.extract_tweet_text()
            return full_text
        except Exception as e:
            print(f"❌ 크롤링 실패: {e}")
            return None

    
    def simulate_human_behavior(self):
        """인간과 유사한 행동 시뮬레이션"""
        try:
            # 랜덤 스크롤
            scroll_amount = random.randint(100, 300)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
            time.sleep(random.uniform(0.5, 1.5))
            
            # 다시 위로 스크롤
            self.driver.execute_script(f"window.scrollBy(0, -{scroll_amount//2});")
            time.sleep(random.uniform(0.3, 0.8))
            
            # 마우스 움직임 시뮬레이션 (헤드리스에서는 효과 없지만 패턴 감지 방지)
            self.driver.execute_script("""
                var event = new MouseEvent('mousemove', {
                    'view': window,
                    'bubbles': true,
                    'cancelable': true,
                    'clientX': arguments[0],
                    'clientY': arguments[1]
                });
                document.dispatchEvent(event);
            """, random.randint(100, 800), random.randint(100, 600))
            
        except Exception as e:
            print(f"⚠️ 행동 시뮬레이션 실패: {e}")
            pass

    def _html_to_text_with_emojis(self, html_str: str) -> str:
        # 1) <img ... alt="🙂" ...> → 🙂 로 치환 (Twemoji)
        html_str = re.sub(
            r'<img[^>]*\salt="([^"]+)"[^>]*>',
            lambda m: html.unescape(m.group(1)),
            html_str,
            flags=re.IGNORECASE
        )
        # 2) <svg ... aria-label="🙂" ...>...</svg> → 🙂
        html_str = re.sub(
            r'<svg[^>]*\saria-label="([^"]+)"[^>]*>.*?</svg>',
            lambda m: html.unescape(m.group(1)),
            html_str,
            flags=re.IGNORECASE | re.DOTALL
        )
        # 3) 링크 텍스트 등 span 정리: 태그 제거 전, 줄바꿈은 보존
        # <br> → 줄바꿈
        html_str = re.sub(r'<br\s*/?>', '\n', html_str, flags=re.IGNORECASE)

        # 4) 남은 태그 제거
        text = re.sub(r'<[^>]+>', '', html_str)

        # 5) HTML 엔티티 디코드 (&amp; 등)
        text = html.unescape(text)

        # 6) 공백 정리
        return re.sub(r'[ \t\f\v]+', ' ', text).strip()
        
    def extract_tweet_text(self):
        try:
            tweet_selectors = [
                '[data-testid="tweetText"]',
                'article[data-testid="tweet"] span[dir="auto"]',
                'div[data-testid="tweetText"]',
                'span[data-testid="tweetText"]'
            ]
            for selector in tweet_selectors:
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, selector)
                    # 핵심: innerHTML로 가져와서 이모지 복원
                    inner_html = el.get_attribute("innerHTML")
                    if inner_html:
                        text = self._html_to_text_with_emojis(inner_html)
                        if text:
                            print(f"✅ 트윗 텍스트(이모지 포함) 추출 성공: {len(text)}자")
                            return text
                except NoSuchElementException:
                    continue
            print("❌ 트윗 텍스트를 찾을 수 없습니다.")
            return None
        except Exception as e:
            print(f"❌ 텍스트 추출 실패: {e}")
            return None
    
    def close(self):
        if self.driver:
            self.driver.quit()

# 크롤러 인스턴스 생성 (지연 초기화)
crawler = None
crawler_created_time = None
CRAWLER_RESTART_INTERVAL = 3600  # 1시간마다 크롤러 재시작

def get_crawler():
    """크롤러 인스턴스를 필요할 때만 생성하고 정기적으로 재시작"""
    global crawler, crawler_created_time
    
    current_time = time.time()
    
    # 크롤러가 없거나 오래된 경우 재생성
    if (crawler is None or 
        crawler_created_time is None or 
        current_time - crawler_created_time > CRAWLER_RESTART_INTERVAL):
        
        # 기존 크롤러 정리
        if crawler:
            try:
                print("🔄 크롤러 재시작 중...")
                crawler.close()
                print("✅ 기존 크롤러 정리 완료")
            except Exception as e:
                print(f"⚠️ 크롤러 정리 중 오류: {e}")
        
        # 새 크롤러 생성
        print("🔧 새 크롤러 초기화 중...")
        crawler = TwitterCrawler()
        crawler_created_time = current_time
        print("✅ 새 크롤러 초기화 완료")
    
    return crawler

def replace_emojis_with_tags(text):
    emojis = []
    def replacer(match):
        emojis.append(match.group(0))
        return f"[EMOJI_{len(emojis) - 1}]"
    new_text = EMOJI_PATTERN.sub(replacer, text)
    return new_text, emojis

def restore_emojis(translated_text, emojis):
    for idx, emoji in enumerate(emojis):
        pattern = re.compile(rf"\[emoji_{idx}\]", re.IGNORECASE)
        translated_text = pattern.sub(emoji, translated_text)
    return translated_text

def mask_urls(text):
    urls = []
    def replacer(match):
        urls.append(match.group(0))
        return f"[URL_{len(urls) - 1}]"
    new_text = URL_PATTERN.sub(replacer, text)
    return new_text, urls

def restore_urls(text, urls):
    for idx, url in enumerate(urls):
        pattern = re.compile(rf"\[url_{idx}\]", re.IGNORECASE)
        # 앞뒤 공백이 없으면 띄어쓰기 강제 삽입
        text = pattern.sub(f" {url} ", text)
    return text

def translate(text):
    for engine in [translate_with_gpt4omini, translate_with_mymemory, translate_with_microsoft, translate_with_deepl]:
        try:
            return engine(text)
        except Exception as e:
            print(f"⚠️ {engine.__name__} 실패: {e}")
    # 모든 번역 실패 시 None 반환
    return None

def translate_preserving_emojis_and_urls(original_text):
    # 1. 이모지 마스킹
    emoji_tagged_text, emojis = replace_emojis_with_tags(original_text)
    # 2. URL 마스킹
    url_tagged_text, urls = mask_urls(emoji_tagged_text)
    # 3. 번역
    translated = translate(url_tagged_text)  # 순차적 번역기 호출
    # 4. 이모지 복원
    text_with_emoji = restore_emojis(translated, emojis)
    # 5. URL 복원
    fully_restored = restore_urls(text_with_emoji, urls)
    return fully_restored

def translate_with_gpt4omini(text: str, target_lang: str = "ko", source_lang: str | None = None) -> str:
    """
    GPT‑4o mini로 번역.
    - 이모지, URL, @멘션, #해시태그, $티커, [EMOJI_1] 같은 플레이스홀더, 줄바꿈/공백은 원형 보존.
    - translate_preserving_emojis_and_urls()가 이미 마스킹/복원을 하므로 여기선 안전하게 번역만.
    """
    if not text or text.strip() == "":
        return text

    system_msg = (
        "You are a precise translator. Translate ONLY natural language segments into the target language. "
        "STRICTLY preserve as-is: emojis, URLs (https://...), emails, @mentions, #hashtags, $tickers, "
        "any placeholders like [EMOJI_0], {EMOJI_1}, [[EMOJI_2]] with exact casing and brackets, "
        "code blocks/inline code, and original line breaks/spaces. "
        "Do not add extra text. Output only the translation."
    )

    # source_lang은 선택 사항 (지정하지 않아도 됨)
    if source_lang:
        user_msg = f"Source language: {source_lang}\nTarget language: {target_lang}\n\nText:\n{text}"
    else:
        user_msg = f"Target language: {target_lang}\n\nText:\n{text}"

    try:
        resp = _gpt_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            timeout=OPENAI_TIMEOUT,
        )
        out = resp.choices[0].message.content or ""
        return out.strip()
    except Exception as e:
        print(f"⚠️ GPT 번역 실패: {e}")
        # translate() 쪽에서 다음 엔진으로 넘어가게 하기 위해 예외 유지
        raise

def translate_with_mymemory(text, source="en", target="ko"):
    # 480자 이상이면 문장 단위로 분할하여 번역
    if len(text) > 430:
        print(f"📏 긴 텍스트 감지 ({len(text)}자), MyMemory 분할 번역 시작")
        sentences = split_text_into_sentences(text)
        translated_parts = []
        
        current_part = ""
        
        for sentence in sentences:
            # 현재 부분에 문장을 추가했을 때 길이 확인
            if len(current_part + sentence) <= 430:
                current_part += sentence
            else:
                # 현재 부분 번역
                if current_part:
                    translated_part = translate_mymemory_part(current_part, source, target)
                    if translated_part:
                        translated_parts.append(translated_part)
                
                # 새 부분 시작
                current_part = sentence
        
        # 마지막 부분 번역
        if current_part:
            translated_part = translate_mymemory_part(current_part, source, target)
            if translated_part:
                translated_parts.append(translated_part)
        
        # 번역된 부분들을 합치기
        if translated_parts:
            return "".join(translated_parts)
        else:
            raise Exception("MyMemory 분할 번역 실패")
    
    # 480자 이하면 기존 방식으로 번역
    return translate_mymemory_part(text, source, target)

def translate_mymemory_part(text, source="en", target="ko"):
    """MyMemory API로 텍스트 번역 (단일 부분)"""
    url = "https://api.mymemory.translated.net/get"
    params = {
        "q": text,
        "langpair": f"{source}|{target}"
    }

    try:
        response = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        result = data["responseData"]["translatedText"]

        if "MYMEMORY WARNING" in result.upper():
            raise Exception("MyMemory usage limit reached")

        return result
    except (RequestException, Timeout) as e:
        print(f"⚠️ MyMemory 요청 실패: {e}")
        raise

def split_text_into_sentences(text):
    """텍스트를 문장 단위로 분할"""
    # 문장 구분자들
    sentence_endings = ['.', '!', '?']
    
    sentences = []
    current_sentence = ""
    
    for char in text:
        current_sentence += char
        
        if char in sentence_endings:
            if current_sentence.strip():
                sentences.append(current_sentence.strip())
            current_sentence = ""
    
    # 마지막 문장 처리
    if current_sentence.strip():
        sentences.append(current_sentence.strip())
    
    return sentences

# def translate_with_googletrans(text, dest='ko'):
#     translator = Translator()
#     try:
#         result = translator.translate(text, dest=dest)
#         return result.text
#     except Exception as e:
#         print("❌ googletrans 오류:", e)
#         return "[Google Translate 실패]"

def translate_with_microsoft(text):
    url = "https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&from=en&to=ko"
    headers = {
        "Ocp-Apim-Subscription-Key": os.getenv("MS_TRANSLATOR_KEY"),
        "Ocp-Apim-Subscription-Region": os.getenv("MS_TRANSLATOR_REGION"),
        "Content-type": "application/json"
    }
    body = [{"text": text}]
    try:
        response = requests.post(url, headers=headers, json=body, timeout=HTTP_TIMEOUT)
        if response.status_code == 429:
            raise Exception("Microsoft usage limit exceeded")
        response.raise_for_status()
        return response.json()[0]["translations"][0]["text"]
    except (RequestException, Timeout) as e:
        print(f"⚠️ Microsoft Translator 요청 실패: {e}")
        raise

def translate_with_deepl(text):
    url = "https://api-free.deepl.com/v2/translate"
    headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
    data = {
        "text": text,
        "target_lang": "KO",  # 한국어
    }
    try:
        response = requests.post(url, headers=headers, data=data, timeout=HTTP_TIMEOUT)
        if response.status_code == 456:
            raise Exception("DeepL usage limit exceeded")
        response.raise_for_status()
        return response.json()["translations"][0]["text"]
    except (RequestException, Timeout) as e:
        print(f"⚠️ DeepL 요청 실패: {e}")
        raise

def extract_emojis(text):
    emojis = EMOJI_PATTERN.findall(text)
    cleaned = EMOJI_PATTERN.sub("", text)
    return emojis, cleaned.strip()

def merge_emojis_back(emojis, translated_text):
    return f"{''.join(emojis)} {translated_text}"

def get_latest_tweet(user_id, last_id=None, max_results=10):
    return call_with_retry(
        client.get_users_tweets,
        id=user_id,
        since_id=last_id,
        max_results=max_results,
        # ✅ 답글/리트윗 제외 → Posts 탭과 일치
        exclude=["replies", "retweets"],
        tweet_fields=["created_at", "id", "text", "attachments", "referenced_tweets"],
        expansions=["attachments.media_keys", "referenced_tweets.id"],
        media_fields=["url", "type"]
    )

def iterate_user_tweets(user_id: str, since_id: Optional[int], page_size: int = 10):
    """
    since_id 이후의 모든 트윗을 '오래된 것부터' yield.
    각 yield는 (tweet, includes) 튜플.
    - page_size: 10~100 (트위터 제한). 100 권장.
    """
    next_token = None
    pages = []

    while True:
        resp = call_with_retry(
            client.get_users_tweets,
            id=user_id,
            since_id=since_id,
            max_results=page_size,
            exclude=["replies", "retweets"],  # Posts 탭과 일치
            tweet_fields=["created_at", "id", "text", "attachments", "referenced_tweets"],
            expansions=["attachments.media_keys", "referenced_tweets.id"],
            media_fields=["url", "type"],
            pagination_token=next_token
        )

        # 응답에 데이터가 없으면 종료
        if not resp.data or len(resp.data) == 0:
            break

        # 트위터는 보통 최신→과거 순으로 전달하므로, 페이지 내에서 뒤집어 '과거→최신' 순서로 정렬
        page_tweets = list(reversed(resp.data))
        pages.append((page_tweets, resp.includes))

        # 다음 페이지가 있으면 이어서, 없으면 종료
        next_token = getattr(resp.meta, "next_token", None)
        if not next_token:
            break

    # 가장 오래된 페이지부터, 페이지 내부도 오래된 트윗부터 순차 처리
    for page_tweets, includes in pages:
        for t in page_tweets:
            yield t, includes

def call_with_retry(func, *args, retries=5, base=1.8, **kwargs):
    """
    - 5xx(503 등), 네트워크 일시 오류 → 지수 백오프 재시도
    - 429(TooManyRequests) → API가 제공하는 재시도 시간 또는 백오프
    """
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except TooManyRequests as e:
            wait = getattr(e, "retry_after", None)
            if not wait:
                wait = base ** attempt
            print(f"⏳ 429 대기 {wait:.1f}s")
            time.sleep(wait)
            attempt += 1
        except TweepyHTTPException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code and 500 <= code < 600 and attempt < retries:
                wait = base ** attempt
                print(f"⏳ {code} 재시도 {attempt+1}/{retries} (대기 {wait:.1f}s)")
                time.sleep(wait)
                attempt += 1
                continue
            raise
        except TweepyException as e:
            if attempt < retries:
                wait = base ** attempt
                print(f"⏳ 일시 오류 재시도 {attempt+1}/{retries} (대기 {wait:.1f}s): {e}")
                time.sleep(wait)
                attempt += 1
                continue
            raise

def fetch_original_retweet(tweet, client, username):
    if tweet.referenced_tweets:
        for ref in tweet.referenced_tweets:
            if ref.type == "retweeted":
                response = client.get_tweet(
                    id=ref.id,
                    tweet_fields=["created_at", "text", "attachments"],
                    expansions=["attachments.media_keys"],
                    media_fields=["url", "type"]
                )

                print(response.data)
                print(response.includes)
                
                # 원본 트윗의 전체 텍스트 가져오기 (크롤링 포함)
                original_tweet = response.data
                full_text = get_full_tweet_text(original_tweet, username)
                
                media_urls = extract_image_urls(original_tweet, response.includes)
                
                return full_text, media_urls

    # 리트윗이 아니면
    return tweet.text, []

def get_full_tweet_text(tweet, username):
    """트윗의 전체 텍스트를 가져오는 함수"""
    api_text = tweet.text
    
    # 텍스트 길이가 임계값을 넘으면 크롤링 시도
    if len(api_text) >= TEXT_LENGTH_THRESHOLD:
        print(f"📏 텍스트 길이({len(api_text)}자)가 임계값({TEXT_LENGTH_THRESHOLD}자)을 초과하여 크롤링을 시도합니다.")
        
        # 크롤링 빈도 제한 (너무 자주 크롤링하지 않도록)
        if hasattr(get_full_tweet_text, 'last_crawl_time'):
            time_since_last = time.time() - get_full_tweet_text.last_crawl_time
            if time_since_last < 30:  # 30초 내에 다시 크롤링하지 않음
                wait_time = 30 - time_since_last + 2
                print(f"⏰ 크롤링 빈도 제한: {wait_time:.1f}초 대기 후 크롤링 진행")
                time.sleep(wait_time)
                print("✅ 대기 완료, 크롤링 시작")
        
        try:
            # 크롤링 전 랜덤 대기
            pre_crawl_delay = random.uniform(1, 3)
            print(f"🔄 크롤링 전 대기: {pre_crawl_delay:.1f}초")
            time.sleep(pre_crawl_delay)
            
            crawled_text = get_crawler().crawl_full_tweet_text(tweet.id, username)
            
            # 크롤링 시간 기록
            get_full_tweet_text.last_crawl_time = time.time()
            
            if crawled_text and len(crawled_text) > len(api_text):
                print(f"✅ 크롤링으로 더 긴 텍스트를 가져왔습니다! ({len(crawled_text)}자)")
                return crawled_text
            else:
                print(f"ℹ️ 크롤링 결과가 API 텍스트와 동일하거나 짧습니다. API 텍스트를 사용합니다.")
                return api_text
        except Exception as e:
            print(f"❌ 크롤링 중 오류 발생: {e}")
            return api_text
    else:
        print(f"📏 텍스트 길이({len(api_text)}자)가 임계값({TEXT_LENGTH_THRESHOLD}자) 미만입니다. API 텍스트를 사용합니다.")
        return api_text

def extract_image_urls(tweet, includes):
    media_urls = []
    # ✅ attachments가 None인지 먼저 확인
    if hasattr(tweet, "attachments") and tweet.attachments and "media_keys" in tweet.attachments and includes:
        media_items = includes.get("media", [])
        media_key_set = set(tweet.attachments["media_keys"])
        for media in media_items:
            if media["media_key"] in media_key_set and media["type"] == "photo":
                media_urls.append(media["url"])
    return media_urls

def send_to_telegram_with_optional_image(message: str, image_urls: List[str]):
    try:
        send_text_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

        if image_urls:
            if len(image_urls) == 1:
                # 이미지가 1장일 때는 sendPhoto
                send_photo_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
                if len(message) <= MAX_CAPTION_LENGTH:
                    photo_payload = {
                        "chat_id": TELEGRAM_CHANNEL_ID,
                        "photo": image_urls[0],
                        "caption": message
                    }
                    response = requests.post(send_photo_url, data=photo_payload, timeout=TELEGRAM_TIMEOUT)
                    response.raise_for_status()
                    print("✅ 사진+메시지 전송 완료")
                else:
                    # 메시지가 길면 사진만 보내고 텍스트 따로
                    response = requests.post(
                        send_photo_url,
                        data={"chat_id": TELEGRAM_CHANNEL_ID, "photo": image_urls[0]},
                        timeout=TELEGRAM_TIMEOUT,
                    )
                    response.raise_for_status()
                    print("✅ 사진 전송 완료 (텍스트는 별도 전송)")
                    response = requests.post(
                        send_text_url,
                        data={"chat_id": TELEGRAM_CHANNEL_ID,"text": message},
                        timeout=TELEGRAM_TIMEOUT,
                    )
                    response.raise_for_status()
                    print("✅ 텍스트 전송 완료")
            else:
                # 이미지가 여러 장일 때는 sendMediaGroup
                send_group_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
                media = []
                for i, u in enumerate(image_urls[:10]):  # 최대 10장
                    item = {"type": "photo", "media": u}
                    if i == 0 and len(message) <= MAX_CAPTION_LENGTH:
                        item["caption"] = message
                    media.append(item)
                response = requests.post(
                    send_group_url,
                    json={"chat_id": TELEGRAM_CHANNEL_ID, "media": media},
                    timeout=TELEGRAM_TIMEOUT,
                )
                response.raise_for_status()
                print(f"✅ 사진 {len(media)}장 전송 완료 (첫 장에만 캡션)")
                if len(message) > MAX_CAPTION_LENGTH:
                    # 캡션 길이 초과분은 별도 메시지 전송
                    response = requests.post(
                        send_text_url,
                        data={"chat_id": TELEGRAM_CHANNEL_ID,"text": message},
                        timeout=TELEGRAM_TIMEOUT,
                    )
                    response.raise_for_status()
                    print("✅ 추가 텍스트 전송 완료")
        else:
            # 이미지가 없을 경우
            response = requests.post(
                send_text_url,
                data={"chat_id": TELEGRAM_CHANNEL_ID, "text": message},
                timeout=TELEGRAM_TIMEOUT,
            )
            response.raise_for_status()
            print("✅ 텍스트 전송 완료 (이미지 없음)")
    except (RequestException, Timeout) as e:
        print("❌ 전송 실패(네트워크/타임아웃):", e)
        print("📦 실패한 메시지:", message)
    except Exception as e:
        print("❌ 전송 실패:", e)
        print("📦 실패한 메시지:", message)

# def send_to_telegram_with_optional_image(message: str, image_urls: List[str]):
#     try:
#         send_text_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
#         if image_urls:
#             send_photo_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

#             if len(message) <= MAX_CAPTION_LENGTH:
#                 # ✅ 사진 + 메시지 함께 전송 (caption 사용)
#                 photo_payload = {
#                     "chat_id": TELEGRAM_CHANNEL_ID,
#                     "photo": image_urls[0],
#                     "caption": message
#                 }
#                 response = requests.post(send_photo_url, data=photo_payload)
#                 response.raise_for_status()
#                 print("✅ 사진+메시지 전송 완료")
#             else:
#                 # ✅ 메시지가 길면 사진만 먼저 전송
#                 photo_payload = {
#                     "chat_id": TELEGRAM_CHANNEL_ID,
#                     "photo": image_urls[0]
#                 }
#                 response = requests.post(send_photo_url, data=photo_payload)
#                 response.raise_for_status()
#                 print("✅ 사진 전송 완료 (텍스트는 별도 전송)")

#                 # 전체 메시지 텍스트 전송 (분할 없이)
#                 text_payload = {
#                     "chat_id": TELEGRAM_CHANNEL_ID,
#                     "text": message
#                 }
#                 response = requests.post(send_text_url, data=text_payload)
#                 response.raise_for_status()
#                 print("✅ 텍스트 전송 완료")
#         else:
#             # ✅ 이미지가 없을 경우 전체 메시지만 전송
#             text_payload = {
#                 "chat_id": TELEGRAM_CHANNEL_ID,
#                 "text": message
#             }
#             response = requests.post(send_text_url, data=text_payload)
#             response.raise_for_status()
#             print("✅ 텍스트 전송 완료 (이미지 없음)")

#     except Exception as e:
#         print("❌ 전송 실패:", e)
#         print("📦 실패한 메시지:", message)

def send_to_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": message
    }
    try:
        response = requests.post(url, data=payload, timeout=TELEGRAM_TIMEOUT)
        response.raise_for_status()
    except (RequestException, Timeout) as e:
        print("❌ 텔레그램 전송 실패(네트워크/타임아웃):", e)
        print("📦 실패한 메시지:", message)
    except Exception as e:
        print("❌ 텔레그램 전송 실패(기타):", e)
        print("📦 실패한 메시지:", message)

def bootstrap_warm_start(user_id: str, username: str):
    """
    last_id 파일이 없을 때: 최신 트윗 ID만 저장하고 전송은 스킵.
    max_results 최소 5로 보장.
    """
    try:
        resp = call_with_retry(
            client.get_users_tweets,
            id=user_id,
            max_results=5,  # 최소값 5
            exclude=["replies", "retweets"],
            tweet_fields=["id", "created_at"]
        )
        if not resp.data:
            # 트윗 자체가 없을 수도 있으니 0으로 마킹
            save_last_id(user_id, 0)
            print(f"👤 @{username} warm-start: 트윗 없음 → last_id=0 저장(스킵)")
            return

        latest_id = max(t.id for t in resp.data)
        save_last_id(user_id, latest_id)
        print(f"👤 @{username} warm-start: last_id={latest_id} 저장(전송 스킵)")
    except Exception as e:
        print(f"❌ warm-start 실패 @{username}: {e}")

# === [TRUMP RSS] 유틸 ===
def _ts_file_path():
    return os.path.join(BASE_DIR, TRUMP_STATE_FILE)

def trump_load_last_ts() -> float:
    try:
        with open(_ts_file_path(), "r", encoding="utf-8") as f:
            return float(f.read().strip())
    except FileNotFoundError:
        return 0.0
    except Exception:
        return 0.0

def trump_save_last_ts(ts: float):
    with open(_ts_file_path(), "w", encoding="utf-8") as f:
        f.write(str(ts))

def trump_entry_ts(e) -> float:
    for k in ("published", "updated"):
        if k in e and e[k]:
            try:
                return parsedate_to_datetime(e[k]).timestamp()
            except Exception:
                pass
    return 0.0

def trump_fetch_entries_sorted():
    feed = feedparser.parse(RSS_URL)
    entries = feed.entries or []
    entries.sort(key=trump_entry_ts)  # 오래된 → 최신
    return entries

def trump_clean_text(html_text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html_text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

def trump_extract_image_urls(entry) -> List[str]:
    urls = []
    html_parts = []
    for key in ("summary", "content", "content:encoded"):
        v = entry.get(key)
        if isinstance(v, list):
            html_parts += [c.get("value", "") for c in v]
        elif v:
            html_parts.append(v)
    html_blob = "\n".join([str(x) for x in html_parts if x])
    for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html_blob, flags=re.I):
        url = m.group(1).strip()
        if url and url not in urls:
            urls.append(url)
    if "enclosures" in entry:
        for enc in entry.enclosures:
            href = enc.get("href")
            typ = (enc.get("type") or "").lower()
            if href and ("image" in typ or href.lower().endswith((".jpg",".jpeg",".png",".gif",".webp"))):
                if href not in urls:
                    urls.append(href)
    return urls

def _format_mmdd_hhmm_utc(dt: datetime) -> str:
    # X 코드와 동일하게 UTC 기준으로 "%m/%d %H:%M"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%m/%d %H:%M")

def trump_format_message_like_twitter(entry_text: str, published_dt: Optional[datetime], username: str, link: str, images: List[str]) -> str:
    # 번역 파이프라인 재사용(이모지/URL 보존)
    translated = translate_preserving_emojis_and_urls(entry_text)
    if translated is None:
        translated = "[번역 실패: 모든 엔진에서 오류 발생]"
    created_at = _format_mmdd_hhmm_utc(published_dt or datetime.utcnow().replace(tzinfo=timezone.utc))
    # X 메시지 포맷과 동일하게 구성
    msg = (
        f"🐦 원문:\n{entry_text}\n\n"
        f"🌐 번역:\n{translated}\n\n🔗"
        f"👤 작성자 : {username}\n"
        f"🕒 작성 시각: {created_at}\n"
    )
    # 원문 링크는 메시지 본문에 굳이 필수는 아니지만, 필요하면 아래 줄 추가:
    # msg += f"\n🔗 원문: {link}\n"
    return msg

def trump_fetch_new_entries():
    entries = trump_fetch_entries_sorted()
    last_ts = trump_load_last_ts()
    new_items = [e for e in entries if trump_entry_ts(e) > last_ts]
    if entries:
        newest_ts = max(trump_entry_ts(e) for e in entries)
        trump_save_last_ts(newest_ts)
    return new_items

def trump_first_run_backfill(count: int = 3):
    entries = trump_fetch_entries_sorted()
    if not entries:
        print("⚠️ [TRUMP RSS] 피드 비어있음, 백필 불가")
        return
    backfill = entries[-count:] if count > 0 else []
    print(f"🚀 [TRUMP RSS] 첫 실행 백필: 최신 {len(backfill)}개 전송")
    for e in backfill:
        # 본문/링크/시각
        link = (e.get("link") or "").strip()
        body = trump_clean_text(e.get("summary") or "")
        # 시각 파싱
        pub_raw = (e.get("published") or e.get("updated") or "").strip()
        pub_dt = None
        try:
            if pub_raw:
                pub_dt = parsedate_to_datetime(pub_raw)
        except Exception:
            pub_dt = None
        imgs = trump_extract_image_urls(e)
        msg = trump_format_message_like_twitter(body, pub_dt, TRUMP_USERNAME, link, imgs)
        send_to_telegram_with_optional_image(msg, imgs)
    newest_ts = max(trump_entry_ts(e) for e in entries)
    trump_save_last_ts(newest_ts)

def trump_poll_once():
    """트루스소셜 새 글을 한 번 폴링하여 X와 동일 포맷으로 텔레그램 전송"""
    try:
        if trump_load_last_ts() == 0.0:
            # 첫 실행이면 백필 후 상태 저장
            trump_first_run_backfill(3)
        new_entries = trump_fetch_new_entries()
        if not new_entries:
            print("🔎 [TRUMP RSS] 새 글 없음")
            return
        for e in new_entries:
            link = (e.get("link") or "").strip()
            body = trump_clean_text(e.get("summary") or "")
            if not body.strip():
                print("⚠️ [TRUMP RSS] 본문 없음, 스킵:", link)
                continue
            
            pub_raw = (e.get("published") or e.get("updated") or "").strip()
            pub_dt = None
            try:
                if pub_raw:
                    pub_dt = parsedate_to_datetime(pub_raw)
            except Exception:
                pub_dt = None
            imgs = trump_extract_image_urls(e)
            msg = trump_format_message_like_twitter(body, pub_dt, TRUMP_USERNAME, link, imgs)
            send_to_telegram_with_optional_image(msg, imgs)
            print("✅ [TRUMP RSS] 텔레그램 전송 완료")
    except Exception as ex:
        print("❌ [TRUMP RSS] 처리 오류:", ex)

def explain_tweepy_error(e):
    try:
        code = getattr(getattr(e, "response", None), "status_code", None)
        body = getattr(getattr(e, "response", None), "text", "")
        print(f"❌ Tweepy HTTP {code}: {body}")
    except Exception:
        print(f"❌ Tweepy error: {repr(e)}")

def run():
    print("트윗 모니터링 시작...")
    print(f"📏 텍스트 길이 임계값: {TEXT_LENGTH_THRESHOLD}자")
    
    # 메모리 모니터링 변수
    last_memory_check = time.time()
    MEMORY_CHECK_INTERVAL = 1800  # 30분마다 메모리 체크
    
    try:
        while True:
            # --- [TRUMP RSS] 먼저 한 번 폴링 ---
            trump_poll_once()
            
            for idx, (user_id, username) in enumerate(zip(TWITTER_USER_IDS, TWITTER_USERNAMES)):
                try:
                    print(f"\n🚀 사용자 @{username} 확인 중...")

                    last_id = get_last_id(user_id)

                    # 🔰 last_id 파일이 없으면: 최신 ID만 저장하고 이번 라운드는 스킵
                    if last_id is None:
                        bootstrap_warm_start(user_id, username)
                        continue

                    max_tweet_id = last_id  # 이번 라운드에서 본 것 중 가장 큰 id 저장용
                    fetched_any = False

                    print(user_id)

                    # ✅ 페이지네이션으로 since_id 이후 전부 가져오기
                    for tweet, includes in iterate_user_tweets(user_id, last_id, page_size=100):
                        fetched_any = True

                        # 엘론(44196397) + quote 제외 규칙이 있으면 유지
                        if user_id in EXCLUDE_QUOTE_USERS and tweet.referenced_tweets:
                            if any(ref.type == "quoted" for ref in tweet.referenced_tweets):
                                print(f"🛑 @{username} quote 트윗 제외: {tweet.id}")
                                # 다음 트윗으로
                                if max_tweet_id is None or tweet.id > max_tweet_id:
                                    max_tweet_id = tweet.id
                                continue
                            
                        print("✅ 새 트윗 발견(id):", tweet.id)
                        print("✅ 새 트윗 작성 시각(created_at):", tweet.created_at)
                        print("✅ 새 트윗 발견(text):", tweet.text)

                        # 리트윗이면 원본 텍스트/이미지 추출, 아니면 그대로 처리
                        if tweet.referenced_tweets:
                            full_text, image_urls = fetch_original_retweet(tweet, client, username)
                        else:
                            full_text = get_full_tweet_text(tweet, username)
                            image_urls = extract_image_urls(tweet, includes)

                        created_at = tweet.created_at.strftime("%m/%d %H:%M")

                        # 번역 (None 가드)
                        translated_text = translate_preserving_emojis_and_urls(full_text)
                        if translated_text is None:
                            translated_text = "[번역 실패: 모든 엔진에서 오류 발생]"

                        message = (
                            f"🐦 원문:\n{full_text}\n\n"
                            f"🌐 번역:\n{translated_text}\n\n🔗"
                            f"👤 작성자 : {username}\n"
                            f"🕒 작성 시각: {created_at}\n"
                        )

                        send_to_telegram_with_optional_image(message, image_urls)
                        print("텔레그램 전송 완료.")

                        # 라운드 최대 tweet_id 업데이트
                        if max_tweet_id is None or tweet.id > max_tweet_id:
                            max_tweet_id = tweet.id

                    # 이번 사용자 라운드에서 무언가 가져왔으면 last_id 갱신
                    if fetched_any and max_tweet_id:
                        save_last_id(user_id, max_tweet_id)
                        print(f"👤 작성자 @{username} 📌 max_tweet_id 저장됨: {max_tweet_id}")
                    else:
                        print(f"👤 작성자 @{username} 🔍 새 트윗 없음.")

                    # 메모리 모니터링 및 정리
                    current_time = time.time()
                    if current_time - last_memory_check > MEMORY_CHECK_INTERVAL:
                        monitor_memory_usage()
                        last_memory_check = current_time

                except Exception as e:
                    explain_tweepy_error(e)
                    # ✅ 여기서 잡아주면 503 등 일시 오류에도 프로세스가 죽지 않음
                    print(f"⚠️ @{username} 처리 중 오류: {e}")
                    time.sleep(10)  # 짧게 쉬고 다음 사용자/다음 라운드 진행
                    continue
                    
            print("마지막 실행 시간 : ", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            time.sleep(CHECK_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n🛑 프로그램 종료 요청됨")
    except Exception as e:
        print(f"❌ 예상치 못한 오류: {e}")
    finally:
        cleanup_resources()
        print("🧹 프로그램 정리 완료")

def monitor_memory_usage():
    """메모리 사용량 모니터링"""
    try:
        process = psutil.Process()
        memory_info = process.memory_info()
        memory_mb = memory_info.rss / 1024 / 1024
        
        print(f"💾 현재 메모리 사용량: {memory_mb:.1f}MB")
        
        # 메모리 사용량이 너무 높으면 가비지 컬렉션 실행
        if memory_mb > 500:  # 500MB 이상이면
            print("🧹 메모리 정리 중...")
            gc.collect()
            print("✅ 메모리 정리 완료")
            
    except Exception as e:
        print(f"⚠️ 메모리 모니터링 오류: {e}")

def cleanup_resources():
    """리소스 정리"""
    global crawler
    try:
        if crawler:
            crawler.close()
            print("🧹 크롤러 정리 완료")
    except Exception as e:
        print(f"⚠️ 크롤러 정리 중 오류: {e}")
    
    # 가비지 컬렉션 실행
    try:
        gc.collect()
        print("🧹 메모리 정리 완료")
    except Exception as e:
        print(f"⚠️ 메모리 정리 중 오류: {e}")

def debug_single_tweet(tweet_id: str, username: str):
    """특정 tweet_id만 단발 테스트"""
    try:
        resp = client.get_tweet(
            id=tweet_id,
            tweet_fields=["created_at","text","attachments","referenced_tweets"],
            expansions=["attachments.media_keys","referenced_tweets.id"],
            media_fields=["url","type"]
        )
        tweet = resp.data
        print("🔎 Debug Tweet")
        print("id:", tweet.id)
        print("created_at:", tweet.created_at)
        print("text:", tweet.text)
        print("referenced_tweets:", tweet.referenced_tweets)
        print("attachments:", tweet.attachments)
        print("includes:", resp.includes)

        # 전체 텍스트 가져오기 (크롤링 포함)
        full_text = get_full_tweet_text(tweet, username)
        print("📜 full_text:", full_text)

        # 번역
        translated = translate_preserving_emojis_and_urls(full_text)
        print("🌐 translated:", translated)

        # 이미지 추출
        image_urls = extract_image_urls(tweet, resp.includes)
        print("🖼️ image_urls:", image_urls)

        # 텔레그램 전송 (테스트라면 주석처리 가능)
        created_at = tweet.created_at.strftime("%m/%d %H:%M")
        message = (
            f"🐦 원문:\n{full_text}\n\n"
            f"🌐 번역:\n{translated}\n\n"
            f"👤 작성자 : {username}\n"
            f"🕒 작성 시각: {created_at}\n"
        )
        send_to_telegram_with_optional_image(message, image_urls)
        print("✅ 텔레그램 전송 완료")

    except Exception as e:
        print("❌ debug_single_tweet 오류:", e)


if __name__ == "__main__":
    # run() 대신 단일 테스트 실행
    # debug_single_tweet("1960800720061370580", "wallstengine")
    run()