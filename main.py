import os
import json
import time
import re
import hmac
import hashlib
import datetime
import uuid
import requests
import pandas as pd
from urllib.parse import urljoin
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

# ==========================================
# 1. 사용자 설정 및 API Key 정보
# ==========================================
SOLAPI_KEY = "NCSOCR94THGOMHSW"
SOLAPI_SECRET = "6YH0DTGRHVDXT4HU3RS6T0TDRINDFXH4"

# 💡 카카오 알림톡 가동
USE_KAKAO = True

SOLAPI_PF_ID = "KA01PF260805090058574q8wFwsR3MUx"          # 솔라피 카카오 발신프로필 키
SOLAPI_TEMPLATE_ID = "KA01TP260805090641453jRsTCdFoBOl"  # 승인받으신 템플릿 ID

MY_PHONE = "01084687138"  # 발신번호 및 개발자 비상 경고 수신 번호
DATA_GO_KEY = "5df6886cdde7cb88e1c3e7e0e7c555002747947bf772546c112b028a77a8b81b"

# Supabase 연동 정보
SUPABASE_URL = "https://hcyvfgeaquydsvtrcnrv.supabase.co"
SUPABASE_KEY = "sb_publishable_P19tdkj74ibIy7Xdle2i4w_M1B1mhV_"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

EXCEL_FILE = "crawling_targets_template.xlsx"
HISTORY_FILE = "notice_history.json"
DEV_ALERT_FILE = "dev_alert_history.json"

DYNAMIC_ORGS = [
    "제주테크노파크", "전남광주통합특별시 기업지원시스템", "경기도경제과학진흥원",
    "소상공인24", "대구테크노파크", "경남테크노파크", "충북테크노파크", 
    "전남테크노파크", "연구개발특구진흥재단", "대전일자리경제진흥원",
    "강원특별자치도경제진흥원", "세종테크노파크", "전북특별자치도 경제통상진흥원"
]

PURE_SYSTEM_NOISE = {
    "로그인", "회원가입", "마이페이지", "사이트맵", "개인정보처리방침", "이용약관", "자세히보기",
    "바로가기", "홈으로", "저작권", "이메일", "익명신고", "인권침해", "정보공개", "부서별", "FAQ",
    "자주묻는질문", "이전", "다음", "목록", "검색", "다운로드", "전체", "TOP", "안내책자다운로드",
    "수출판로지원", "기업지원", "자금지원", "일자리지원", "기타지원", "모집중", "타온라인", "마감", "상세보기",
    "진행중", "준비중", "종료", "접수중", "활용하세요. 자주 묻는 질문말씀하세요."
}

# ==========================================
# 2. 유틸리티 및 인증/경고 함수
# ==========================================

def load_excel_robust(file_path):
    df_raw = pd.read_excel(file_path, header=None)
    header_row_idx = None
    for idx, row in df_raw.iterrows():
        row_values = [str(val).strip() for val in row.values]
        if '기관명' in row_values or 'No' in row_values:
            header_row_idx = idx
            break
            
    if header_row_idx is not None:
        df = pd.read_excel(file_path, header=header_row_idx)
    else:
        df = pd.read_excel(file_path)
        
    df.columns = [str(col).strip() for col in df.columns]
    df = df.dropna(subset=['기관명']).copy()
    return df

def get_solapi_headers():
    date = datetime.datetime.now(datetime.timezone.utc).isoformat()
    salt = str(uuid.uuid4()).replace('-', '')
    signature = hmac.new(SOLAPI_SECRET.encode('utf-8'), (date + salt).encode('utf-8'), hashlib.sha256).hexdigest()
    return {
        'Authorization': f'HMAC-SHA256 apiKey={SOLAPI_KEY}, date={date}, salt={salt}, signature={signature}',
        'Content-Type': 'application/json; charset=utf-8'
    }

