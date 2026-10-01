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
#    🔒 실제 키 값은 코드에 넣지 않는다. 전부 환경변수(GitHub Actions Secrets 등)로 주입.
# ==========================================

def _require_env(name: str) -> str:
    """필수 환경변수를 읽어온다. 없으면 즉시 명확한 에러로 실패시킨다(조용히 빈 문자열로 넘어가지 않음)."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"환경변수 '{name}'가 설정되지 않았습니다. "
            f"GitHub Actions Secrets(Settings > Secrets and variables > Actions)에 등록한 뒤 다시 실행하세요."
        )
    return val

SOLAPI_KEY = _require_env("SOLAPI_KEY")
SOLAPI_SECRET = _require_env("SOLAPI_SECRET")

# 💡 카카오 알림톡 가동 (기본 True, 환경변수로 끌 수 있음)
USE_KAKAO = os.environ.get("USE_KAKAO", "true").strip().lower() != "false"

# 🔥 승인 완료된 신규 가변형 알림 템플릿 정보
SOLAPI_PF_ID = _require_env("SOLAPI_PF_ID")
SOLAPI_TEMPLATE_ID = _require_env("SOLAPI_TEMPLATE_ID")

MY_PHONE = _require_env("MY_PHONE")
DATA_GO_KEY = _require_env("DATA_GO_KEY")

# Supabase 연동 정보
# ⚠️ 이 스크립트는 서버(=GitHub Actions)에서만 실행되고, users/notification_logs 테이블을
#    브라우저에서 쓰는 것과 달리 전체 읽기/쓰기 권한이 필요하다. RLS를 제대로 켠 뒤에는
#    publishable(anon) 키로는 아무것도 못 읽게 되므로, 여기는 반드시 Supabase의
#    "service_role" 키를 SUPABASE_SERVICE_ROLE_KEY 라는 이름의 Secret으로 등록해서 써야 한다.
SUPABASE_URL = _require_env("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or _require_env("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# SSL 인증서 검증을 끌지 여부. 기본은 켜짐(안전)이고, 특정 사이트가 인증서 문제로
# 막힐 때만 명시적으로 SSL_VERIFY=false 로 지정해서 예외적으로 끄도록 한다.
SSL_VERIFY = os.environ.get("SSL_VERIFY", "true").strip().lower() != "false"

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

def is_generic_url(link):
    """게시판 목록/공통 URL인지 판별 (중복 차단 오작동 방지)"""
    if not link:
        return True
    generic_keywords = [
        "selectSIIA200View.do", "list.do", "list.jsp", "boardList.do",
        "NR_list.do", "schPblancDiv", "pageIndex", "cpage=", "board_list",
        "bizpbanc-ongoing.do"
    ]
    return any(k in link for k in generic_keywords)

def fetch_all_notification_logs(page_size=1000, max_pages=500):
    """notification_logs 전체를 (title, link)로 페이지 단위로 모두 읽어 온다.
    정렬 기준을 고정해야 페이지 경계에서 행이 빠지거나 겹치지 않는다."""
    rows = []
    start = 0
    for _ in range(max_pages):
        page = (
            supabase.table("notification_logs")
            .select("title, link")
            .order("created_at")
            .order("email")
            .order("title")
            .order("link")
            .range(start, start + page_size - 1)
            .execute()
        )
        data = page.data or []
        rows.extend(data)
        if len(data) < page_size:
            break
        start += page_size
    return rows

def load_history_from_supabase():
    """Supabase DB에서 과거 발송된 공고 목록(제목/개별 상세링크)을 로드"""
    sent_set = set()
    try:
        # 🔧 [버그수정] 예전엔 .execute() 한 번만 호출해서 Supabase 기본 상한(1000행)에서 조용히
        # 잘렸다. 이력이 1000행을 넘기면(2026-10-01 실제 발생: 전날 965건 → 다음날 정확히 1000건)
        # 가장 최근에 발송한 행들이 비교 대상에서 빠져서, 이미 보낸 공고가 "신규"로 다시
        # 잡혀 유저에게 중복 발송됐다. 이제 1000행씩 끝까지 페이지를 넘겨가며 전부 읽는다.
        all_rows = fetch_all_notification_logs()
        res = type("Res", (), {"data": all_rows})()
        if res.data:
            for item in res.data:
                raw_title = item.get("title", "").strip()
                # 🔧 [버그수정] 예전엔 알려진 기관명만 나열해서 접두어를 벗겨냈는데,
                # 실제 크롤링 대상 기관 목록(DYNAMIC_ORGS, 엑셀)엔 이 목록에 없는 기관명이
                # 훨씬 많아서 대부분 기관은 "[기관명] 제목" 형태 그대로 남아 있었다.
                # 그 결과 main()의 순수 제목 비교(`latest_title in sent_history`)가 항상 실패해서
                # 중복 발송 방지가 사실상 안 먹히는 상태였다. 기관명을 나열하는 대신
                # 맨 앞의 "[...]" 괄호 전체를 범용으로 제거하도록 일반화한다.
                clean_title = re.sub(r'^\[[^\]]+\]\s*', '', raw_title).strip()
                if clean_title:
                    sent_set.add(clean_title)
                if raw_title:
                    sent_set.add(raw_title)
                link = item.get("link", "").strip()
                if link and not is_generic_url(link):
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
# 3. 💬 정밀 지역/업종 필터 및 알림톡 발송 엔진
# ==========================================

ALL_KOREA_REGIONS = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
    "영월", "정선", "평창", "화천", "양구", "인제", "고성", "양양", "철원", "홍천", "횡성", "원주", "춘천", "강릉", "동해", "삼척", "속초", "태백",
    "단양", "제천", "보은", "옥천", "영동", "증평", "진천", "괴산", "음성", "청주", "충주",
    "태안", "당진", "서산", "홍성", "보령", "청양", "부여", "서천", "논산", "계룡", "금산", "예산", "아산", "천안", "공주",
    "완주", "진안", "무주", "장수", "임실", "순창", "고창", "부안", "군산", "익산", "정읍", "남원", "김제", "전주",
    "영광", "함평", "장성", "담양", "곡성", "구례", "장흥", "강진", "해남", "영암", "무안", "완도", "진도", "신안", "여수", "순천", "나주", "광양", "고흥", "보성", "화순", "목포",
    "문경", "예천", "영주", "봉화", "울진", "상주", "의성", "안동", "영양", "김천", "구미", "군위", "칠곡", "성주", "고령", "영천", "포항", "경주", "청도", "영덕", "청송", "울릉",
    "거창", "함양", "산청", "합천", "창녕", "밀양", "의령", "진주", "하동", "남해", "사천", "통영", "거제", "창원", "김해", "양산",
    "수원", "성남", "안양", "부천", "광명", "평택", "안산", "고양", "과천", "구리", "남양주", "오산", "시흥", "군포", "의왕", "하남", "용인", "파주", "이천", "안성", "김포", "화성", "광주", "양주", "포천", "여주", "연천", "가평", "양평",
    "서귀포", "강남", "서초", "송파", "강동", "용산", "마포", "영등포", "종로", "중구", "성동", "광진", "동대문", "중랑", "성북", "강북", "도봉", "노원", "은평", "서대문", "양천", "구로", "금천", "동작", "관악",
    "해운대", "수영", "남구", "동래", "연제", "부산진", "사상", "사하", "강서", "금정", "기장", "영도", "수성", "달서", "달성", "유성", "대덕"
]

# 🔧 [버그수정] 지역명이 부분 문자열 비교(in)로만 검사되다 보니, 지역명과 우연히 겹치는
# 일반 한국어 단어/합성어 때문에 오탐(엉뚱한 지역 알림 발송)이나 누락(전국 공고가 조용히
# 필터링됨)이 실제로 발생한다. 예:
#   "달성"(대구 달성군) ⊂ "목표달성 지원사업"
#   "고령"(경북 고령군) ⊂ "고령친화산업 육성 지원사업" (이 저장소의 notice_history.json에 실제로 있던 제목)
#   "경기"(경기도)      ⊂ "환경기술 지원사업"
#   "대구"(대구광역시)  ⊂ "확대구축 지원사업"
# 완벽한 형태소 분석 없이는 100% 해결이 안 되지만, 실제로 부딪힌 충돌 단어들을
# 블록리스트로 등록해서 최소한 알려진 오탐/누락은 막는다. 새로운 충돌 사례를 발견하면
# 이 딕셔너리에 계속 추가하면 된다.
REGION_NAME_FALSE_POSITIVE_WORDS = {
    "달성": ["목표달성", "성과달성", "달성률", "달성도", "미션달성", "목표를달성"],
    "고령": ["고령자", "고령화", "고령친화", "고령층", "초고령"],
    "경기": ["환경기술", "환경기업", "환경기반", "환경개선", "체감경기", "경기침체", "경기회복", "경기변동"],
    "대구": ["확대구축", "확대구매", "확대구성", "확대구현", "대구분"],
    "정선": ["정선된", "정선하여", "엄정선정", "정선기준"],
    "장수": ["장수기업", "우수장수기업", "백년장수", "장수생", "장수사진"],
}

# 🔧 [필터링 수정] ALL_KOREA_REGIONS는 시/도와 그 아래 시/군/구 이름이 계층 구분 없이
# 한 줄로 섞여 있다. 그래서 "성북구", "파주", "시흥" 처럼 상위 시/도 이름(서울/경기)을
# 제목에 아예 안 쓰고 시/군/구 이름만 쓰는 게 보통인 제목들은, 밑의 제외 루프가
# "성북"/"파주"/"시흥"을 서울·경기와 무관한 별개의 지역으로 오인해서 정작 대상인
# 서울/경기 사용자에게까지 발송을 막아버렸다(2026-09-30 실제 크롤러 결과로 확인:
# 성북구·강동구·마포·구로구·파주·시흥 관련 공고가 전 사용자에게 전혀 발송되지 않았음).
# 아래는 "이름이 전국에서 유일해서 소속 시/도가 명확한" 시/군/구만 매핑한 것이다.
# "고성"(강원/경남), "중구"/"남구"(여러 시에 동일 이름 존재), "광주"(광역시/경기 광주시),
# "강서"(부산/서울 둘 다 있음) 처럼 이름만으로는 어느 지역인지 알 수 없는 것은
# 잘못 매핑하면 오히려 새로운 오탐을 만들 수 있어 일부러 매핑하지 않고 기존 방식(그대로 별개 지역
# 취급)을 유지한다.
SUB_REGION_PARENT = {
    # 강원
    "영월": "강원", "정선": "강원", "평창": "강원", "화천": "강원", "양구": "강원", "인제": "강원",
    "양양": "강원", "철원": "강원", "홍천": "강원", "횡성": "강원", "원주": "강원", "춘천": "강원",
    "강릉": "강원", "동해": "강원", "삼척": "강원", "속초": "강원", "태백": "강원",
    # 충북
    "단양": "충북", "제천": "충북", "보은": "충북", "옥천": "충북", "영동": "충북", "증평": "충북",
    "진천": "충북", "괴산": "충북", "음성": "충북", "청주": "충북", "충주": "충북",
    # 충남
    "태안": "충남", "당진": "충남", "서산": "충남", "홍성": "충남", "보령": "충남", "청양": "충남",
    "부여": "충남", "서천": "충남", "논산": "충남", "계룡": "충남", "금산": "충남", "예산": "충남",
    "아산": "충남", "천안": "충남", "공주": "충남",
    # 전북
    "완주": "전북", "진안": "전북", "무주": "전북", "장수": "전북", "임실": "전북", "순창": "전북",
    "고창": "전북", "부안": "전북", "군산": "전북", "익산": "전북", "정읍": "전북", "남원": "전북",
    "김제": "전북", "전주": "전북",
    # 전남
    "영광": "전남", "함평": "전남", "장성": "전남", "담양": "전남", "곡성": "전남", "구례": "전남",
    "장흥": "전남", "강진": "전남", "해남": "전남", "영암": "전남", "무안": "전남", "완도": "전남",
    "진도": "전남", "신안": "전남", "여수": "전남", "순천": "전남", "나주": "전남", "광양": "전남",
    "고흥": "전남", "보성": "전남", "화순": "전남", "목포": "전남",
    # 경북
    "문경": "경북", "예천": "경북", "영주": "경북", "봉화": "경북", "울진": "경북", "상주": "경북",
    "의성": "경북", "안동": "경북", "영양": "경북", "김천": "경북", "구미": "경북", "군위": "경북",
    "칠곡": "경북", "성주": "경북", "고령": "경북", "영천": "경북", "포항": "경북", "경주": "경북",
    "청도": "경북", "영덕": "경북", "청송": "경북", "울릉": "경북",
    # 경남
    "거창": "경남", "함양": "경남", "산청": "경남", "합천": "경남", "창녕": "경남", "밀양": "경남",
    "의령": "경남", "진주": "경남", "하동": "경남", "남해": "경남", "사천": "경남", "통영": "경남",
    "거제": "경남", "창원": "경남", "김해": "경남", "양산": "경남",
    # 경기
    "수원": "경기", "성남": "경기", "안양": "경기", "부천": "경기", "광명": "경기", "평택": "경기",
    "안산": "경기", "고양": "경기", "과천": "경기", "구리": "경기", "남양주": "경기", "오산": "경기",
    "시흥": "경기", "군포": "경기", "의왕": "경기", "하남": "경기", "용인": "경기", "파주": "경기",
    "이천": "경기", "안성": "경기", "김포": "경기", "화성": "경기", "양주": "경기", "포천": "경기",
    "여주": "경기", "연천": "경기", "가평": "경기", "양평": "경기",
    # 서울 (자치구 이름이 다른 시와 안 겹치는 것만)
    "강남": "서울", "서초": "서울", "송파": "서울", "강동": "서울", "용산": "서울", "마포": "서울",
    "영등포": "서울", "종로": "서울", "성동": "서울", "광진": "서울", "동대문": "서울", "중랑": "서울",
    "성북": "서울", "강북": "서울", "도봉": "서울", "노원": "서울", "은평": "서울", "서대문": "서울",
    "양천": "서울", "구로": "서울", "금천": "서울", "동작": "서울", "관악": "서울",
    "서귀포": "제주",
    # 부산 (자치구 이름이 다른 시와 안 겹치는 것만)
    "해운대": "부산", "수영": "부산", "동래": "부산", "연제": "부산", "부산진": "부산", "사상": "부산",
    "사하": "부산", "금정": "부산", "기장": "부산", "영도": "부산",
    # 대구
    "수성": "대구", "달서": "대구", "달성": "대구",
    # 대전
    "유성": "대전", "대덕": "대전",
}

def _is_region_false_positive(reg, title):
    """title 안의 reg 문자열이 실제로는 무관한 합성어의 일부인지 검사한다."""
    return any(compound in title for compound in REGION_NAME_FALSE_POSITIVE_WORDS.get(reg, ()))

# 🔧 [필터링 수정] "동남권"(부산·울산·경남), "충청권/충청권역"(충북·충남) 처럼
# 실제 공고 제목에 흔히 쓰이는 광역 "권역" 명칭은 ALL_KOREA_REGIONS(시/도 단위)
# 어디에도 부분 문자열로 걸리지 않는다. 그래서 notice_region="전국"인 공고(특히
# K-Startup/기업마당 API 수집 건)의 제외 루프가 이런 특정 권역 공고를 걸러내지
# 못하고 전체 사용자에게 새어나갔다(예: "충청권역" 공고가 부산/경기 사용자에게도 발송).
REGION_CLUSTER_MAP = {
    "수도권": ["서울", "경기", "인천"],
    "동남권": ["부산", "울산", "경남"],
    "충청권역": ["충북", "충남", "대전", "세종"],
    "충청권": ["충북", "충남", "대전", "세종"],
    "호남권": ["전북", "전남", "광주"],
    "영남권": ["대구", "경북", "부산", "울산", "경남"],
    "강원권역": ["강원"],
    "강원권": ["강원"],
    "제주권": ["제주"],
}

def is_region_matching(user_region, notice_region, title):
    u_reg = (user_region
             .replace("특별자치시", "")
             .replace("특별자치도", "")
             .replace("특별시", "")
             .replace("광역시", "")
             .replace("도", "")
             .replace("시", "")
             .strip())

    if u_reg in ["전국", ""]:
        return True

    if u_reg in notice_region or (u_reg in title and not _is_region_false_positive(u_reg, title)):
        return True

    if notice_region == "전국":
        # 🔧 [필터링 수정] "비수도권"(서울·경기·인천을 제외한 전 지역 대상, 정부지원사업 제목에
        # 매우 흔히 쓰임)은 글자 그대로 "수도권"이라는 부분 문자열을 포함한다. 그래서 이 체크를
        # 먼저 하지 않으면 아래 REGION_CLUSTER_MAP 루프가 "수도권"만 보고 뜻을 정반대로 해석해서
        # 서울/경기/인천 사용자에게는 내보내고(원래는 제외돼야 함), 정작 대상인 비수도권
        # 사용자(부산/대전 등)는 제외해버리는(원래는 통과돼야 함) 완전히 뒤집힌 결과를 냈다.
        if "비수도권" in title:
            if u_reg in REGION_CLUSTER_MAP["수도권"]:
                return False
        else:
            for cluster, provinces in REGION_CLUSTER_MAP.items():
                if cluster in title and u_reg not in provinces:
                    return False

        for reg in ALL_KOREA_REGIONS:
            if reg not in title or _is_region_false_positive(reg, title):
                continue
            # 🔧 [필터링 수정] reg가 시/군/구면 그 상위 시/도(reg_province)로 바꿔서 사용자
            # 지역과 비교한다 — 안 그러면 "성북"(서울 성북구)이 "서울"과 아예 무관한
            # 지역인 것처럼 취급돼 서울 사용자까지 제외돼 버린다(위 SUB_REGION_PARENT 설명 참고).
            reg_province = SUB_REGION_PARENT.get(reg, reg)
            if u_reg in title or u_reg == reg_province or u_reg in reg_province or reg_province in u_reg:
                continue
            return False
        return True

    return False

def is_category_matching(user_category, title, notice_category="", notice_region=""):
    clean_cat = (user_category or "소상공인").strip()
    
    niche_excludes = [
        "전력산업", "CBAM", "탄소국경", "제약기업", "온디바이스", "원전", "낙농", "축산", "어업",
        "방폭", "시멘트", "콘크리트", "선박제조", "중장비", "플랜트", "반도체 후공정", "서점"
    ]
    if any(bad in title for bad in niche_excludes):
        return False

    if clean_cat in ["창업", "스타트업", "예비창업", "초기창업"]:
        startup_keywords = [
            "창업", "스타트업", "예비창업", "초기창업", "청년창업", "IR", "입주", "보육", 
            "아이디어", "패키지", "챌린지", "밋업", "멘토링", "액셀러", "오픈이노베이션",
            "피칭", "데모데이", "TIPS", "팁스", "캠퍼스타운", "투자유치", "시제품"
        ]
        if any(k in title or k in notice_category for k in startup_keywords):
            return True
        if notice_region not in ["전국", ""] and any(k in title for k in ["사업화", "마케팅", "지원사업"]):
            return True
        return False

    if clean_cat in ["소상공인", "자영업", "골목상권"]:
        rnd_excludes = ["R&D", "연구개발", "기술개발", "특허출원", "성능평가", "인프라구축", "선도연구", "과제 기획"]
        if any(bad in title for bad in rnd_excludes):
            return False
            
        sosang_keywords = [
            "소상공인", "자영업", "골목", "상점", "전통시장", "점포", "착한가격", "온라인 판로",
            "경영환경", "마케팅", "바우처", "이차보전", "특례보증", "시설개선", "임차료", "수수료"
        ]
        if any(k in title or k in notice_category for k in sosang_keywords):
            return True
        if notice_region not in ["전국", ""] and any(k in title for k in ["경영", "자금", "지원사업"]):
            return True
        return False

    if clean_cat in title or clean_cat in notice_category:
        return True
    if notice_region not in ["전국", ""]:
        return True

    return False

# 유저 keywords 필드 안에서 "지역"/"업종"으로 인식되는 힌트 단어 목록.
# add_notice_to_user_buckets(크롤링 중 매칭)와 main()의 발송 단계에서 똑같이 써야 하는데
# 예전엔 두 군데에 거의 동일한 리스트가 복붙돼 있어서 한쪽만 고치면 어긋날 위험이 있었다.
# parse_user_keywords()로 합쳐서 한 곳에서만 관리한다.
REGION_KEYWORD_HINTS = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]
CATEGORY_KEYWORD_HINTS = ["소상공인", "자영업", "창업", "제조", "IT", "스타트업"]

def parse_user_keywords(raw_keywords):
    """유저의 keywords 필드를 (지역, 업종, 자유 키워드 리스트, 전화번호)로 분리한다.
    지역/업종 힌트에도, 전화번호 패턴에도 안 걸리는 나머지 키워드는 '관심 키워드'로 취급해서
    공고 제목 매칭(is_keyword_matching)에 쓴다."""
    keywords = raw_keywords or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",")]

    user_region = "전국"
    user_category = "소상공인"
    user_keywords = []
    user_phone = None

    for kw in keywords:
        kw_clean = str(kw).strip()
        if not kw_clean:
            continue
        digits_only = kw_clean.replace("📱", "").replace("-", "").strip()
        if "📱" in kw_clean or re.match(r'^\d{9,11}$', digits_only):
            user_phone = kw_clean.replace("📱", "").strip()
        elif any(r in kw_clean for r in REGION_KEYWORD_HINTS):
            user_region = kw_clean
        elif any(c in kw_clean for c in CATEGORY_KEYWORD_HINTS):
            user_category = kw_clean
        else:
            user_keywords.append(kw_clean)

    return user_region, user_category, user_keywords, user_phone

def is_keyword_matching(user_keywords, title, notice_category=""):
    """유저가 등록한 '관심 키워드' 중 하나라도 공고 제목/게시판 카테고리에 포함되면 매칭."""
    if not user_keywords:
        return False
    return any(kw and (kw in title or (notice_category and kw in notice_category)) for kw in user_keywords)

def get_notice_priority(item):
    org = item.get('org_name', '')
    title = item.get('title', '')
    
    tp_keywords = ["테크노파크", "경제진흥원", "TP", "경제통상진흥원", "창조경제혁신센터"]
    if any(k in org for k in tp_keywords) or any(k in title for k in ["테크노파크", "경제진흥원"]):
        return 1
    
    startup_keywords = ["K-Startup", "창업", "스타트업", "청년창업", "초기창업", "예비창업"]
    if any(k in org for k in startup_keywords) or any(k in title for k in ["창업", "스타트업"]):
        return 2
        
    if "기업마당" in org:
        return 3
        
    return 4

def send_integrated_kakao_alimtalk(to_phone, user_name, matched_notices, user_region="전국", user_category="소상공인"):
    solapi_url = "https://api.solapi.com/messages/v4/send"
    headers = get_solapi_headers()
    today_str = datetime.date.today().strftime('%Y.%m.%d')
    clean_phone = ''.join(filter(str.isdigit, str(to_phone)))
    total_count = len(matched_notices)

    sorted_notices = sorted(matched_notices, key=get_notice_priority)
    top_notices = sorted_notices[:3]

    notice_lines = []
    for idx, item in enumerate(top_notices, 1):
        t = f"[{item['org_name']}] {item['title']}"
        d = item.get('deadline')
        if d and d not in ['상세링크 참조', '-', '']:
            notice_lines.append(f"{idx}. {t} (~{d})")
        else:
            notice_lines.append(f"{idx}. {t}")

    notice_list_str = "\n".join(notice_lines)
    more_text_str = f"\n\n외 {total_count - 3}건의 맞춤 공고가 더 등록되었습니다." if total_count > 3 else ""
    user_name_val = user_name or "대표"
    user_region_val = user_region or "전국"
    user_category_val = user_category or "소상공인"

    variables = {
        "#{고객명}": user_name_val,
        "#{today_date}": today_str,
        "#{count}": str(total_count),
        "#{notice_list}": notice_list_str,
        "#{more_text}": more_text_str,
        "#{지역}": user_region_val,
        "#{업종}": user_category_val
    }

    alimtalk_text = (
        f"[비즈맵] 맞춤 지원사업 공고 안내\n\n"
        f"안녕하세요, {user_name_val}님!\n\n"
        f"{today_str}\n"
        f"{user_name_val}님 사업장에 딱 맞는 신규 지원사업 공고가 총 {total_count}건 등록되었습니다.\n\n"
        f"📌 오늘의 주요 맞춤 공고\n"
        f"{notice_list_str}{more_text_str}\n\n"
        f"아래 버튼을 누르시면 오늘 추천된 모든 지원사업의 상세 내용과 신청 원문 링크를 한눈에 확인하실 수 있습니다.\n\n"
        f"※ 본 메시지는 대표님께서 비즈맵 서비스 가입 시 직접 신청 및 동의하신 지원사업 맞춤 알림 조건({user_region_val} / {user_category_val})에 따라 신규 공고 발생 시 발송되는 안내 메시지입니다.\n\n"
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
            res = cffi_requests.get(target_url, impersonate="chrome", timeout=12, verify=SSL_VERIFY)
            if res.status_code == 200:
                return res
        except Exception:
            if attempt < max_retries:
                time.sleep(1.5)
    return None

def fetch_with_playwright(target_url, org_name=""):
    # 🔧 [버그수정] 예전엔 browser.close()가 함수 끝에 딱 한 줄로만 있어서, 그 이전 어디서든
    # 예외가 나면(page.content() 등에서) close()가 실행되지 않고 바로 바깥 except로 빠져나갔다.
    # sync_playwright()의 with 블록이 드라이버 프로세스는 정리해도 개별 브라우저 프로세스가
    # 항상 깨끗이 종료된다는 보장은 없어서, 기관 수가 많은 실행에서 좀비 크로미움 프로세스가
    # 쌓일 수 있었다. try/finally로 browser.close()를 반드시 실행되게 보장한다.
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
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

                return content
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        print(f"  ⚠️ Playwright 구동 에러: {e}")
        return None

# 🔥 [개선된 중복 문자열 정밀 제거 함수: 글자 수 편차/뱃지 태그 중복 완벽 제거]
def clean_duplicate_text(text):
    text = " ".join(text.strip().split())
    text = text.rstrip("+>│| ").strip()
    length = len(text)
    
    if length > 15:
        # 1. 완벽히 2등분으로 일치하는 경우
        half = length // 2
        if text[:half].strip() == text[half:].strip():
            return text[:half].strip()
        
        # 2. '연장', '모집' 등 뱃지/공백으로 인해 앞뒤 길이가 약간 다른 상태로 반복된 경우
        for offset in range(-12, 13):
            split_point = half + offset
            if 8 < split_point < length - 8:
                part1 = text[:split_point].strip()
                part2 = text[split_point:].strip()
                
                check_len = min(len(part1), len(part2))
                if check_len > 12:
                    # 핵심 제목 문장(10글자 이상)이 서로 교차 포함되어 있는지 검증
                    if (part1[:check_len-4] in part2) or (part2[:check_len-4] in part1):
                        # '연장' 같은 앞단 잉여 뱃지 태그가 붙지 않은 더 짧고 정갈한 문자열 채택
                        return part1 if len(part1) <= len(part2) else part2
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
    href_lower = href.lower()

    # 🔧 [버그수정] 예전엔 href에 "#"이 한 글자라도 들어있으면 무조건 무효 링크로 취급해서
    # target_url(게시판 목록 URL)로 되돌려버렸다. 문제는 "list.do?id=1#detail" 같은
    # 정상적인 상세 링크에도 "#"이 들어있다는 점이다. 그 결과 그 게시판의 모든 공고가
    # 같은 목록 URL로 뭉개져서, 첫 신규 공고 발송 이후 진짜 신규 공고들이
    # "이미 발송된 링크"로 오판되어 계속 누락되는 문제가 있었다.
    # -> "#"을 포함하는지가 아니라, href가 정말로 자리표시자(placeholder)인 경우만 무효 처리한다.
    is_placeholder_href = href_lower in ("", "#", "#none", "#link", "javascript:void(0)", "javascript:void(0);", "javascript:;")
    is_blocked_href = any(k in href_lower for k in ["kakaoalarm", "login", "mypage"])
    if is_placeholder_href or is_blocked_href:
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

# 게시판 하나에서 한 번에 확인할 최신 공고 개수(위에서부터). 크롤러 실행 사이에 새 글이
# 여러 개 올라와도 놓치지 않기 위함. 너무 키우면 오래된 글까지 신규로 잡힐 수 있음.
MAX_ITEMS_PER_BOARD = 20

def extract_titles_and_links_smart(soup, org_name, target_url, limit=MAX_ITEMS_PER_BOARD):
    """게시판에서 (제목, 링크)를 위에서부터 최대 limit개까지 추출한다.
    기관별 전용 규칙 → 일반 규칙 순으로 시도하고, 처음으로 결과가 나온 규칙의 결과만 쓴다."""
    results = []
    seen_titles = set()

    def _add(txt, link):
        if txt in seen_titles:
            return False
        seen_titles.add(txt)
        results.append((txt, link))
        return len(results) >= limit

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

    if results:
        return results

    if "경상북도경제진흥원" in org_name:
        for item in soup.select(".gallery-title, .gallery_title, .gallery-item, .sub_biz_list li, .card_box, .biz_list li, article, .item"):
            a_tag = item.find("a") or item.find_parent("a")
            raw = item.get_text(" ", strip=True)
            raw = re.sub(r'^([가-힣]{2,10}(지원|육성|사업))?\s*(진행중|모집중|접수중|마감|종료|준비중)?\s*', '', raw)
            txt = clean_duplicate_text(raw)
            if a_tag and is_valid_real_notice(txt):
                if _add(txt, make_full_url(a_tag, target_url)):
                    return results

    if results:
        return results

    if "강원특별자치도" in org_name:
        for node in soup.select(".bo_tit a, td.td_subject a, .list_subject a, .subject a, .item_subject a"):
            txt = clean_duplicate_text(node.get_text()).strip()
            if is_valid_real_notice(txt):
                if _add(txt, make_full_url(node, target_url)):
                    return results

    if results:
        return results

    if "경북테크노파크" in org_name:
        for a in soup.select("a[href*='boardDetail.do'], a[onclick*='fn_egov_inqire_notice'], .bbs_list td.subject a, .board_list td a, table tbody tr td a"):
            txt = clean_duplicate_text(a.get_text(" ", strip=True))
            if is_valid_real_notice(txt) and not txt.isdigit():
                if _add(txt, make_full_url(a, target_url)):
                    return results

    if results:
        return results

    if "대전일자리" in org_name:
        for a in soup.select("a[href*='TSK_PBNC_ID'], a[href*='form.tab'], a[href*='view'], .b-cont a, .board_list li a, .bbs_list tbody tr a, .list_item a"):
            txt = clean_duplicate_text(a.get_text(" ", strip=True)).strip()
            if is_valid_real_notice(txt) and len(txt) >= 10 and re.search(r'(공고|모집|지원사업|선정|참여)', txt):
                if _add(txt, make_full_url(a, target_url)):
                    return results

    if results:
        return results

    if "전북테크노파크" in org_name:
        for tr in soup.select("tbody tr, table tr"):
            a_tag = tr.select_one("td.subject a, td.title a, td.left a, td:nth-child(2) a, a")
            if a_tag:
                txt = clean_duplicate_text(a_tag.text)
                if is_valid_real_notice(txt):
                    if _add(txt, make_full_url(a_tag, target_url)):
                        return results

    if results:
        return results

    if "경기도경제과학" in org_name or "경기기업비서" in org_name:
        for card in soup.select(".card-body, .card, .prj_list_box, .card_item, ul.list li, .list_box li"):
            a_tag = card.select_one(".tit a, a.title, dt a, h4 a, a.prj_name, a") or card.find_parent("a")
            if a_tag:
                txt = clean_duplicate_text(a_tag.get_text(" ", strip=True))
                if is_valid_real_notice(txt):
                    if _add(txt, make_full_url(a_tag, target_url)):
                        return results

    if results:
        return results

    if "서울경제진흥원" in org_name:
        for card in soup.select(".company_support_list li, .card_box, div.card_inner"):
            a_tag = card.select_one("a.title, dt a, h4 a, .tit a, a")
            if a_tag:
                txt = clean_duplicate_text(a_tag.text)
                if is_valid_real_notice(txt):
                    if _add(txt, make_full_url(a_tag, target_url)):
                        return results

    if results:
        return results

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
                if _add(txt, make_full_url(detail_a or links[0], target_url)):
                    return results

    # 💡 일반 테이블 기반 게시판 (부산테크노파크 등 포함)
    if results:
        return results

    for tr in soup.select("tbody tr, table tr"):
        a_tag = tr.select_one("td.subject a, td.title a, td.al a, td.left a, td.align_l a, a")
        if a_tag:
            txt = clean_duplicate_text(a_tag.text)
            if is_valid_real_notice(txt):
                if _add(txt, make_full_url(a_tag, target_url)):
                    return results

    if results:
        return results

    for a in soup.select(".kboard-list-title a, .kboard-title a, .pms-board-list td a, .bbs_list td a, .board_list a, ul.board_list li a"):
        txt = clean_duplicate_text(a.text)
        if is_valid_real_notice(txt):
            if "javascript" not in txt.lower():
                if _add(txt, make_full_url(a, target_url)):
                    return results

    if results:
        return results

    for node in soup.select("li a, article a, .item a, .card a, [class*='card'] a, [class*='item'] a"):
        raw = node.get_text(" ", strip=True)
        txt = clean_duplicate_text(raw).strip()
        if is_valid_real_notice(txt) and re.search(r'(공고|모집|지원사업|참여기업|선정|신청|접수|사업|모집공고)', txt):
            if _add(txt, make_full_url(node, target_url)):
                return results

    return results


def extract_title_and_link_smart(soup, org_name, target_url):
    """(하위 호환) 가장 위의 공고 1건만 반환."""
    items = extract_titles_and_links_smart(soup, org_name, target_url, limit=1)
    return items[0] if items else (None, target_url)

# ==========================================
# 5. 공고 수집 및 유저 바구니 축적 함수
# ==========================================

def add_notice_to_user_buckets(title, org_name, notice_region, category, target_url, user_buckets, sent_history, pending_logs):
    matched_count = 0

    for email, u_data in user_buckets.items():
        user = u_data['user']
        user_region, user_category, user_keywords, _ = parse_user_keywords(user.get("keywords"))

        # 🆕 [필터 확장] 지역은 항상 맞아야 하고(필수 조건), 그 위에 업종 또는 관심 키워드
        # 둘 중 하나만 맞아도 통과시킨다. 즉:
        #   지역 + 업종만 맞아도 통과 / 지역 + 키워드만 맞아도 통과
        #   업종 + 키워드가 맞아도 지역이 안 맞으면 통과 안 됨
        region_ok = is_region_matching(user_region, notice_region, title)
        category_ok = is_category_matching(user_category, title, category, notice_region)
        keyword_ok = is_keyword_matching(user_keywords, title, category)

        if region_ok and (category_ok or keyword_ok):
            u_data['notices'].append({
                "title": title,
                "org_name": org_name,
                "link": target_url,
                "region": notice_region,
                "category": category,
                "deadline": "상세링크 참조"
            })
            matched_count += 1

            # 🔧 [버그수정] 예전엔 매칭 건마다 supabase.insert()를 한 번씩 호출해서
            # (유저 수 × 신규공고 수) 만큼 DB 왕복이 발생했다. 여기서는 바로 insert하지 않고
            # 리스트에 모아뒀다가 main()에서 한 번에 배치로 저장한다(flush_pending_logs 참고).
            pending_logs.append({
                "email": email,
                "title": f"[{org_name}] {title}",
                "link": target_url
            })

    sent_history.add(title)
    if not is_generic_url(target_url):
        sent_history.add(target_url)
    return matched_count

def flush_pending_logs(pending_logs, chunk_size=200):
    """모아둔 notification_logs insert를 청크 단위로 배치 저장한다."""
    if not pending_logs:
        return
    try:
        for i in range(0, len(pending_logs), chunk_size):
            chunk = pending_logs[i:i + chunk_size]
            supabase.table("notification_logs").insert(chunk).execute()
        print(f"📦 발송 이력 {len(pending_logs)}건을 Supabase에 배치 저장했습니다.")
    except Exception as e:
        print(f"⚠️ 발송 이력 배치 저장 실패: {e}")
    finally:
        pending_logs.clear()

def collect_kstartup_api(user_buckets, sent_history, pending_logs):
    print("\n🌐 [공식 API] K-Startup 사업공고 수집 중 (최대 100건)...")
    url = f"https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01?serviceKey={DATA_GO_KEY}&page=1&perPage=100&returnType=json"
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
                detail_url = str(item.get('detl_pg_url') or item.get('detlurl') or '').strip()

                if not is_valid_real_notice(title):
                    continue
                if title in sent_history or (detail_url and detail_url in sent_history):
                    continue

                print(f"  📢 [K-Startup 신규 공고 발견!] {title}")
                add_notice_to_user_buckets(title, "K-Startup", "전국", "창업지원", detail_url or "https://www.k-startup.go.kr", user_buckets, sent_history, pending_logs)
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
                    if t in sent_history or (not is_generic_url(link) and link in sent_history):
                        continue

                    print(f"  📢 [K-Startup(웹백업) 신규 공고 발견!] {t}")
                    add_notice_to_user_buckets(t, "K-Startup", "전국", "창업지원", link, user_buckets, sent_history, pending_logs)
                    new_count += 1
                if new_count == 0:
                    print("  ✅ [K-Startup(웹백업)] 최신 공고 변동 없음")
        except Exception as e:
            print(f"  ❌ K-Startup 웹 백업 실패: {e}")

def collect_bizinfo_api(user_buckets, sent_history, pending_logs):
    print("\n🌐 [기업마당] 지원사업 공고 전수 수집 시작 (API + 웹 3중 백업)...")
    url = f"https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService?serviceKey={DATA_GO_KEY}&pageNo=1&numOfRows=100&dataType=json"
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
                detail_url = str(item.get('pblancUrl') or '').strip()

                if not is_valid_real_notice(title):
                    continue
                if title in sent_history or (detail_url and detail_url in sent_history):
                    continue

                print(f"  📢 [기업마당 API 신규 공고 발견!] {title}")
                add_notice_to_user_buckets(title, "기업마당", "전국", "중소기업지원", detail_url or "https://www.bizinfo.go.kr", user_buckets, sent_history, pending_logs)
                new_count += 1

            if new_count == 0:
                print("  ✅ [기업마당 API] 최신 공고 변동 없음 (이미 발송 완료)")
            api_success = True
    except Exception:
        print("  ⚠️ [기업마당 API 지연/차단] -> 웹 직접 크롤링(전수 탐색)으로 전환합니다.")

    if not api_success:
        try:
            new_count = 0
            for page_no in range(1, 4):
                target_web_url = f"https://www.bizinfo.go.kr/sii/siia/selectSIIA200View.do?schPblancDiv=01&rows=50&cpage={page_no}"
                html = None
                
                res = fetch_cffi_with_retry(target_web_url, max_retries=2)
                if res and res.status_code == 200 and "selectSIIA200Detail.do" in res.text:
                    html = res.content.decode('utf-8', errors='ignore')
                
                if not html:
                    print(f"  ↪️ [기업마당 P.{page_no}] Playwright 브라우저 직접 렌더링 가동...")
                    html = fetch_with_playwright(target_web_url, "기업마당")

                if not html:
                    print(f"  ⚠️ [기업마당 P.{page_no}] 페이지 렌더링 응답 없음")
                    break

                soup = BeautifulSoup(html, "html.parser")
                rows = soup.select("table tbody tr, tbody tr, .table_style01 tr, .table_list tr, tr")
                
                valid_rows = [tr for tr in rows if tr.find("a")]
                print(f"  🔎 [기업마당 P.{page_no}] 공고 {len(valid_rows)}개 행 탐색 중...")
                if not valid_rows:
                    break

                page_has_new = False
                for tr in valid_rows:
                    a_tags = tr.find_all("a")
                    target_a = None
                    for a in a_tags:
                        txt = a.get_text(" ", strip=True)
                        if len(txt) >= 8 and is_valid_real_notice(txt):
                            target_a = a
                            break
                    if not target_a and a_tags:
                        target_a = a_tags[0]

                    if not target_a:
                        continue

                    raw_text = clean_duplicate_text(target_a.get_text(" ", strip=True))
                    if not is_valid_real_notice(raw_text):
                        continue

                    href = target_a.get("href", "")
                    onclick = target_a.get("onclick", "")
                    attr_str = f"{href} {onclick}"
                    
                    pblanc_match = re.search(r"PBLN_[a-zA-Z0-9_]+", attr_str) or re.search(r"pblancId=([^&'\"]+)", attr_str) or re.search(r"fn_goDetail\(['\"]([^'\"]+)['\"]\)", attr_str)
                    
                    if pblanc_match:
                        pblanc_id = pblanc_match.group(1) if len(pblanc_match.groups()) > 0 and pblanc_match.group(1) else pblanc_match.group(0)
                        link = f"https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId={pblanc_id}"
                    elif href and not href.startswith("javascript") and href not in ["#", "#none"]:
                        link = urljoin("https://www.bizinfo.go.kr", href)
                    else:
                        link = "https://www.bizinfo.go.kr/sii/siia/selectSIIA200View.do?schPblancDiv=01"

                    if raw_text in sent_history:
                        continue
                    if not is_generic_url(link) and link in sent_history:
                        continue

                    print(f"  📢 [기업마당 신규 발견!] {raw_text}")
                    print(f"     🔗 링크: {link}")
                    add_notice_to_user_buckets(raw_text, "기업마당", "전국", "중소기업지원", link, user_buckets, sent_history, pending_logs)
                    new_count += 1
                    page_has_new = True

                if not page_has_new:
                    print(f"  ℹ️ [기업마당 P.{page_no}] 이전 발송 이력 구간 도달 (탐색 완료)")
                    break

            if new_count == 0:
                print("  ✅ [기업마당(웹백업)] 최신 공고 변동 없음 (이미 발송 완료)")
        except Exception as web_err:
            print(f"  ❌ 기업마당 백업 크롤링 에러: {web_err}")

def collect_jejutp_api(user_buckets, sent_history, pending_logs):
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
                res = requests.get(api, headers=headers, timeout=10, verify=SSL_VERIFY)
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

        if title in sent_history or (detail and not is_generic_url(detail) and detail in sent_history):
            print(f"  ✅ [제주TP] 변동 없음 (이미 발송 완료)")
        else:
            print(f"  📢 [제주TP 신규 공고 발견!] {title}")
            add_notice_to_user_buckets(title, "제주테크노파크", "제주", "사업공고", detail, user_buckets, sent_history, pending_logs)
    except Exception as e:
        print(f"  ⚠️ [제주TP] 오류: {e}")

def collect_gjbizinfo_api(user_buckets, sent_history, pending_logs):
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
        res = requests.post(api, data=payload, headers=headers, timeout=10, verify=SSL_VERIFY)
        items = res.json().get("dataArr", {}).get("getOnlineList", [])
        if not items:
            return

        newest = items[0]
        title = clean_duplicate_text(str(newest.get("ONLINE_NAME", "")).strip())
        sn = newest.get("ONLINE_SN")
        detail = f"https://www.gjbizinfo.or.kr/onlineView.do?pageId=www48&online_sn={sn}"

        if not is_valid_real_notice(title):
            return

        if title in sent_history or (detail and not is_generic_url(detail) and detail in sent_history):
            print(f"  ✅ [전남광주통합] 변동 없음 (이미 발송 완료)")
        else:
            print(f"  📢 [전남광주통합 신규 공고 발견!] {title}")
            add_notice_to_user_buckets(title, "전남광주통합 기업지원시스템", "전남", "지원사업정보", detail, user_buckets, sent_history, pending_logs)
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
    pending_logs = []
    print(f"👥 현재 활성 구독 유저: {len(user_buckets)}명 (맞춤 공고 바구니 준비 완료)")

    if '수집 여부' not in df.columns:
        print("❌ 엑셀에 '수집 여부' 컬럼이 없습니다. 엑셀 포맷을 확인하세요.")
        return

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
            items = []

            if any(dyn_org in org_name for dyn_org in DYNAMIC_ORGS):
                pw_html = fetch_with_playwright(target_url, org_name)
                if pw_html:
                    pw_soup = BeautifulSoup(pw_html, "html.parser")
                    items = extract_titles_and_links_smart(pw_soup, org_name, target_url)
            else:
                res = fetch_cffi_with_retry(target_url, max_retries=2)
                if res and res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', errors='ignore'), "html.parser")
                    items = extract_titles_and_links_smart(soup, org_name, target_url)

                if not items:
                    pw_html = fetch_with_playwright(target_url, org_name)
                    if pw_html:
                        pw_soup = BeautifulSoup(pw_html, "html.parser")
                        items = extract_titles_and_links_smart(pw_soup, org_name, target_url)

            if not items:
                # 🔧 [버그수정] 예전엔 이 케이스(파싱 자체가 아무 제목도 못 찾음)에서
                # send_dev_warning이 호출되지 않았다. 정작 이 함수가 보내는 경고 문구는
                # "공고 제목 미수집 또는 디자인 개편 감지"라고 돼 있는데, 그 상황을 감지하는
                # 코드가 없었던 것이다. 사이트 개편으로 크롤러가 조용히 깨져도 개발자에게
                # 아무 알림이 가지 않아 무한정 방치될 수 있었다.
                print("  ℹ️ 최신 공고 제목을 찾지 못함 (파싱 실패 가능성 → 개발자 경고 발송)")
                send_dev_warning(org_name, category, target_url, dev_history)
                continue

            # 🔧 [개선] 예전엔 게시판당 맨 위 1건만 확인해서, 실행 사이에 새 글이 여러 개
            # 올라오면 나머지가 영영 누락됐다. 이제 위에서부터 최대 MAX_ITEMS_PER_BOARD건을
            # 확인하고, 이미 발송 이력에 있는 건 건너뛴다.
            new_found = 0
            for latest_title, notice_link in items:
                if not is_valid_real_notice(latest_title):
                    continue
                if latest_title in sent_history or (not is_generic_url(notice_link) and notice_link in sent_history):
                    continue
                print(f"  📢 [신규 공고 발견!] {latest_title}")
                print(f"  🔗 개별 상세 링크: {notice_link}")
                add_notice_to_user_buckets(latest_title, org_name, region, category, notice_link, user_buckets, sent_history, pending_logs)
                new_found += 1

            if new_found == 0:
                print("  ✅ 변동 없음 (이미 발송 완료된 공고)")

        except Exception as e:
            print(f"  ❌ 크롤링 에러: {e}")
            send_dev_warning(org_name, category, target_url, dev_history)
            
        time.sleep(0.5)

    collect_kstartup_api(user_buckets, sent_history, pending_logs)
    collect_bizinfo_api(user_buckets, sent_history, pending_logs)
    collect_jejutp_api(user_buckets, sent_history, pending_logs)
    collect_gjbizinfo_api(user_buckets, sent_history, pending_logs)

    flush_pending_logs(pending_logs)
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

        user_region, user_category, _, user_phone = parse_user_keywords(user.get("keywords"))

        if not user_phone:
            continue

        if len(notices) > 0:
            print(f"\n💬 [{user_name}님 ({user_phone})] 조건:({user_region}/{user_category}) | 정밀 맞춤 공고 {len(notices)}건 발송 중...")
            if USE_KAKAO:
                success, err_msg = send_integrated_kakao_alimtalk(user_phone, user_name, notices, user_region, user_category)
                if success:
                    print(f"  🎉 [통합 알림톡 발송 성공!]")
                    total_sent_users += 1
                else:
                    print(f"  ❌ [통합 알림톡 발송 실패]: {err_msg}")
        else:
            print(f"ℹ️ [{user_name}님] 오늘 신규 정밀 매칭 공고 없음 (불필요한 스팸 발송 완벽 차단)")

    print(f"\n==========================================")
    print(f"✨ 모든 프로세스 완료! (총 {total_sent_users}명에게 맞춤 통합 리포트 발송 완료)")
    print(f"==========================================")

if __name__ == "__main__":
    main()
