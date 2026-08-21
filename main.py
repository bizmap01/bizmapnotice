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
import urllib3
from urllib.parse import urljoin
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

# SSL 인증서 경고 메시지 출력 억제
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# 1. 사용자 설정 및 API Key 정보
# ==========================================
SOLAPI_KEY = "NCSOCR94THGOMHSW"
SOLAPI_SECRET = "6YH0DTGRHVDXT4HU3RS6T0TDRINDFXH4"

# 💡 카카오 알림톡 가동
USE_KAKAO = True

# 현재 승인 완료된 통합 알림 템플릿 정보
SOLAPI_PF_ID = "KA01PF260805090058574q8wFwsR3MUx"
SOLAPI_TEMPLATE_ID = "KA01TP260812073720778KfjGkb5vE9a"

MY_PHONE = "01084687138"
DATA_GO_KEY = "5df6886cdde7cb88e1c3e7e0e7c555002747947bf772546c112b028a77a8b81b"

# Supabase 연동 정보
SUPABASE_URL = "https://hcyvfgeaquydsvtrcnrv.supabase.co"
SUPABASE_KEY = "sb_publishable_P19tdkj74ibIy7Xdle2i4w_M1B1mhV_"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

EXCEL_FILE = "crawling_targets_template.xlsx"
DEV_ALERT_FILE = "dev_alert_history.json"

DYNAMIC_ORGS = [
    "경북테크노파크", "대전일자리경제진흥원",
    "소상공인24", "대구테크노파크", "경남테크노파크", "충북테크노파크",
    "전남테크노파크", "세종테크노파크", "전북특별자치도 경제통상진흥원",
    "서울경제진흥원", "전북테크노파크", "지식재산처", "지역지식재산센터", "발명진흥회"
]

# 시스템 노이즈, 선정결과, 직원 채용 공고 차단 필터
PURE_SYSTEM_NOISE = {
    "로그인", "회원가입", "마이페이지", "사이트맵", "개인정보처리방침", "이용약관", "자세히보기",
    "바로가기", "홈으로", "저작권", "이메일", "익명신고", "인권침해", "정보공개", "부서별", "FAQ",
    "자주묻는질문", "이전", "다음", "목록", "검색", "다운로드", "전체", "TOP", "안내책자다운로드",
    "수출판로지원", "기업지원", "자금지원", "일자리지원", "기타지원", "모집중", "타온라인", "마감", "상세보기",
    "진행중", "준비중", "종료", "접수중", "본문으로바로가기", "본문바로가기", "카카오톡알림신청", "알림신청",
    "유관기관지원정보", "기업마당지원사업", "분야별지원사업", "지역별지원사업", "일정별지원사업",
    "지원사업안내", "전체목록", "주요사업", "카카오톡알림",
    # 선정 결과 및 합격자 발표
    "선정결과", "선정안내", "최종선정", "선정기업", "선정자", "합격자", "심사결과", "결과공고", "결과발표", "합격자발표",
    # 기관 자체 인력/직원 채용 공고 제외
    "채용공고", "직원채용", "임시직", "기간제", "공무직", "인턴채용", "단기근로"
}

# ==========================================
# 2. 유틸리티 및 Supabase DB 이력 조회 함수
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

def load_history_from_supabase():
    """Supabase DB에서 과거 발송된 공고 목록(제목/링크)을 세트로 로드"""
    sent_set = set()
    try:
        res = supabase.table("notification_logs").select("title, link").execute()
        if res.data:
            for item in res.data:
                raw_title = item.get("title", "")
                clean_title = re.sub(r'^\[.*?\]\s*', '', raw_title).strip()
                if clean_title:
                    sent_set.add(clean_title)
                link = item.get("link", "").strip()
                if link:
                    sent_set.add(link)
        print(f"📦 Supabase DB에서 과거 발송 이력 {len(res.data)}건을 성공적으로 불러왔습니다.")
    except Exception as e:
        print(f"⚠️ Supabase 과거 이력 조회 실패: {e}")
    return sent_set

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
# 3. 💬 통합 알림톡 발송 엔진 (현재 승인 템플릿용)
# ==========================================

def is_region_matching(user_region, notice_region, title):
    """구/군 단위까지 확장된 타 지역 오발송 정밀 차단 함수"""
    u_reg = user_region.replace("광역시", "").replace("특별자치시", "").replace("특별자치도", "").replace("도", "").replace("시", "").strip()
    
    if u_reg in ["전국", ""]:
        return True
        
    if u_reg in notice_region or u_reg in title:
        return True
        
    if notice_region == "전국":
        other_regions = [
            "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
            "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
            "수성", "달성", "해운대", "기장", "유성", "대덕", "강남", "서초", "송파",
            "판교", "분당", "용인", "화성", "고양", "수원", "성남", "안양", "부천",
            "금산", "단양", "인제", "원주", "춘천", "강릉", "청주", "충주", "천안",
            "아산", "전주", "익산", "목포", "여수", "순천", "포항", "구미", "창원", "김해"
        ]
        for reg in other_regions:
            if reg in title and u_reg not in title and u_reg not in reg and reg not in u_reg:
                return False
        return True
        
    return False

