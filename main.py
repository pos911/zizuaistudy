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
        try:
            return json.loads(env_json)
        except Exception as e:
            print(f"![오류] ENV_JSON 파싱 실패: {e}")
    
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
    
    three_days_ago = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    cursor.execute("DELETE FROM news WHERE created_at < ?", (three_days_ago,))
    conn.commit()
    conn.close()

def get_naver_news(query):
    """네이버 뉴스 검색 (필터링을 고려하여 20건 검색)"""
    url = f"https://openapi.naver.com/v1/search/news.json?query={query}&display=20&sort=date"
    headers = {"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET}
    res = requests.get(url, headers=headers)
    return res.json().get('items', []) if res.status_code == 200 else []

def analyze_batch_filtered(news_list):
    """
    [핵심 수정] 기업 브랜딩/리스크 뉴스 선별 분석
    """
    if not news_list:
        return []

    combined_text = ""
    for idx, news in enumerate(news_list, 1):
        combined_text += f"[{idx}] 제목: {news['title']}\n내용: {news['desc']}\n\n"

    prompt = f"""
    당신은 기업 평판 리스크 관리 전문가입니다. 
    다음 {len(news_list)}개의 뉴스를 분석하여 '한국투자증권' 기업 자체의 이슈만 선별하세요.

    [절대 규칙]
    1. '한국투자증권'이 단순히 주식 종목을 분석하거나 목표주가를 제시한 리포트 기사는 무조건 "PASS"라고만 출력하세요.
    2. 선별된 기사는 반드시 아래 포맷만 출력하세요. (잡다한 설명 금지)
       [감성] | 요약문
    3. 감성은 [긍정], [부정], [중립] 중 하나만 사용하세요.
    4. 각 뉴스 결과 사이에는 반드시 '###' 구분자를 넣어주세요.

    뉴스 목록:
    {combined_text}
    """

    try:
        response = client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
        results = response.text.split('###')
        
        # 개수 보정 (응답 개수가 안 맞을 경우 대비)
        if len(results) < len(news_list):
            results.extend(["PASS"] * (len(news_list) - len(results)))
            
        return [res.strip() for res in results]
    except Exception as e:
        print(f"![오류] 통합 분석 중 에러: {e}")
        return ["PASS"] * len(news_list)

def main():
    init_db()
    print("[*] 한국투자증권 뉴스 필터링 시스템 가동 (Strict Mode)")
    
    items = get_naver_news("한국투자증권")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    new_to_analyze = []
    final_messages = []
    
    # 1차: 파이썬 키워드 필터 (리포트 용어)
    # 제목에 이 단어가 있으면 API 호출조차 하지 않음 (비용 0원)
    EXCLUDE_KEYWORDS = ['목표주가', '목표가', '투자의견', '상향', '하향', '유지', '매수', '매도', 'Buy', 'Hold', 'Target Price']

    for item in items:
        title = item['title'].replace('<b>', '').replace('</b>', '').replace('&quot;', '"')
        desc = item['description'].replace('<b>', '').replace('</b>', '')
        
        cursor.execute("SELECT title FROM news WHERE title=?", (title,))
        if cursor.fetchone():
            continue

        if any(keyword in title for keyword in EXCLUDE_KEYWORDS):
            print(f"[1차 필터] 제외됨(제목 키워드): {title}")
            continue

        new_to_analyze.append({
            'title': title,
            'link': item['link'],
            'desc': desc,
            'pubDate': item['pubDate']
        })

    # 2차: AI 분석 및 결과 처리
    if new_to_analyze:
        print(f"[*] {len(new_to_analyze)}건 분석 시작...")
        analysis_results = analyze_batch_filtered(new_to_analyze)
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        for i, news in enumerate(new_to_analyze):
            if i >= len(analysis_results): break
            
            raw_result = analysis_results[i].strip()
            
            # ------------------------------------------------------------------
            # [수정된 핵심 로직] PASS가 포함되면 무조건 삭제 (대소문자 무시)
            # "제외할 기사: PASS", "[PASS]" 등 어떤 형태든 PASS가 들어가면 다 죽임
            # ------------------------------------------------------------------
            if "PASS" in raw_result.upper():
                print(f"[2차 필터] AI 제외(PASS): {news['title']}")
                continue
            
            # 안전장치: 혹시 PASS를 안 썼는데 내용이 리포트인 경우 한번 더 거름
            if any(bad in raw_result for bad in EXCLUDE_KEYWORDS):
                print(f"[2차 필터] 내용 부적절: {news['title']}")
                continue

            # 포맷 클리닝 (잡다한 접두어 제거)
            clean_result = raw_result.replace("- 선별된 기사:", "").replace("선별된 기사:", "").strip()
            
            # 파싱
            if "|" in clean_result:
                parts = clean_result.split('|', 1)
                sentiment = parts[0].strip()
                summary = parts[1].strip()
            else:
                # 형식이 깨졌지만 유효한 내용인 경우
                sentiment = "🔔알림" 
                summary = clean_result

            # DB 저장
            cursor.execute("INSERT INTO news (title, link, description, pubDate, summary, sentiment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                           (news['title'], news['link'], news['desc'], news['pubDate'], summary, sentiment, today_str))
            
            # 메시지 포맷
            final_messages.append(f"{sentiment} <b>{news['title']}</b>\n{summary}\n<a href='{news['link']}'>🔗 기사보기</a>")

        conn.commit()
        
        if final_messages:
            message = f"<b>[한국투자증권 기업 주요 뉴스]</b>\n\n" + "\n\n".join(final_messages)
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                          json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True})
            print(f"[*] {len(final_messages)}건 전송 완료.")
        else:
            print("[*] 전송할 뉴스가 없습니다 (모두 필터링됨).")
    else:
        print("[*] 신규 뉴스가 없습니다.")

    conn.close()

if __name__ == "__main__":
    main()