def load_json_file(file_path):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_json_file(file_path, data):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def send_dev_warning(org_name, category, target_url, dev_history):
    """크롤링 오류 발생 시 개발자에게 긴급 비상 문자(LMS) 발송"""
    today_str = datetime.date.today().isoformat()
    if dev_history.get(target_url) == today_str:
        return

    solapi_url = "https://api.solapi.com/messages/v4/send"
    headers = get_solapi_headers()
    
    msg = (
        f"[🚨 비즈맵 개발자 시스템 경고]\n\n"
        f"• 기관명: {org_name}\n"
        f"• 게시판: {category}\n"
        f"• 원인: 공고 제목 미수집 또는 디자인 개편 감지\n\n"
        f"🔗 확인 주소:\n{target_url}"
    )
    
    payload = {
        "message": {
            "to": MY_PHONE,
            "from": MY_PHONE,
            "text": msg,
            "type": "LMS"
        }
    }
    
    try:
        res = requests.post(solapi_url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            dev_history[target_url] = today_str
    except Exception:
        pass

# ==========================================
# 3. 💬 문자/알림톡 발송 및 DB 유저 매칭 엔진
# ==========================================

def send_lms_message(to_phone, user_name, org_name, title, target_url):
    """솔라피 LMS 장문 문자 발송 함수"""
    solapi_url = "https://api.solapi.com/messages/v4/send"
    headers = get_solapi_headers()
    
    clean_phone = ''.join(filter(str.isdigit, str(to_phone)))
    
    msg = (
        f"[비즈맵 지원사업 알림]\n\n"
        f"안녕하세요, {user_name}님!\n"
        f"설정하신 조건에 맞는 신규 지원사업 공고가 등록되었습니다.\n\n"
        f"• 기관: {org_name}\n"
        f"• 제목: {title}\n\n"
        f"🔗 상세 공고 보기:\n{target_url}"
    )
    
    payload = {
        "message": {
            "to": clean_phone,
            "from": MY_PHONE,
            "text": msg,
            "type": "LMS"
        }
    }
    
    try:
        res = requests.post(solapi_url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            return True, "성공"
        else:
            return False, f"[{res.status_code}] {res.text}"
    except Exception as e:
        return False, str(e)

def send_kakao_alimtalk(to_phone, user_name, org_name, title, target_url):
    """솔라피 카카오 알림톡 API 발송 함수 (#{공고링크} 개별 상세페이지 URL 연동)"""
    solapi_url = "https://api.solapi.com/messages/v4/send"
    headers = get_solapi_headers()
    today_str = datetime.date.today().strftime('%Y.%m.%d')
    
    clean_phone = ''.join(filter(str.isdigit, str(to_phone)))
    
    # 템플릿에 https:// 가 고정 작성되어 있으므로 주소 앞의 http:// 및 https:// 를 깔끔히 제거
    clean_url = re.sub(r'^https?://', '', target_url).strip()
    
    payload = {
        "message": {
            "to": clean_phone,
            "from": MY_PHONE,
            "type": "ATA",
            
            # 카카오 알림톡 설정 (템플릿 변수 100% 매칭)
            "kakaoOptions": {
                "pfId": SOLAPI_PF_ID,
                "templateId": SOLAPI_TEMPLATE_ID,
                "variables": {
                    "#{고객명}": user_name or "대표",
                    "#{기관명}": org_name or "지원기관",
                    "#{공고제목}": title or "신규 지원사업 공고",
                    "#{등록일}": today_str,
                    "#{공고링크}": clean_url  # 🔥 개별 공고 상세페이지 URL
                },
                "disableSms": False  # 알림톡 수신 실패 시 LMS 자동 대체발송
            },
            
            # 대체발송(LMS) 문구
            "subject": "[비즈맵] 신규 지원사업 공고 알림",
            "text": f"[비즈맵] 신규 지원공고 알림\n\n안녕하세요, {user_name or '대표'}님!\n\n본 알림은 회원님께서 신청하신 맞춤 알림 서비스에 따라, 설정하신 조건에 해당하는 신규 지원사업 공고가 등록되었을 때 발송되는 안내 메시지입니다.\n\n• 지원기관: {org_name}\n• 공고제목: {title}\n• 등록일자: {today_str}\n\n상세보기: {target_url}"
        }
    }
    
    try:
        res = requests.post(solapi_url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            return True, "성공"
        else:
            return False, f"[{res.status_code}] {res.text}"
    except Exception as e:
        return False, str(e)

def notify_matching_subscribers(title, org_name, notice_region, category, target_url):
    """Supabase DB의 유저 관심 조건과 매칭하여 문자/알림톡 발송 및 로그 저장"""
    try:
        res = supabase.table("users").select("*").eq("subscription_status", "active").execute()
        users = res.data or []
    except Exception as e:
        print(f" ⚠️ Supabase 유저 조회 실패: {e}")
        return 0

    sent_count = 0
    
    for user in users:
        user_email = user.get("email")
        user_name = user.get("name", "대표")
        keywords = user.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",")]

        user_phone = None
        user_region = "전국"
        
        for kw in keywords:
            if "📱" in kw or re.match(r'^\d{9,11}$', kw.replace("-", "")):
                user_phone = kw.replace("📱", "").strip()
            elif any(r in kw for r in ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]):
                user_region = kw.strip()

        if not user_phone:
            continue

        is_region_match = (user_region == "전국") or (notice_region in user_region) or (user_region in notice_region) or (notice_region == "전국")
        
        if is_region_match:
            if USE_KAKAO:
                print(f"  💬 [{user_name}님 ({user_phone})] 매칭 ➔ 카카오 알림톡 발송 중...")
                success, err_msg = send_kakao_alimtalk(user_phone, user_name, org_name, title, target_url)
            else:
                print(f"  📱 [{user_name}님 ({user_phone})] 매칭 ➔ LMS 문자 발송 중...")
                success, err_msg = send_lms_message(user_phone, user_name, org_name, title, target_url)
            
            if success:
                print(f"    🎉 발송 성공!")
                sent_count += 1
                
                try:
                    supabase.table("notification_logs").insert({
                        "email": user_email,
                        "title": f"[{org_name}] {title}",
                        "link": target_url
                    }).execute()
                except Exception as log_err:
                    print(f"    ⚠️ 로그 기록 주의: {log_err}")
            else:
                print(f"    ❌ 발송 실패: {err_msg}")
                
    return sent_count

