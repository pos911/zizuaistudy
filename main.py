import os
import json
import sqlite3
import requests
import time
from google import genai
from datetime import datetime, timedelta
from dotenv import load_dotenv

# 로컬 환경 변수 로드 (.env 파일이 있을 경우)
load_dotenv()

def get_env():
    """GitHub Secrets(JSON) 또는 로컬 환경 변수에서 설정값 로드"""
    env_json = os.getenv("ENV_JSON")
    if env_json:
        # GitHub Actions 등 서버 환경 (JSON 객체 방식)
        try:
            return json.loads(env_json)
        except Exception as e:
            print(f"![오류] ENV_JSON 파싱 실패: {e}")
    
    # 로컬 환경 또는 개별 등록 방식
    return {
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
        "NAVER_CLIENT_ID": os.getenv("NAVER_CLIENT_ID"),
        "NAVER_CLIENT_SECRET": os.getenv("NAVER_CLIENT_SECRET"),
        "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID")
    }

# 설정값 할당
config = get_env()
client = genai.Client(api_key=config.get("GEMINI_API_KEY"))
NAVER_ID = config.get("NAVER_CLIENT_ID")
NAVER_SECRET = config.get("NAVER_CLIENT_SECRET")
TG_TOKEN = config.get("TELEGRAM_TOKEN")
TG_CHAT_ID = config.get("TELEGRAM_CHAT_ID")
DB_PATH = "news.db"

def init_db():
    """DB 초기화 및 3일 전 데이터 자동 삭제"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS news 
                    (title TEXT UNIQUE, link TEXT, description TEXT, 
                     pubDate TEXT, summary TEXT, sentiment TEXT, created_at DATE)''')
    
    # 3일 전 뉴스 데이터 정리 (관리 효율성)
    three_days_ago = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    cursor.execute("DELETE FROM news WHERE created_at < ?", (three_days_ago,))
    conn.commit()
    conn.close()

def get_naver_news(query):
    """네이버 뉴스 검색 (최신순 10건)"""
    url = f"https://openapi.naver.com/v1/search/news.json?query={query}&display=10&sort=date"
    headers = {"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET}
    res = requests.get(url, headers=headers)
    return res.json().get('items', []) if res.status_code == 200 else []

def analyze_batch(news_list):
    """여러 뉴스를 한 번의 API 호출로 통합 분석 (RPD 절약 핵심)"""
    if not news_list:
        return []

    combined_text = ""
    for idx, news in enumerate(news_list, 1):
        combined_text += f"[{idx}] 제목: {news['title']}\n내용: {news['desc']}\n\n"

    prompt = f"""
    당신은 금융 분석 전문가입니다. 다음 {len(news_list)}개의 뉴스를 각각 분석하여 
    형식에 맞춰 [긍정/부정/중립] 여부와 한줄 요약을 작성하세요.
    각 분석 결과 사이에는 '###' 구분자를 넣어주세요.

    {combined_text}
    """

    try:
        # 단 1회 호출로 모든 뉴스 처리
        response = client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
        # 결과 파싱 (### 구분자 기준)
        analysis_results = response.text.split('###')
        return [res.strip() for res in analysis_results]
    except Exception as e:
        print(f"![오류] 통합 분석 중 에러 발생: {e}")
        return ["미분류 (분석 실패)"] * len(news_list)

def main():
    init_db()
    print("[*] 한국투자증권 뉴스 모니터링 가동 (통합 분석 모드)")
    
    items = get_naver_news("한국투자증권")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    new_to_analyze = []  # 분석 대기 리스트
    final_messages = []  # 텔레그램 전송용

    # 1단계: 신규 뉴스만 필터링
    for item in items:
        title = item['title'].replace('<b>', '').replace('</b>', '').replace('&quot;', '"')
        cursor.execute("SELECT title FROM news WHERE title=?", (title,))
        
        if not cursor.fetchone():
            new_to_analyze.append({
                'title': title,
                'link': item['link'],
                'desc': item['description'].replace('<b>', '').replace('</b>', ''),
                'pubDate': item['pubDate']
            })

    # 2단계: 통합 분석 실행 (API 호출 1회 소모)
    if new_to_analyze:
        print(f"[*] {len(new_to_analyze)}건의 신규 뉴스 발견. 통합 분석을 시작합니다...")
        analysis_data = analyze_batch(new_to_analyze)
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        for i, news in enumerate(new_to_analyze):
            summary = analysis_data[i] if i < len(analysis_data) else "요약 생성 누락"
            sentiment = "👍긍정" if "긍정" in summary else "👎부정" if "부정" in summary else "😐중립"
            
            cursor.execute("""INSERT INTO news (title, link, description, pubDate, summary, sentiment, created_at) 
                              VALUES (?, ?, ?, ?, ?, ?, ?)""", 
                           (news['title'], news['link'], news['desc'], news['pubDate'], summary, sentiment, today_str))
            
            final_messages.append(f"{i+1}. {sentiment}\n{news['title']}\n<a href='{news['link']}'>🔗 기사보기</a>")

        conn.commit()
        
        message = f"<b>[신규 뉴스 통합 분석 리스트]</b>\n\n" + "\n\n".join(final_messages)
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True})
        print(f"[*] 분석 및 전송 완료.")
    else:
        print("[*] 분석할 새로운 뉴스가 없습니다.")

    conn.close()

if __name__ == "__main__":
    main()