import os
import json
import sqlite3
import requests
import time
from google import genai
from google.genai import types
from google.genai.errors import ClientError
from datetime import datetime, timedelta
from dotenv import load_dotenv

# 로컬 환경 변수 로드
load_dotenv()

def get_env(key):
    """환경변수를 가져오되, 없으면 에러를 발생시킴"""
    value = os.getenv(key)
    if value is None:
        print(f"![CRITICAL] 환경변수 누락: {key}")
        return None
    return value

# 필수 환경변수 검증 및 로드
REQUIRED_VARS = ["GEMINI_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]
missing_vars = [key for key in REQUIRED_VARS if os.getenv(key) is None]

if missing_vars:
    print(f"![CRITICAL] 필수 환경변수가 누락되었습니다: {', '.join(missing_vars)}")
    import sys
    sys.exit(1)

# 설정값 할당
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
NAVER_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_SECRET = os.getenv("NAVER_CLIENT_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
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
    """네이버 뉴스 검색 (20건)"""
    url = f"https://openapi.naver.com/v1/search/news.json?query={query}&display=20&sort=date"
    headers = {"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        return res.json().get('items', []) if res.status_code == 200 else []
    except Exception as e:
        print(f"![오류] 네이버 뉴스 검색 실패: {e}")
        return []

def analyze_batch_filtered(news_list):
    """
    [핵심 수정] JSON 구조화된 응답을 요청하여 인덱스 밀림 방지 및 품질 향상
    """
    if not news_list:
        return []

    # 프롬프트에 전달할 뉴스 목록 구성 (ID 포함)
    news_content = json.dumps([
        {"id": news['id'], "title": news['title'], "description": news['desc']} 
        for news in news_list
    ], ensure_ascii=False, indent=2)

    prompt = f"""
    당신은 '기업 평판 리스크 관리 전문가'입니다.
    주어지는 뉴스 목록을 분석하여, '한국투자증권' 기업 자체의 리스크나 브랜딩에 관련된 중요 뉴스만 선별하세요.

    [분석 규칙]
    1. **PASS 처리 대상 (엄격히 적용)**:
       - 단순 주식 시황, 목표주가 변동(상향/하향/유지), 투자의견(Buy/Hold) 리포트
       - 단순 실적 공시 나열, 특징주 언급, 종목 추천 기사
    2. **KEEP 처리 대상**:
       - 기업 경영 이슈, 사고, 법적 분쟁, 새로운 서비스 출시, CEO 동정, 대규모 투자/제휴 등 기업 실체와 관련된 뉴스
    3. **출력 형식 (JSON)**:
       - 반드시 아래 JSON 스키마를 따르는 리스트만 출력하세요. 다른 말은 절대 금지합니다.
       - status는 "KEEP" 또는 "PASS" 중 하나여야 합니다.
       - sentiment는 "긍정", "부정", "중립" 중 하나여야 합니다. KEEP인 경우 필수로 작성하고, PASS인 경우 비워두거나 무시합니다.
       - 감성은 기업 입장에서의 유불리를 따지세요.
       
    [JSON Schema]
    [
      {{
        "id": <뉴스ID (정수)>,
        "status": "KEEP" or "PASS",
        "sentiment": "<감성>",
        "summary": "<한 줄 핵심 요약>"
      }},
      ...
    ]

    [분석할 뉴스 목록]
    {news_content}
    """

    for attempt in range(3): # 최대 3회 재시도
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash', 
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json" # JSON 응답 강제
                )
            )
            
            # JSON 파싱
            return json.loads(response.text)

        except ClientError as e:
            if e.code == 429:
                print(f"⏳ Quota exceeded (Attempt {attempt+1}/3). Waiting 60s...")
                time.sleep(60)
                continue
            print(f"![오류] API 클라이언트 에러: {e}")
            break
        except json.JSONDecodeError:
            print(f"![오류] JSON 파싱 실패. 응답이 올바르지 않습니다.")
            # 파싱 실패 시 재시도 할 수도 있지만, 여기서는 생략
            break
        except Exception as e:
            print(f"![오류] 통합 분석 중 에러: {e}")
            break
            
    return [] # 실패 시 빈 리스트 반환

def main():
    init_db()
    print("[*] 한국투자증권 뉴스 필터링 시스템 가동 (JSON Mode)")
    
    items = get_naver_news("한국투자증권")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    new_to_analyze = []
    
    # 1. 파이썬 레벨 키워드 필터링 (비용 절감)
    EXCLUDE_KEYWORDS = ['목표주가', '목표가', '투자의견', '상향', '하향', '유지', '매수', '매도', 'Buy', 'Hold', 'Target Price', '특징주']

    for idx, item in enumerate(items):
        title = item['title'].replace('<b>', '').replace('</b>', '').replace('&quot;', '"')
        desc = item['description'].replace('<b>', '').replace('</b>', '').replace('&quot;', '"')
        
        # 중복 확인
        cursor.execute("SELECT title FROM news WHERE title=?", (title,))
        if cursor.fetchone():
            continue

        # 키워드 필터링
        if any(keyword in title for keyword in EXCLUDE_KEYWORDS):
            print(f"[1차 필터] 제외됨(제목 키워드): {title}")
            continue

        # 분석 대상에 추가 (ID 부여)
        new_to_analyze.append({
            'id': idx,
            'title': title,
            'link': item['link'],
            'desc': desc,
            'pubDate': item['pubDate']
        })

    # 2. AI 배치 분석
    if new_to_analyze:
        print(f"[*] {len(new_to_analyze)}건 AI 분석 요청...")
        analysis_results = analyze_batch_filtered(new_to_analyze)
        
        # 분석 결과 매핑을 위한 딕셔너리 생성
        result_map = {res['id']: res for res in analysis_results if 'id' in res and 'status' in res}
        
        final_messages = []
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        for news in new_to_analyze:
            res = result_map.get(news['id'])
            
            # 결과가 없거나 PASS인 경우 저장 안 함
            if not res or res.get('status') != 'KEEP':
                reason = "AI PASS" if res else "분석 실패/누락"
                print(f"[2차 필터] {reason}: {news['title']}")
                continue
            
            # KEEP인 경우 저장 및 전송
            sentiment = res.get('sentiment', '중립')
            summary = res.get('summary', '요약 없음')
            
            # 감성 이모지 추가
            if "긍정" in sentiment: sentiment_display = "👍긍정"
            elif "부정" in sentiment: sentiment_display = "👎부정"
            else: sentiment_display = "⚖️중립"

            print(f"[저장] {sentiment_display} | {news['title']}")

            cursor.execute("INSERT INTO news (title, link, description, pubDate, summary, sentiment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                           (news['title'], news['link'], news['desc'], news['pubDate'], summary, sentiment_display, today_str))
            
            final_messages.append(f"{sentiment_display} <b>{news['title']}</b>\n{summary}\n<a href='{news['link']}'>🔗 기사보기</a>")
        
        conn.commit()
        
        # 텔레그램 전송
        if final_messages:
            message = f"<b>[한국투자증권 기업 주요 뉴스]</b>\n\n" + "\n\n".join(final_messages)
            try:
                requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                              json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=10)
                print(f"[*] {len(final_messages)}건 전송 완료.")
            except Exception as e:
                print(f"![전송 오류] {e}")
        else:
            print("[*] 전송할 뉴스가 없습니다 (모두 필터링됨).")
            
    else:
        print("[*] 신규 분석 대상 뉴스가 없습니다.")

    conn.close()

if __name__ == "__main__":
    main()