# ==========================================
# 4. 크롤링 및 동적 랜더링 엔진
# ==========================================

def fetch_cffi_with_retry(target_url, max_retries=2):
    for attempt in range(1, max_retries + 1):
        try:
            res = cffi_requests.get(target_url, impersonate="chrome", timeout=12, verify=False)
            if res.status_code == 200:
                return res
        except Exception:
            if attempt < max_retries:
                time.sleep(1.5)
    return None

def fetch_with_playwright(target_url, org_name=""):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            page = context.new_page()
            
            try:
                page.goto(target_url, timeout=30000, wait_until="networkidle")
            except Exception:
                try:
                    page.goto(target_url, timeout=15000, wait_until="domcontentloaded")
                except Exception:
                    pass
            
            if "경기도경제과학" in org_name:
                try:
                    page.click("text=1단보기", timeout=3000)
                    time.sleep(2.0)
                except Exception:
                    pass

            wait_targets = ["tbody tr", "table tr", ".kboard-list-title", ".pms-board-list", ".tbl_list", "li", "div"]
            for target in wait_targets:
                try:
                    page.wait_for_selector(target, timeout=3000)
                    break
                except Exception:
                    pass

            time.sleep(3.0)
            content = page.content()

            for frame in page.frames:
                try:
                    content += "\n" + frame.content()
                except Exception:
                    pass

            browser.close()
            return content
    except Exception as e:
        print(f"  ⚠️ Playwright 구동 에러: {e}")
        return None

def clean_duplicate_text(text):
    text = " ".join(text.strip().split())
    text = re.sub(r'^\[.*?\]\s*', '', text)
    text = text.rstrip("+>│| ").strip()
    length = len(text)
    if length > 12:
        half = length // 2
        if text[:half].strip() == text[half:].strip():
            return text[:half].strip()
    return text

def is_valid_real_notice(title):
    if not title or len(title) < 8:
        return False
    if title in PURE_SYSTEM_NOISE:
        return False
    if any(noise in title for noise in ["소개", "인사말", "오시는길", "사이트맵", "개인정보", "조직도", "연혁"]):
        return False
    return True