def send_integrated_kakao_alimtalk(to_phone, user_name, matched_notices, user_region="전국", user_category="소상공인"):
    """🔥 현재 승인 템플릿 규격에 맞춘 안전 발송 및 지역/업종 변수 완벽 매핑"""
    solapi_url = "https://api.solapi.com/messages/v4/send"
    headers = get_solapi_headers()
    today_str = datetime.date.today().strftime('%Y.%m.%d')
    clean_phone = ''.join(filter(str.isdigit, str(to_phone)))
    total_count = len(matched_notices)

    def get_notice_info(idx):
        if idx < total_count:
            item = matched_notices[idx]
            t = f"[{item['org_name']}] {item['title']}"
            t_short = (t[:32] + '..') if len(t) > 34 else t
            d = item.get('deadline') or '상세링크 참조'
            return t_short, d
        return "(추가 공고 없음)", "-"

    title1, date1 = get_notice_info(0)
    title2, date2 = get_notice_info(1)
    title3, date3 = get_notice_info(2)

    more_text = f"\n외 {total_count - 3}건의 맞춤 공고가 더 등록되었습니다." if total_count > 3 else ""

    variables = {
        "#{고객명}": user_name or "대표",
        "#{today_date}": today_str,
        "#{count}": str(total_count),
        "#{title1}": title1,
        "#{date1}": date1,
        "#{title2}": title2,
        "#{date2}": date2,
        "#{title3}": title3,
        "#{date3}": date3,
        "#{more_text}": more_text,
        "#{지역}": user_region or "전국",
        "#{업종}": user_category or "소상공인",
        "#{분야}": user_category or "소상공인",
        "#{region}": user_region or "전국",
        "#{category}": user_category or "소상공인"
    }

    alimtalk_text = (
        f"[비즈맵] 맞춤 지원사업 공고 안내\n\n"
        f"안녕하세요, {user_name or '대표'}님!\n\n"
        f"{today_str}\n"
        f"{user_name or '대표'}님 사업장에 딱 맞는 신규 지원사업 공고가 총 {total_count}건 등록되었습니다.\n\n"
        f"📌 오늘의 주요 맞춤 공고\n"
        f"1. {title1} (~{date1})\n"
        f"2. {title2} (~{date2})\n"
        f"3. {title3} (~{date3})\n"
        f"{more_text}\n\n"
        f"아래 버튼을 누르시면 오늘 추천된 모든 지원사업의 상세 내용과 신청 원문 링크를 한눈에 확인하실 수 있습니다.\n\n"
        f"※ 본 메시지는 대표님께서 비즈맵 서비스 가입 시 직접 신청 및 동의하신 지원사업 맞춤 알림 조건({user_region} / {user_category})에 따라 신규 공고 발생 시 발송되는 안내 메시지입니다.\n\n"
        f"※ 수신 조건 변경 및 일시정지는 [마이페이지]에서 언제든지 가능합니다."
    )

    payload = {
        "message": {
            "to": clean_phone,
            "from": MY_PHONE,
            "type": "ATA",
            "kakaoOptions": {
                "pfId": SOLAPI_PF_ID,
                "templateId": SOLAPI_TEMPLATE_ID,
                "variables": variables,
                "disableSms": False
            },
            "subject": "[비즈맵] 오늘의 맞춤 지원사업 통합 알림",
            "text": alimtalk_text
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

# ==========================================
# 4. 크롤링 및 파싱 엔진
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
    """🔥 지식재산처 및 동적 사이트 네비게이션 충돌 방지 강화 버전"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            try:
                page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            except Exception:
                try:
                    page.goto(target_url, timeout=30000, wait_until="commit")
                except Exception:
                    pass
            
            if "경기도경제과학" in org_name:
                try:
                    page.click("text=1단보기", timeout=3000)
                    time.sleep(1.5)
                except Exception:
                    pass

            # 잠재적 리다이렉트 대기
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

            wait_targets = [
                "tbody tr", "table tr", ".kboard-list-title", ".pms-board-list",
                ".tbl_list", ".sub_biz_list", ".bo_tit", ".prj_list_box",
                ".company_support_list", ".business_list", "li", "div"
            ]
            for target in wait_targets:
                try:
                    page.wait_for_selector(target, timeout=1500)
                    break
                except Exception:
                    pass

            # 안전 재시도 루프 (네비게이션 중 content 호출 에러 방지)
            content = ""
            for _ in range(4):
                try:
                    time.sleep(1.2)
                    content = page.content()
                    if content and len(content) > 300:
                        break
                except Exception:
                    time.sleep(1.2)

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
        
    clean_t = title.replace(" ", "")
    for noise in PURE_SYSTEM_NOISE:
        if noise.replace(" ", "") in clean_t:
            return False
            
    bad_keywords = [
        "바로가기", "알림신청", "유관기관", "기업마당지원", "분야별지원", "지역별지원", "일정별지원",
        "로그인", "회원가입", "마이페이지", "사이트맵", "개인정보", "조직도", "연혁",
        "선정결과", "선정안내", "최종선정", "합격자발표", "심사결과", "결과발표", "결과공고", "선정기업",
        "채용공고", "직원채용", "임시직", "기간제", "공무직", "인턴채용", "단기근로"
    ]
    if any(bad in clean_t for bad in bad_keywords):
        return False
        
    return True

def make_full_url(a_elem, target_url):
    if not a_elem:
        return target_url
    
    href = a_elem.get("href", "").strip()
    onclick = a_elem.get("onclick", "").strip()
    attr_str = href + " " + onclick

    if any(invalid in href.lower() for invalid in ["kakaoalarm", "login", "mypage", "javascript:void", "#"]):
        if not onclick or "javascript:void" in onclick:
            return target_url

    if href and not href.startswith("javascript") and href not in ["#", "#none", "#LINK", "none"]:
        full = urljoin(target_url, href)
        if "NR_list.do" in full:
            full = full.replace("NR_list.do", "NR_view.do")
        elif "selectPageList.do" in full:
            full = full.replace("selectPageList.do", "selectPageDetail.do")
        elif "boardList.do" in full:
            full = full.replace("boardList.do", "boardDetail.do")
        elif "bepa.kr" in full and "idx=" in full and "view=" not in full:
            full += "&view=view"
        return full

    numbers = re.findall(r"['\"](\d+)['\"]", attr_str) or re.findall(r"\b(\d{4,10})\b", attr_str)
    
    if numbers:
        num_id = numbers[0]

        if "giba.or.kr" in target_url:
            bbs_cd_match = re.search(r"bbsCd=(\d+)", target_url)
            bbs_cd = bbs_cd_match.group(1) if bbs_cd_match else "11"
            return f"https://giba.or.kr/fe/bizinfo/bizannounce/NR_view.do?bbsCd={bbs_cd}&bizAnnoSeq={num_id}"

        if "gtp.or.kr" in target_url:
            bbs_id_match = re.search(r"bbsId=([^&]+)", target_url)
            bbs_id = bbs_id_match.group(1) if bbs_id_match else "BBSMSTR_000000000001"
            return f"https://www.gtp.or.kr/gtp/selectPageDetail.do?bbsId={bbs_id}&nttNo={num_id}"

        if "itp.or.kr" in target_url:
            if "board/list.jsp" in target_url:
                base_url = target_url.replace("board/list.jsp", "board/view.jsp")
                return f"{base_url}&data_sid={num_id}"
            elif "list.do" in target_url:
                base_url = target_url.replace("list.do", "view.do")
                return f"{base_url}&idx={num_id}"
            else:
                base_url = target_url.replace("list", "view")
                delim = "&" if "?" in base_url else "?"
                return f"{base_url}{delim}data_sid={num_id}"

        if "gbtp.or.kr" in target_url or "board.do" in target_url:
            bbs_id_match = re.search(r"bbsId=([^&]+)", target_url)
            bbs_id = bbs_id_match.group(1) if bbs_id_match else "BBSMSTR_000000000021"
            return f"https://www.gbtp.or.kr/user/boardDetail.do?bbsId={bbs_id}&nttNo={num_id}"

        if "bepa.kr" in target_url:
            base_url = target_url.split('?')[0]
            no_match = re.search(r"no=(\d+)", target_url)
            no_param = f"no={no_match.group(1)}&" if no_match else ""
            items_match = re.search(r"items=([^&]+)", target_url)
            items_param = f"&items={items_match.group(1)}" if items_match else ""
            return f"{base_url}?{no_param}idx={num_id}&view=view{items_param}"

        base_url = target_url
        base_url = base_url.replace("selectPageList.do", "selectPageDetail.do")
        base_url = base_url.replace("selectList.do", "selectDetail.do")
        base_url = base_url.replace("boardList.do", "boardDetail.do")
        base_url = base_url.replace("NR_list.do", "NR_view.do")
        base_url = base_url.replace("/list.jsp", "/view.jsp")

        if "seq=" in base_url:
            return re.sub(r"seq=[^&]+", f"seq={num_id}", base_url)
        elif "nttNo=" in base_url:
            return re.sub(r"nttNo=[^&]+", f"nttNo={num_id}", base_url)
        elif "idx=" in base_url:
            return re.sub(r"idx=[^&]+", f"idx={num_id}", base_url)
            
        delim = "&" if "?" in base_url else "?"
        return f"{base_url}{delim}nttNo={num_id}"

    return target_url

def extract_title_and_link_smart(soup, org_name, target_url):
    unwanted_selectors = [
        "header", "footer", "nav", "#header", "#footer", "#gnb", "#lnb", "#snb",
        ".header", ".footer", ".gnb", ".lnb", ".snb", ".sidebar", ".top_menu",
        ".site_map", ".util_menu", "#sidebar", ".foot_area", ".location", ".breadcrumb",
        "#skipNav", "#skip_nav", ".skip_nav", ".skipNav", ".skip", "#skip",
        ".quick_menu", ".quick", ".floating", "#quickMenu", ".sba_quick", ".floating_banner",
        "[href*='javascript:void']", "[href*='#cont']", "[href*='#skip']", "[href*='KakaoAlarm']", "[href*='login']"
    ]
    for sel in unwanted_selectors:
        for tag in soup.select(sel):
            tag.decompose()

    if "경상북도경제진흥원" in org_name:
        for item in soup.select(".gallery-title, .gallery_title, .gallery-item, .sub_biz_list li, .card_box, .biz_list li, article, .item"):
            a_tag = item.find("a") or item.find_parent("a")
            raw = item.get_text(" ", strip=True)
            raw = re.sub(r'^([가-힣]{2,10}(지원|육성|사업))?\s*(진행중|모집중|접수중|마감|종료|준비중)?\s*', '', raw)
            txt = clean_duplicate_text(raw)
            if a_tag and is_valid_real_notice(txt):
                return txt, make_full_url(a_tag, target_url)

    if "강원특별자치도" in org_name:
        for node in soup.select(".bo_tit a, td.td_subject a, .list_subject a, .subject a, .item_subject a"):
            txt = re.sub(r'^\[.*?\]\s*', '', clean_duplicate_text(node.get_text())).strip()
            if is_valid_real_notice(txt):
                return txt, make_full_url(node, target_url)

    if "경북테크노파크" in org_name:
        for a in soup.select("a[href*='boardDetail.do'], a[onclick*='fn_egov_inqire_notice'], .bbs_list td.subject a, .board_list td a, table tbody tr td a"):
            txt = clean_duplicate_text(a.get_text(" ", strip=True))
            if is_valid_real_notice(txt) and not txt.isdigit():
                return txt, make_full_url(a, target_url)

    if "대전일자리" in org_name:
        for a in soup.select("a[href*='TSK_PBNC_ID'], a[href*='form.tab'], a[href*='view'], .b-cont a, .board_list li a, .bbs_list tbody tr a, .list_item a"):
            txt = re.sub(r'^\[.*?\]\s*', '', clean_duplicate_text(a.get_text(" ", strip=True))).strip()
            if is_valid_real_notice(txt) and len(txt) >= 10 and re.search(r'(공고|모집|지원사업|선정|참여)', txt):
                return txt, make_full_url(a, target_url)

    if "전북테크노파크" in org_name:
        for tr in soup.select("tbody tr, table tr"):
            a_tag = tr.select_one("td.subject a, td.title a, td.left a, td:nth-child(2) a, a")
            if a_tag:
                txt = clean_duplicate_text(a_tag.text)
                txt = re.sub(r'^\[.*?\]\s*', '', txt).strip()
                if is_valid_real_notice(txt):
                    return txt, make_full_url(a_tag, target_url)

    if "경기도경제과학" in org_name or "경기기업비서" in org_name:
        for card in soup.select(".card-body, .card, .prj_list_box, .card_item, ul.list li, .list_box li"):
            a_tag = card.select_one(".tit a, a.title, dt a, h4 a, a.prj_name, a") or card.find_parent("a")
            if a_tag:
                txt = clean_duplicate_text(a_tag.get_text(" ", strip=True))
                if is_valid_real_notice(txt):
                    return txt, make_full_url(a_tag, target_url)

    if "서울경제진흥원" in org_name:
        for card in soup.select(".company_support_list li, .card_box, div.card_inner"):
            a_tag = card.select_one("a.title, dt a, h4 a, .tit a, a")
            if a_tag:
                txt = clean_duplicate_text(a_tag.text)
                if is_valid_real_notice(txt):
                    return txt, make_full_url(a_tag, target_url)

    if "연구개발특구" in org_name:
        for item in soup.select(".board_list li, .bbs_list li, ul.lst li, .list li, ul li, li, table tbody tr"):
            links = item.find_all("a")
            if not links:
                continue
            full = item.get_text(" ", strip=True)
            if "이전 사업공고" in full or "다음 사업공고" in full:
                continue
            cleaned = re.sub(r'(자세히보기|진행중|접수중|마감|준비중|종료|URL\s*공유|프린트|페이스북|트위터|공유하기)', ' ', full)
            cleaned = re.sub(r'20\d{2}[-.]\d{1,2}[-.]\d{1,2}\s*~?\s*(20\d{2}[-.]\d{1,2}[-.]\d{1,2})?', ' ', cleaned)
            cleaned = re.sub(r'^\s*(기술이전[·]?사업화|해외진출|투[·]?융자[ ·]?연계|교육[·]?컨설팅|사업화|정책자금|R&D|기타|투자연계)\s*', '', cleaned)
            txt = clean_duplicate_text(cleaned.strip())
            if is_valid_real_notice(txt) and len(txt) >= 10 and re.search(r'(공고|모집|사업|지원|참여)', txt):
                detail_a = None
                for a in links:
                    if "자세히보기" in a.get_text() or a.get("href", "") not in ("", "#", "#none"):
                        detail_a = a
                        break
                return txt, make_full_url(detail_a or links[0], target_url)

    for tr in soup.select("tbody tr, table tr"):
        a_tag = tr.select_one("td.subject a, td.title a, td.al a, td.left a, td.align_l a, a")
        if a_tag:
            txt = clean_duplicate_text(a_tag.text)
            if is_valid_real_notice(txt):
                return txt, make_full_url(a_tag, target_url)

    for a in soup.select(".kboard-list-title a, .kboard-title a, .pms-board-list td a, .bbs_list td a, .board_list a, ul.board_list li a"):
        txt = clean_duplicate_text(a.text)
        if is_valid_real_notice(txt):
            if "javascript" not in txt.lower():
                return txt, make_full_url(a, target_url)

    for node in soup.select("li a, article a, .item a, .card a, [class*='card'] a, [class*='item'] a"):
        raw = node.get_text(" ", strip=True)
        txt = re.sub(r'^\[.*?\]\s*', '', clean_duplicate_text(raw)).strip()
        if is_valid_real_notice(txt) and re.search(r'(공고|모집|지원사업|참여기업|선정|신청|접수|사업|모집공고)', txt):
            return txt, make_full_url(node, target_url)

    return None, target_url

# ==========================================
# 5. 공고 수집 및 유저 바구니 축적 함수
# ==========================================

def add_notice_to_user_buckets(title, org_name, notice_region, category, target_url, user_buckets, sent_history):
    matched_count = 0
    
    for email, u_data in user_buckets.items():
        user = u_data['user']
        keywords = user.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",")]

        user_region = "전국"
        for kw in keywords:
            if any(r in kw for r in ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]):
                user_region = kw.strip()

        if is_region_matching(user_region, notice_region, title):
            u_data['notices'].append({
                "title": title,
                "org_name": org_name,
                "link": target_url,
                "region": notice_region,
                "deadline": "상세링크 참조"
            })
            matched_count += 1
            
            try:
                supabase.table("notification_logs").insert({
                    "email": email,
                    "title": f"[{org_name}] {title}",
                    "link": target_url
                }).execute()
            except Exception:
                pass

    sent_history.add(title)
    sent_history.add(target_url)
    return matched_count

def collect_kstartup_api(user_buckets, sent_history):
    """🔥 K-Startup: 1차 공식 API -> 실패 시 2차 웹(Playwright) 크롤링"""
    print("\n🌐 [공식 API] K-Startup 사업공고 수집 중...")
    url = f"https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01?serviceKey={DATA_GO_KEY}&page=1&perPage=20&returnType=json"
    api_success = False

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            items = data.get('data', [])
            if not items and 'response' in data:
                items = data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
            if isinstance(items, dict):
                items = [items]

            new_count = 0
            for item in items:
                title = str(item.get('biz_pbanc_nm') or item.get('intg_pbanc_biz_nm') or item.get('pbancNm') or '').strip()
                title = clean_duplicate_text(title)
                detail_url = str(item.get('detl_pg_url') or item.get('detlurl') or 'https://www.k-startup.go.kr').strip()

                if not is_valid_real_notice(title):
                    continue
                if title in sent_history or detail_url in sent_history:
                    continue

                print(f"  📢 [K-Startup 신규 공고 발견!] {title}")
                add_notice_to_user_buckets(title, "K-Startup", "전국", "창업지원", detail_url, user_buckets, sent_history)
                new_count += 1

            if new_count == 0:
                print("  ✅ [K-Startup] 최신 공고 변동 없음 (이미 발송 완료)")
            api_success = True
    except Exception:
        print("  ⚠️ [K-Startup API 지연] -> 웹 직접 크롤링으로 전환합니다.")

    if not api_success:
        try:
            pw_html = fetch_with_playwright("https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do", "K-Startup")
            if pw_html:
                soup = BeautifulSoup(pw_html, "html.parser")
                new_count = 0
                for a_tag in soup.select("ul.notice_list li a, .pms-board-list td a, tbody tr td a, .tit a"):
                    t = clean_duplicate_text(a_tag.get_text(" ", strip=True))
                    href = a_tag.get("href", "")
                    link = urljoin("https://www.k-startup.go.kr", href) if href else "https://www.k-startup.go.kr"

                    if not is_valid_real_notice(t):
                        continue
                    if t in sent_history or link in sent_history:
                        continue

                    print(f"  📢 [K-Startup(웹백업) 신규 공고 발견!] {t}")
                    add_notice_to_user_buckets(t, "K-Startup", "전국", "창업지원", link, user_buckets, sent_history)
                    new_count += 1
                if new_count == 0:
                    print("  ✅ [K-Startup(웹백업)] 최신 공고 변동 없음")
        except Exception as e:
            print(f"  ❌ K-Startup 웹 백업 실패: {e}")

def collect_bizinfo_api(user_buckets, sent_history):
    """🔥 기업마당: 공식 API -> 지정 웹 주소(selectSIIA200View.do) 크롤링 3중 완벽 백업"""
    print("\n🌐 [공식 API] 기업마당 지원사업 수집 중...")
    url = f"https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService?serviceKey={DATA_GO_KEY}&pageNo=1&numOfRows=20&dataType=json"
    api_success = False

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            items = data.get('jsonArray', [])
            if not items and 'response' in data:
                items = data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
            if isinstance(items, dict):
                items = [items]

            new_count = 0
            for item in items:
                title = str(item.get('pblancNm') or item.get('title') or '').strip()
                title = clean_duplicate_text(title)
                detail_url = str(item.get('pblancUrl') or 'https://www.bizinfo.go.kr').strip()

                if not is_valid_real_notice(title):
                    continue
                if title in sent_history or detail_url in sent_history:
                    continue

                print(f"  📢 [기업마당 신규 공고 발견!] {title}")
                add_notice_to_user_buckets(title, "기업마당", "전국", "중소기업지원", detail_url, user_buckets, sent_history)
                new_count += 1

            if new_count == 0:
                print("  ✅ [기업마당] 최신 공고 변동 없음 (이미 발송 완료)")
            api_success = True
    except Exception:
        print(f"  ⚠️ [기업마당 API 지연] -> 지정 웹페이지 크롤링으로 전환합니다.")

    # 💡 2차/3차 백업: 대표님이 지정하신 실시간 웹 공고 주소 파싱
    if not api_success:
        try:
            target_web_url = "https://www.bizinfo.go.kr/sii/siia/selectSIIA200View.do?schPblancDiv=01"
            html = None
            
            res = fetch_cffi_with_retry(target_web_url, max_retries=2)
            if res and res.status_code == 200:
                html = res.content.decode('utf-8', errors='ignore')
            
            if not html:
                print("  ↪️ [기업마당] Playwright 브라우저 직접 렌더링으로 3차 백업 가동")
                html = fetch_with_playwright(target_web_url, "기업마당")

            if html:
                soup = BeautifulSoup(html, "html.parser")
                new_count = 0
                
                # 기업마당 목록 테이블 순회
                for tr in soup.select("table tbody tr, tbody tr, .table_style01 tr, .table_list tr"):
                    a_tag = tr.select_one("td.subject a, td.txt_l a, td:nth-child(3) a, a.tit, a")
                    if not a_tag:
                        continue
                    
                    raw_text = clean_duplicate_text(a_tag.get_text(" ", strip=True))
                    if not is_valid_real_notice(raw_text):
                        continue

                    # 고유 공고 ID(pblancId) 정밀 추출 및 상세 페이지 링크 생성
                    href = a_tag.get("href", "")
                    onclick = a_tag.get("onclick", "")
                    attr_str = href + " " + onclick
                    
                    pblanc_match = re.search(r"PBLN_[a-zA-Z0-9_]+", attr_str) or re.search(r"pblancId=([^&'\"]+)", attr_str)
                    
                    if pblanc_match:
                        pblanc_id = pblanc_match.group(0) if "PBLN_" in pblanc_match.group(0) else pblanc_match.group(1)
                        link = f"https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId={pblanc_id}"
                    elif href and not href.startswith("javascript") and href not in ["#", "#none"]:
                        link = urljoin("https://www.bizinfo.go.kr", href)
                    else:
                        link = target_web_url

                    if raw_text in sent_history or link in sent_history:
                        continue

                    print(f"  📢 [기업마당(웹백업) 신규 공고 발견!] {raw_text}")
                    add_notice_to_user_buckets(raw_text, "기업마당", "전국", "중소기업지원", link, user_buckets, sent_history)
                    new_count += 1

                if new_count == 0:
                    print("  ✅ [기업마당(웹백업)] 최신 공고 변동 없음 (이미 발송 완료)")
            else:
                print("  ❌ [기업마당] 웹 백업 응답 없음")
        except Exception as web_err:
            print(f"  ❌ 기업마당 백업 크롤링 에러: {web_err}")

def collect_jejutp_api(user_buckets, sent_history):
    print("\n🌐 [API] 제주테크노파크 사업공고 수집 중...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.jejutp.or.kr/board/business",
    }
    api_candidates = [
        "https://www.jejutp.or.kr/board/business/list?keyword=&page=0&size=30&cate=",
        "https://www.jejutp.or.kr/board/business/list?keyword=&pageNumber=0&size=30&cate=",
        "https://www.jejutp.or.kr/api/board/business/list?keyword=&page=0&size=30&cate=",
    ]
    title, detail = None, None

    try:
        data = None
        for api in api_candidates:
            try:
                res = requests.get(api, headers=headers, timeout=10, verify=False)
                if res.status_code == 200 and res.text.strip().startswith(("{", "[")):
                    data = res.json()
                    break
            except Exception:
                continue

        if data is not None:
            items = None
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                for k in ("content", "list", "data", "items", "rows", "result", "resultList"):
                    v = data.get(k)
                    if isinstance(v, list) and v:
                        items = v
                        break
            if items:
                first = items[0]
                anno = first.get("anno", first) if isinstance(first, dict) else {}
                cand = clean_duplicate_text(str(
                    anno.get("annoName") or anno.get("title") or anno.get("subject") or ""
                ))
                anno_id = (anno.get("annoId") or anno.get("id") or anno.get("seq")
                           or first.get("id") or first.get("annoId") or "")
                if is_valid_real_notice(cand):
                    title = cand
                    detail = (f"https://www.jejutp.or.kr/board/business/detail/{anno_id}"
                              if anno_id else "https://www.jejutp.or.kr/board/business")

        if not title:
            html = fetch_with_playwright("https://www.jejutp.or.kr/board/business", "제주테크노파크")
            if html:
                jsoup = BeautifulSoup(html, "html.parser")
                for a in jsoup.select("a[href*='/board/business/detail']"):
                    raw = clean_duplicate_text(a.get_text(" ", strip=True))
                    t = re.sub(r'^\s*\d+\s*', '', raw)
                    t = re.sub(r'^\s*D-\s*\d+\s*', '', t)
                    t = re.sub(r'^\s*(마감|D-\d+|접수중|신청가능|모집중|진행중|준비중|종료)\s*', '', t)
                    t = clean_duplicate_text(t.strip())
                    if is_valid_real_notice(t):
                        title = t
                        detail = urljoin("https://www.jejutp.or.kr", a.get("href", "").split("?")[0])
                        break

        if not title:
            return

        if title in sent_history or detail in sent_history:
            print(f"  ✅ [제주TP] 변동 없음 (이미 발송 완료)")
        else:
            print(f"  📢 [제주TP 신규 공고 발견!] {title}")
            add_notice_to_user_buckets(title, "제주테크노파크", "제주", "사업공고", detail, user_buckets, sent_history)
    except Exception as e:
        print(f"  ⚠️ [제주TP] 오류: {e}")

def collect_gjbizinfo_api(user_buckets, sent_history):
    print("\n🌐 [API] 전남광주통합 기업지원시스템 수집 중...")
    api = "https://www.gjbizinfo.or.kr/getOnlineList.do"
    payload = {
        "pageId": "www48", "movePage": "", "searchSup": "",
        "sosang": "N", "searchTp": "B", "searchQuery": "",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.gjbizinfo.or.kr/online.do?pageId=www48",
    }
    try:
        res = requests.post(api, data=payload, headers=headers, timeout=10, verify=False)
        items = res.json().get("dataArr", {}).get("getOnlineList", [])
        if not items:
            return

        newest = items[0]
        title = clean_duplicate_text(str(newest.get("ONLINE_NAME", "")).strip())
        sn = newest.get("ONLINE_SN")
        detail = f"https://www.gjbizinfo.or.kr/onlineView.do?pageId=www48&online_sn={sn}"

        if not is_valid_real_notice(title):
            return

        if title in sent_history or detail in sent_history:
            print(f"  ✅ [전남광주통합] 변동 없음 (이미 발송 완료)")
        else:
            print(f"  📢 [전남광주통합 신규 공고 발견!] {title}")
            add_notice_to_user_buckets(title, "전남광주통합 기업지원시스템", "전남", "지원사업정보", detail, user_buckets, sent_history)
    except Exception as e:
        print(f"  ⚠️ [전남광주통합] API 오류: {e}")

# ==========================================
# 6. 메인 실행 및 통합 발송 총괄
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

    sent_history = load_history_from_supabase()
    dev_history = load_json_file(DEV_ALERT_FILE)

    try:
        res = supabase.table("users").select("*").eq("subscription_status", "active").execute()
        active_users = res.data or []
    except Exception as e:
        print(f"❌ Supabase 유저 로드 실패: {e}")
        return

    user_buckets = {u['email']: {'user': u, 'notices': []} for u in active_users}
    print(f"👥 현재 활성 구독 유저: {len(user_buckets)}명 (맞춤 공고 바구니 준비 완료)")

    target_df = df[df['수집 여부'].astype(str).str.upper() == 'Y']
    print(f"\n📊 총 {len(target_df)}개 기관 게시판 모니터링을 시작합니다...\n")

    for idx, row in target_df.iterrows():
        org_name = str(row.get('기관명', '알 수 없음')).strip()
        region = str(row.get('지역', '전국')).strip()
        category = str(row.get('게시판 구분', '공고')).strip()
        target_url = str(row.get('게시판 URL (상세주소)', '')).strip()

        if any(keyword in org_name for keyword in
               ["K-Startup", "기업마당", "제주테크노파크", "전남광주통합"]):
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
                print("  ℹ️ 최신 공고 제목을 찾지 못함 (필터링 또는 변동 없음)")
                continue

            if latest_title in sent_history or notice_link in sent_history:
                print("  ✅ 변동 없음 (이미 발송 완료된 공고)")
            else:
                print(f"  📢 [신규 공고 발견!] {latest_title}")
                print(f"  🔗 개별 상세 링크: {notice_link}")
                add_notice_to_user_buckets(latest_title, org_name, region, category, notice_link, user_buckets, sent_history)

        except Exception as e:
            print(f"  ❌ 크롤링 에러: {e}")
            send_dev_warning(org_name, category, target_url, dev_history)
            
        time.sleep(0.5)

    collect_kstartup_api(user_buckets, sent_history)
    collect_bizinfo_api(user_buckets, sent_history)
    collect_jejutp_api(user_buckets, sent_history)
    collect_gjbizinfo_api(user_buckets, sent_history)

    save_json_file(DEV_ALERT_FILE, dev_history)

    # ==========================================
    # 7. 🔥 크롤링 완료 후 유저별 통합 1통 발송
    # ==========================================
    print(f"\n==========================================")
    print(f"📨 [통합 알림 발송 단계] 유저별 맞춤 다이제스트 발송을 시작합니다...")
    print(f"==========================================")

    total_sent_users = 0
    for email, u_data in user_buckets.items():
        user = u_data['user']
        notices = u_data['notices']
        user_name = user.get("name", "대표")
        
        keywords = user.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",")]

        user_phone = None
        user_region = "전국"
        user_category = "소상공인"

        for kw in keywords:
            if "📱" in kw or re.match(r'^\d{9,11}$', kw.replace("-", "")):
                user_phone = kw.replace("📱", "").strip()
            elif any(r in kw for r in ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]):
                user_region = kw.strip()
            elif any(c in kw for c in ["소상공인", "자영업", "창업", "제조", "IT", "스타트업"]):
                user_category = kw.strip()

        if not user_phone:
            continue

        if len(notices) > 0:
            print(f"\n💬 [{user_name}님 ({user_phone})] 조건:({user_region}/{user_category}) | 신규 공고 {len(notices)}건 통합 발송 중...")
            if USE_KAKAO:
                success, err_msg = send_integrated_kakao_alimtalk(user_phone, user_name, notices, user_region, user_category)
                if success:
                    print(f"  🎉 [통합 알림톡 발송 성공!]")
                    total_sent_users += 1
                else:
                    print(f"  ❌ [통합 알림톡 발송 실패]: {err_msg}")
        else:
            print(f"ℹ️ [{user_name}님] 오늘 신규 매칭 공고 없음 (불필요한 스팸 발송 차단)")

    print(f"\n==========================================")
    print(f"✨ 모든 프로세스 완료! (총 {total_sent_users}명에게 맞춤 통합 리포트 발송 완료)")
    print(f"==========================================")

if __name__ == "__main__":
    main()