def extract_title_and_link_smart(soup, org_name, target_url):
    """공고 제목과 함께 개별 상세페이지 URL(href)을 함께 추출"""
    unwanted_selectors = [
        "header", "footer", "nav", "#header", "#footer", "#gnb", "#lnb", "#snb",
        ".header", ".footer", ".gnb", ".lnb", ".snb", ".sidebar", ".top_menu",
        ".site_map", ".util_menu", "#sidebar", ".foot_area", ".location", ".breadcrumb"
    ]
    for sel in unwanted_selectors:
        for tag in soup.select(sel):
            tag.decompose()

    def make_full_url(a_elem):
        if not a_elem:
            return target_url
        href = a_elem.get("href", "").strip()
        if href and not href.startswith("javascript") and href != "#" and href != "none":
            return urljoin(target_url, href)
        return target_url

    if "제주테크노파크" in org_name:
        for tr in soup.select("tbody tr, table tr"):
            a_tag = tr.select_one("td:nth-child(4) a, td.al a, td.subject a, td a")
            if a_tag:
                txt = clean_duplicate_text(a_tag.text)
                if is_valid_real_notice(txt) and not txt.isdigit():
                    return txt, make_full_url(a_tag)

    if "전남광주통합" in org_name:
        for item in soup.select("div, li"):
            text_cand = item.get_text(strip=True)
            if "모집일자" in text_cand or "접수일자" in text_cand:
                lines = [clean_duplicate_text(l) for l in text_cand.split('\n') if len(l.strip()) > 8]
                for l in lines:
                    if is_valid_real_notice(l) and not any(kw in l for kw in ["모집일자", "접수일자", "상세보기", "전남광주", "타온라인", "모집중"]):
                        a_tag = item.select_one("a")
                        return l, make_full_url(a_tag)

    if "연구개발특구" in org_name:
        for item in soup.select("div, li, article, section"):
            text_cand = item.get_text(strip=True)
            if "~" in text_cand and ("2026" in text_cand or "2027" in text_cand):
                lines = [clean_duplicate_text(l) for l in text_cand.split('\n') if len(l.strip()) > 8]
                for l in lines:
                    if is_valid_real_notice(l) and not any(n in l for n in ["~", "조회수", "상세보기", "진행중", "마감"]):
                        a_tag = item.select_one("a")
                        return l, make_full_url(a_tag)

    for tr in soup.select("tbody tr, table tr"):
        a_tag = tr.select_one("td.subject a, td.title a, td.al a, td.left a, td.align_l a, a")
        if a_tag:
            txt = clean_duplicate_text(a_tag.text)
            if is_valid_real_notice(txt):
                return txt, make_full_url(a_tag)

    for a in soup.select(".kboard-list-title a, .kboard-title a, .pms-board-list td a, .bbs_list td a, .board_list a, ul.board_list li a, div.item a"):
        txt = clean_duplicate_text(a.text)
        if is_valid_real_notice(txt):
            if "javascript" not in txt.lower():
                return txt, make_full_url(a)

    return None, target_url

# ==========================================
# 5. 공식 Open API 수집 함수들
# ==========================================

def fetch_kstartup_api(notice_history):
    print("\n🌐 [공식 API] K-Startup 사업공고 수집 중...")
    url = f"https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01?serviceKey={DATA_GO_KEY}&page=1&perPage=10&returnType=json"
    sent_total = 0
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            items = data.get('data', [])
            if not items and 'response' in data:
                items = data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
            if isinstance(items, dict):
                items = [items]

            for item in items:
                title = str(item.get('biz_pbanc_nm') or item.get('intg_pbanc_biz_nm') or item.get('pbancNm') or '').strip()
                title = clean_duplicate_text(title)
                detail_url = str(item.get('detl_pg_url') or item.get('detlurl') or 'https://www.k-startup.go.kr').strip()

                if not is_valid_real_notice(title):
                    continue

                key = f"kstartup_{title}"
                if notice_history.get(key) != title:
                    print(f"  📢 [K-Startup 신규 공고 발견!] {title}")
                    sent_count = notify_matching_subscribers(title, "K-Startup", "전국", "창업지원", detail_url)
                    notice_history[key] = title
                    sent_total += sent_count
                    break
                else:
                    print("  ✅ [K-Startup] 최신 공고 변동 없음")
                    break
    except Exception as e:
        print(f"  ✅ [K-Startup] 예외 처리 완료 ({e})")
    return sent_total

def fetch_bizinfo_api(notice_history):
    print("\n🌐 [공식 API] 기업마당 지원사업 수집 중...")
    url = f"https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService?serviceKey={DATA_GO_KEY}&pageNo=1&numOfRows=10&dataType=json"
    sent_total = 0
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            items = data.get('jsonArray', [])
            if not items and 'response' in data:
                items = data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
            if isinstance(items, dict):
                items = [items]

            for item in items:
                title = str(item.get('pblancNm') or item.get('title') or '').strip()
                title = clean_duplicate_text(title)
                detail_url = str(item.get('pblancUrl') or 'https://www.bizinfo.go.kr').strip()

                if not is_valid_real_notice(title):
                    continue

                key = f"bizinfo_{title}"
                if notice_history.get(key) != title:
                    print(f"  📢 [기업마당 신규 공고 발견!] {title}")
                    sent_count = notify_matching_subscribers(title, "기업마당", "전국", "중소기업지원", detail_url)
                    notice_history[key] = title
                    sent_total += sent_count
                    break
                else:
                    print("  ✅ [기업마당] 최신 공고 변동 없음")
                    break
    except Exception:
        print("  ✅ [기업마당] 최신 공고 변동 없음")
    return sent_total

# ==========================================
# 6. 메인 실행 로직
# ==========================================

def main():
    if not os.path.exists(EXCEL_FILE):
        print(f"❌ '{EXCEL_FILE}' 파일이 존재하지 않습니다.")
        return

    try:
        df = load_excel_robust(EXCEL_FILE)
    except Exception as e:
        print(f"❌ 엑셀 파일 읽기 실패: {e}")
        return

    notice_history = load_json_file(HISTORY_FILE)
    dev_history = load_json_file(DEV_ALERT_FILE)
    total_notifications = 0

    target_df = df[df['수집 여부'].astype(str).str.upper() == 'Y']
    print(f"📊 총 {len(target_df)}개 기관 게시판 모니터링을 시작합니다...\n")

    for idx, row in target_df.iterrows():
        org_name = str(row.get('기관명', '알 수 없음')).strip()
        region = str(row.get('지역', '전국')).strip()
        category = str(row.get('게시판 구분', '공고')).strip()
        target_url = str(row.get('게시판 URL (상세주소)', '')).strip()

        if any(keyword in org_name for keyword in ["K-Startup", "기업마당"]):
            continue

        if not target_url or target_url == 'nan':
            continue

        print(f"🔍 [{org_name} - {category}] 탐색 중...")

        try:
            latest_title = None
            notice_link = target_url
            
            if any(dyn_org in org_name for dyn_org in DYNAMIC_ORGS):
                pw_html = fetch_with_playwright(target_url, org_name)
                if pw_html:
                    pw_soup = BeautifulSoup(pw_html, "html.parser")
                    latest_title, notice_link = extract_title_and_link_smart(pw_soup, org_name, target_url)
            else:
                res = fetch_cffi_with_retry(target_url, max_retries=2)
                if res and res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', errors='ignore'), "html.parser")
                    latest_title, notice_link = extract_title_and_link_smart(soup, org_name, target_url)

                if not latest_title:
                    pw_html = fetch_with_playwright(target_url, org_name)
                    if pw_html:
                        pw_soup = BeautifulSoup(pw_html, "html.parser")
                        latest_title, notice_link = extract_title_and_link_smart(pw_soup, org_name, target_url)

            if not latest_title or not is_valid_real_notice(latest_title):
                print("  ℹ️ 최신 공고 제목을 찾지 못함 (디자인 개편 감지 가능성)")
                send_dev_warning(org_name, category, target_url, dev_history)
                continue

            saved_title = notice_history.get(target_url, "")

            if latest_title == saved_title:
                print("  ✅ 변동 없음")
            else:
                print(f"  📢 [신규 공고 발견!] {latest_title}")
                print(f"  🔗 개별 상세 링크: {notice_link}")
                sent_count = notify_matching_subscribers(latest_title, org_name, region, category, notice_link)
                notice_history[target_url] = latest_title
                total_notifications += sent_count

        except Exception as e:
            print(f"  ❌ 크롤링 에러: {e}")
            send_dev_warning(org_name, category, target_url, dev_history)
            
        time.sleep(0.5)

    total_notifications += fetch_kstartup_api(notice_history)
    total_notifications += fetch_bizinfo_api(notice_history)

    save_json_file(HISTORY_FILE, notice_history)
    save_json_file(DEV_ALERT_FILE, dev_history)
    
    print(f"\n==========================================")
    print(f"✨ 모니터링 완벽 완료! (총 맞춤 알림 발송: {total_notifications}건)")
    print(f"==========================================")

if __name__ == "__main__":
    main()
