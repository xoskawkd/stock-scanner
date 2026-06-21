"""
Tae Scanner v9 — 경량화 + 실전 최적화
코인 제거 / 폰 최적화 / KIS 우선 / 종목명 수정
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import json, os
from scipy import stats as sp
from datetime import datetime, timedelta
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import FinanceDataReader as fdr

try:
    from zoneinfo import ZoneInfo
except:
    ZoneInfo = None

# ============================================================
# API 키 — Streamlit Secrets에서 읽기 (코드에 직접 넣지 마세요)
# share.streamlit.io → 앱 → Settings → Secrets 에서 설정
# ============================================================
def _s(key, default=""):
    try: return st.secrets.get(key, default)
    except: return default

KRX_API_KEY     = _s("KRX_API_KEY")
FINNHUB_API_KEY = _s("FINNHUB_API_KEY")
KIS_APP_KEY     = _s("KIS_APP_KEY")
KIS_APP_SECRET  = _s("KIS_APP_SECRET")
KIS_IS_REAL     = True
KIS_BASE_URL    = "https://openapi.koreainvestment.com:9443"
DART_API_KEY    = _s("DART_API_KEY")
GITHUB_TOKEN    = _s("GITHUB_TOKEN")
GITHUB_GIST_ID  = _s("GITHUB_GIST_ID")
_KIS_TOKEN     = {"token": "", "expires": None}
_KIS_NAME_CACHE = {}  # {code: name} — KIS 가격 조회 시 종목명 함께 저장
import threading
_KIS_LOCK = threading.Lock()

# ============================================================
# 설정
# ============================================================
KR_SCAN_N  = 300  # 코스피 상위
KQ_SCAN_N  = 200  # 코스닥 상위

THRESHOLDS = {
    "KR": {"min_vol":30_000, "max_rsi":83,"max_gain5":0.18,"max_ma20_dev":1.18,"max_hi60":0.95},
    "US": {"min_vol":100_000,"max_rsi":83,"max_gain5":0.18,"max_ma20_dev":1.18,"max_hi60":0.95},
}

# 가중치
DEFAULT_W = {
    "s1_strong":12,"s1_weak":6,"s2_strong":9,"s2_weak":5,
    "s2t_strong":5,"s2t_weak":3,"s3_strong":17,"s3_weak":10,
    "s4":12,"s5":3,"rsi_good":5,"rsi_oversold":3,"rsi_extreme":2,
    "min_pass_score":12,
}

def load_weights():
    if os.path.exists("weights.json"):
        try:
            w = json.load(open("weights.json","r",encoding="utf-8"))
            for k,v in DEFAULT_W.items():
                if k not in w: w[k]=v
            return w
        except: pass
    return DEFAULT_W.copy()

W = load_weights()

# ============================================================
# 포트폴리오 저장
# ============================================================
DATA_FILE = "portfolio.json"

def gist_load() -> list:
    """GitHub Gist에서 포트폴리오 불러오기"""
    if not GITHUB_TOKEN or not GITHUB_GIST_ID:
        return []
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GITHUB_GIST_ID}",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github.v3+json"},
            timeout=5).json()
        files = r.get("files", {})
        if "portfolio.json" in files:
            raw = files["portfolio.json"].get("content", "[]")
            return json.loads(raw)
    except: pass
    return []


def gist_save(data: list) -> bool:
    """GitHub Gist에 포트폴리오 저장"""
    global GITHUB_GIST_ID
    if not GITHUB_TOKEN:
        return False
    payload = {
        "description": "Tae Scanner Portfolio",
        "public": False,
        "files": {"portfolio.json": {"content": json.dumps(data, ensure_ascii=False, indent=2)}}
    }
    try:
        if GITHUB_GIST_ID:
            # 기존 Gist 업데이트
            r = requests.patch(
                f"https://api.github.com/gists/{GITHUB_GIST_ID}",
                headers={"Authorization": f"token {GITHUB_TOKEN}",
                         "Accept": "application/vnd.github.v3+json"},
                json=payload, timeout=5)
        else:
            # 새 Gist 생성
            r = requests.post(
                "https://api.github.com/gists",
                headers={"Authorization": f"token {GITHUB_TOKEN}",
                         "Accept": "application/vnd.github.v3+json"},
                json=payload, timeout=5)
            gist_id = r.json().get("id", "")
            if gist_id:
                GITHUB_GIST_ID = gist_id
                st.sidebar.info(f"✅ Gist 생성됨: {gist_id} — Streamlit Secrets에 GITHUB_GIST_ID 추가하세요")
        return r.status_code in [200, 201]
    except: return False


def load_portfolio():
    # 1. GitHub Gist 우선
    if GITHUB_TOKEN and GITHUB_GIST_ID:
        data = gist_load()
        if data:
            # 로컬에도 백업
            try: json.dump(data, open(DATA_FILE,"w"), ensure_ascii=False)
            except: pass
            return data
    # 2. 로컬 파일 fallback
    if os.path.exists(DATA_FILE):
        try: return json.load(open(DATA_FILE,"r"))
        except: pass
    return []

def save_portfolio(data):
    # 1. 로컬 저장
    try: json.dump(data, open(DATA_FILE,"w"), ensure_ascii=False)
    except: pass
    # 2. GitHub Gist 동기화
    if GITHUB_TOKEN:
        gist_save(data)

# ============================================================
# KIS API
# ============================================================
def kis_token() -> str:
    global _KIS_TOKEN
    if not KIS_APP_KEY or not KIS_APP_SECRET: return ""
    now = datetime.now()
    # 캐시 확인 (Lock 없이 먼저 — 대부분 여기서 끝남)
    if _KIS_TOKEN["token"] and _KIS_TOKEN["expires"] and now < _KIS_TOKEN["expires"]:
        return _KIS_TOKEN["token"]
    # 발급 필요 시 Lock으로 race condition 방지
    with _KIS_LOCK:
        # Lock 진입 후 다시 확인 (다른 스레드가 먼저 발급했을 수 있음)
        if _KIS_TOKEN["token"] and _KIS_TOKEN["expires"] and now < _KIS_TOKEN["expires"]:
            return _KIS_TOKEN["token"]
        for attempt in range(3):  # 최대 3회 재시도
            try:
                r = requests.post(f"{KIS_BASE_URL}/oauth2/tokenP",
                    json={"grant_type":"client_credentials",
                          "appkey":KIS_APP_KEY,"appsecret":KIS_APP_SECRET},
                    timeout=5).json()
                t = r.get("access_token","")
                if t:
                    _KIS_TOKEN["token"] = t
                    _KIS_TOKEN["expires"] = datetime.now() + timedelta(hours=23)
                    return t
            except:
                if attempt == 2: return ""
                import time; time.sleep(0.5)
    return ""

def kis_headers(tr_id):
    t = kis_token()
    if not t: return None
    return {"authorization":f"Bearer {t}","appkey":KIS_APP_KEY,"appsecret":KIS_APP_SECRET,"tr_id":tr_id}

@st.cache_data(ttl=3600, show_spinner=False)
def kis_close_price(code: str) -> tuple:
    """KIS 당일 종가 조회 — 장 마감 후 사용"""
    if not KIS_APP_KEY or not KIS_APP_SECRET: return 0.0, ""
    h = kis_headers("FHKST03010100")
    if not h: return 0.0, ""
    try:
        today = datetime.now().strftime("%Y%m%d")
        r = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": code,
                "fid_input_date_1": today,
                "fid_input_date_2": today,
                "fid_period_div_code": "D",
                "fid_org_adj_prc": "0",
            },
            headers=h, timeout=4).json()
        rows = r.get("output2", []) or r.get("output1", [])
        if rows:
            p = float(str(rows[0].get("stck_clpr", 0) or 0).replace(",",""))
            kor_name = rows[0].get("hts_kor_isnm","").strip()
            if kor_name and code not in _KIS_NAME_CACHE:
                _KIS_NAME_CACHE[code] = kor_name
            if p > 0: return p, "KIS(종가)"
        return 0.0, ""
    except: return 0.0, ""


def kis_price(code: str) -> tuple:
    """KIS 현재가 — 장 중 실시간, 장외 종가"""
def kis_price(code: str) -> tuple:
    """KIS 현재가 — 장 중 실시간, 장외 종가 자동 전환"""
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        return 0.0, ""
    for attempt in range(2):
        h = kis_headers("FHKST01010100")
        if not h: return 0.0, ""
        try:
            r = requests.get(
                f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":code},
                headers=h, timeout=4).json()
            rt_cd = r.get("rt_cd","")
            if rt_cd == "1" and attempt == 0:
                with _KIS_LOCK:
                    _KIS_TOKEN["token"] = ""
                    _KIS_TOKEN["expires"] = None
                continue
            output = r.get("output", {})
            p = float(output.get("stck_prpr", 0) or 0)
            # 종목명 함께 저장 (listing 없어도 이름 표시 가능)
            kor_name = output.get("hts_kor_isnm","").strip()
            if kor_name and code not in _KIS_NAME_CACHE:
                _KIS_NAME_CACHE[code] = kor_name
            if p > 0: return p, "KIS"
            # 장 마감 후 → 종가 API로 전환
            p_close, src_close = kis_close_price(code)
            if p_close > 0: return p_close, src_close
            return 0.0, ""
        except:
            return 0.0, ""
    return 0.0, ""

@st.cache_data(ttl=86400, show_spinner=False)
def kis_name(code: str) -> str:
    """
    KIS 종목명 조회
    1. inquire-price (현재가) 응답의 hts_kor_isnm — 가장 신뢰
    2. search-stock-info 응답 필드들
    """
    if not KIS_APP_KEY or not KIS_APP_SECRET: return ""
    # 방법 1: inquire-price에서 hts_kor_isnm 추출 (가장 정확)
    try:
        h = kis_headers("FHKST01010100")
        if h:
            r = requests.get(
                f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":code},
                headers=h, timeout=3).json()
            out = r.get("output", {})
            n = out.get("hts_kor_isnm","").strip()
            if n: return n
    except: pass
    # 방법 2: search-stock-info
    try:
        h2 = kis_headers("CTPF1002R")
        if h2:
            r2 = requests.get(
                f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/search-stock-info",
                params={"PRDT_TYPE_CD":"300","PDNO":code},
                headers=h2, timeout=3).json()
            out2 = r2.get("output", {})
            n2 = (out2.get("prdt_abrv_name","") or
                  out2.get("prdt_name","") or
                  out2.get("hts_kor_isnm","") or "")
            if n2: return n2.strip()
    except: pass
    return ""

@st.cache_data(ttl=1800, show_spinner=False)
def kis_investor(code: str) -> dict:
    h = kis_headers("FHKST01010900")
    if not h: return {}
    try:
        r = requests.get(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor",
            params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":code},
            headers=h, timeout=4).json()
        result = {"외국인":0,"기관":0,"개인":0,"연기금":0}
        for row in r.get("output",[]):
            inv = row.get("invst_nm","")
            try: net = int(row.get("netbuy_qty",0) or 0)
            except: net = 0
            if "외국인" in inv: result["외국인"] = net
            elif "기관계" in inv: result["기관"] = net
            elif "개인" in inv: result["개인"] = net
            elif "연기금" in inv: result["연기금"] = net
        return result
    except: return {}

@st.cache_data(ttl=1800, show_spinner=False)
def kis_investor_trend(code: str, days=5) -> list:
    h = kis_headers("FHKST01010600")
    if not h: return []
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now()-timedelta(days=days*2)).strftime("%Y%m%d")
        r = requests.get(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-investor",
            params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":code,
                    "fid_begin_dt":start,"fid_end_dt":end},
            headers=h, timeout=4).json()
        trend = []
        for row in r.get("output",[])[:days]:
            try:
                trend.append({
                    "date":   row.get("stck_bsop_date",""),
                    "외국인": int(row.get("frgn_ntby_qty",0) or 0),
                    "기관":   int(row.get("orgn_ntby_qty",0) or 0),
                    "개인":   int(row.get("prsn_ntby_qty",0) or 0),
                    "연기금": int(row.get("pnsn_ntby_qty",0) or 0),
                })
            except: pass
        return trend
    except: return []

def supply_score(code: str) -> int:
    """
    수급 점수 계산 — scan_kr 정렬용
    수급 강도(거래대금 대비 비율) + 연속성 반영
    반환: 0~40점
    """
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        return 0
    trend = kis_investor_trend(code, 5)
    if not trend: return 0

    score = 0
    today = trend[0]
    fore = today.get("외국인", 0)      # 외국인 순매수 수량
    inst = today.get("기관", 0)        # 기관 순매수 수량
    pension = today.get("연기금", 0)   # 연기금 순매수 수량

    # 연속성 계산
    fore_streak = sum(1 for t in trend if t.get("외국인", 0) > 0)
    inst_streak = sum(1 for t in trend if t.get("기관", 0) > 0)

    # 수급 강도 — 거래대금 대비 비율 (KIS investor에서 금액도 가져오면 정확)
    # 수량 기반으로 상대적 강도 추정
    # 외국인 순매수가 기관보다 3배 이상이면 강한 매수세
    # fore_ratio: 외국인 순매수가 전체 순매수(양수만) 중 얼마나 차지하는지
    # 외국인 매수 100, 기관 매도 -100 → 분모=100(외국인만), fore_ratio=1.0
    pos_total = max(fore, 0) + max(inst, 0)
    fore_ratio = fore / pos_total if pos_total > 0 and fore > 0 else 0

    # ── 외국인 ──
    if fore > 0:
        score += 5
        # 강도 보너스 (전체 순매수 중 외국인 비중)
        if fore_ratio >= 0.7: score += 5   # 외국인이 70%+ 주도
        elif fore_ratio >= 0.5: score += 3
        # 연속성 보너스
        if fore_streak >= 3: score += 8
        elif fore_streak >= 2: score += 4
    elif fore < 0:
        score -= 5
        if fore_streak == 0:  # 5일 연속 매도
            score -= 3

    # ── 기관 ──
    if inst > 0:
        score += 5
        if inst_streak >= 2: score += 4
        # 기관 전환 매수 (어제 매도 → 오늘 매수)
        if len(trend) >= 2 and trend[1].get("기관", 0) <= 0:
            score += 3
    elif inst < 0:
        score -= 3

    # ── 연기금 (장기 투자자, 신뢰도 높음) ──
    if pension > 0:
        score += 3  # 2→3점 (연기금 매수는 강한 신호)

    # ── 외국인 + 기관 동시 매수 (가장 강한 신호) ──
    if fore > 0 and inst > 0:
        score += 5
        # 외국인+기관+연기금 동시 → 추가 보너스
        if pension > 0:
            score += 3

    return max(0, min(score, 40))


def supply_signal(code: str) -> dict:
    trend = kis_investor_trend(code, 5)
    if not trend: return {"ok":False}
    today = trend[0]
    fore = today.get("외국인",0)
    inst = today.get("기관",0)
    pension = today.get("연기금",0) if "연기금" in today else 0

    fore_streak = sum(1 for t in trend if t.get("외국인",0)>0)
    inst_rev = len(trend)>=2 and trend[0].get("기관",0)>0 and trend[1].get("기관",0)<=0

    score = 0; signals = []
    if fore > 0:
        score += 2; signals.append(f"외국인 순매수")
        if fore_streak >= 3: score += 2; signals.append(f"{fore_streak}일 연속★")
    elif fore < 0: score -= 2; signals.append("외국인 순매도")
    if inst > 0:
        score += 2; signals.append("기관 순매수")
        if inst_rev: score += 2; signals.append("기관 전환★")
    elif inst < 0: score -= 1; signals.append("기관 순매도")

    if score >= 5: v,c = "🔥강한매수세","#10b981"
    elif score >= 3: v,c = "🟢매수세우위","#10b981"
    elif score >= 1: v,c = "🟡중립","#f59e0b"
    elif score <= -2: v,c = "🔴매도세","#ef4444"
    else: v,c = "⚪중립","#64748b"

    return {"ok":True,"verdict":v,"color":c,"score":score,
            "signals":signals,"fore":fore,"inst":inst,"streak":fore_streak}

# ============================================================
# 가격 조회
# ============================================================
@st.cache_data(ttl=30, show_spinner=False)
def kr_price(code: str, market_map: dict = None) -> tuple:
    # 1순위: KIS 실시간
    if KIS_APP_KEY and KIS_APP_SECRET:
        p, src = kis_price(code)
        if p > 0: return p, src
    # 2순위: KRX
    if KRX_API_KEY:
        try:
            d = datetime.now()
            for _ in range(5):
                ds = d.strftime("%Y%m%d")
                res = requests.get("http://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
                    params={"basDd":ds}, headers={"AUTH_KEY":KRX_API_KEY}, timeout=5).json()
                rows = res.get("OutBlock_1",[])
                if rows:
                    df = pd.DataFrame(rows)
                    row = df[df.get("ISU_SRT_CD","") == code] if "ISU_SRT_CD" in df.columns else pd.DataFrame()
                    if not row.empty:
                        p = float(str(row.iloc[0].get("TDD_CLSPRC","0")).replace(",",""))
                        if p > 0: return p, "KRX"
                d -= timedelta(days=1)
        except: pass
    # yfinance fallback — KIS listing으로 시장 구분
    try:
        # market_map으로 suffix 결정 (listing 반복 호출 방지)
        _sfx = ".KQ"  # 기본 코스닥
        try:
            if market_map and code in market_map:
                mkt = str(market_map[code])
                _sfx = ".KS" if "KOSPI" in mkt else ".KQ"
            else:
                _lst = krx_listing()
                _row = _lst[_lst["Code"]==code]
                if not _row.empty:
                    mkt = str(_row["Market"].values[0])
                    _sfx = ".KS" if "KOSPI" in mkt else ".KQ"
        except:
            _sfx = ".KS" if code[:2] in ["00","01","02","03","04","05","06"] else ".KQ"
        t = yf.Ticker(f"{code}{_sfx}")
        p = float(getattr(t.fast_info,"last_price",0) or 0)
        if p > 0: return p, "yfinance"
    except: pass
    return 0.0, "실패"

# 미국 주요 휴장일
_US_HOLIDAYS = {
    2025: {(1,1),(1,20),(2,17),(4,18),(5,26),(6,19),(7,4),(9,1),(11,27),(12,25)},
    2026: {(1,1),(1,19),(2,16),(4,3),(5,25),(6,19),(7,4),(9,7),(11,26),(12,25)},
    2027: {(1,1),(1,18),(2,15),(4,2),(5,31),(6,19),(7,4),(9,6),(11,25),(12,24)},
}

def is_us_open():
    if not ZoneInfo: return True
    try:
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5: return False
        # 휴장일 체크
        holidays = _US_HOLIDAYS.get(now.year, set())  # 없는 연도는 빈 set
        if (now.month, now.day) in holidays: return False
        open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        return open_t <= now <= close_t
    except: return True

@st.cache_data(ttl=60, show_spinner=False)
def us_price(ticker: str) -> tuple:
    if FINNHUB_API_KEY:
        try:
            r = requests.get("https://finnhub.io/api/v1/quote",
                params={"symbol":ticker,"token":FINNHUB_API_KEY},timeout=3).json()
            c,pc = float(r.get("c",0) or 0), float(r.get("pc",0) or 0)
            if is_us_open() and c>0: return c,"Finnhub"
            if not is_us_open() and pc>0: return pc,"Finnhub(종가)"
            if c>0: return c,"Finnhub"
        except: pass
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="1d",interval="1m")
        if not df.empty:
            p = float(df["Close"].dropna().iloc[-1])
            if p>0: return p,"yfinance"
    except: pass
    return 0.0,"실패"

def us_prepost(ticker: str) -> tuple:
    """장외가 조회 — 캐시 없음 (실시간)"""
    try:
        t = yf.Ticker(ticker)
        reg = t.history(period="1d",interval="1m",prepost=False)
        reg_p = float(reg["Close"].dropna().iloc[-1]) if not reg.empty else 0
        pp = t.history(period="1d",interval="1m",prepost=True)
        if pp.empty: return 0,""
        pp_p = float(pp["Close"].dropna().iloc[-1])
        if not ZoneInfo: return pp_p,"장외"
        now = datetime.now(ZoneInfo("America/New_York"))
        o = now.replace(hour=9,minute=30,second=0)
        c = now.replace(hour=16,minute=0,second=0)
        pre = now.replace(hour=4,minute=0,second=0)
        aft = now.replace(hour=20,minute=0,second=0)
        if o<=now<=c: sess="🏛️정규장"
        elif pre<=now<o: sess="🌅프리마켓"
        elif c<now<=aft: sess="🌙애프터마켓"
        else: sess="🌙애프터마켓"  # 자정 이후도 애프터마켓으로 표시
        diff = (pp_p-reg_p)/reg_p*100 if reg_p>0 else 0
        # 정규장 중엔 차이 없으면 표시 안함
        if sess == "🏛️정규장" and abs(diff) < 0.05: return 0,""
        # 그 외엔 항상 표시
        return pp_p, f"{sess} {diff:+.1f}%"
    except: return 0,""

# ============================================================
# OHLCV
# ============================================================
@st.cache_data(ttl=600, show_spinner=False)
def ohlcv_kr(code):
    try:
        df = fdr.DataReader(code, start="2024-01-01")
        if df is not None and len(df)>=60:
            df.columns=[c.lower() for c in df.columns]
            # 마지막 봉이 너무 오래됐으면 stale 데이터
            last_dt = df.index[-1]
            try:
                last_dt = pd.Timestamp(last_dt).tz_localize(None)
                days_old = (datetime.now() - last_dt.to_pydatetime()).days
                if days_old > 10:  # 10일 이상 오래된 데이터
                    return None
            except: pass
            return df
    except: pass
    return None

@st.cache_data(ttl=600, show_spinner=False)
def ohlcv_us(ticker):
    try:
        df = yf.Ticker(ticker).history(period="1y")
        if not df.empty and len(df)>=60:
            df.columns=[c.lower() for c in df.columns]
            return df
    except: pass
    try:
        df = fdr.DataReader(ticker, start="2024-01-01")
        if df is not None and len(df)>=60:
            df.columns=[c.lower() for c in df.columns]
            return df
    except: pass
    return None

# ============================================================
# 종목 리스트
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def krx_listing():
    try:
        from pykrx import stock as pk
        today = datetime.now().strftime("%Y%m%d")
        rows=[]
        for mkt in ["KOSPI","KOSDAQ"]:
            for code in pk.get_market_ticker_list(today,market=mkt):
                try:
                    name = pk.get_market_ticker_name(code)
                    mc = pk.get_market_cap(today,today,code)
                    cap = int(mc["시가총액"].iloc[0]) if not mc.empty else 0
                    rows.append({"Code":code,"Name":name,"Marcap":cap,"Market":mkt})
                except: pass
        if rows: return pd.DataFrame(rows)
    except: pass
    try:
        df = fdr.StockListing("KRX")
        if df is not None and len(df)>0: return df
    except: pass
    # fallback
    data=[
        ("005930","삼성전자",400e12,"KOSPI"),("000660","SK하이닉스",120e12,"KOSPI"),
        ("207940","삼성바이오로직스",50e12,"KOSPI"),("373220","LG에너지솔루션",60e12,"KOSPI"),
        ("035420","NAVER",30e12,"KOSPI"),("005380","현대차",40e12,"KOSPI"),
        ("000270","기아",30e12,"KOSPI"),("105560","KB금융",15e12,"KOSPI"),
        ("055550","신한지주",12e12,"KOSPI"),("086790","하나금융지주",10e12,"KOSPI"),
        ("068270","셀트리온",10e12,"KOSPI"),("006400","삼성SDI",15e12,"KOSPI"),
        ("329180","HD현대중공업",8e12,"KOSPI"),("042700","한미반도체",8e12,"KOSPI"),
        ("051910","LG화학",20e12,"KOSPI"),("035720","카카오",15e12,"KOSPI"),
        ("003550","LG",10e12,"KOSPI"),("096770","SK이노베이션",7e12,"KOSPI"),
        ("010130","고려아연",8e12,"KOSPI"),("009150","삼성전기",7e12,"KOSPI"),
        ("247540","에코프로비엠",8e12,"KOSDAQ"),("086520","에코프로",6e12,"KOSDAQ"),
        ("196170","알테오젠",3e12,"KOSDAQ"),("091990","셀트리온헬스케어",3e12,"KOSDAQ"),
        ("036570","엔씨소프트",3e12,"KOSDAQ"),("263750","펄어비스",1e12,"KOSDAQ"),
        ("039030","이오테크닉스",1e12,"KOSDAQ"),("214150","클래시스",1e12,"KOSDAQ"),
        ("277810","레인보우로보틱스",1e12,"KOSDAQ"),("357780","솔브레인",2e12,"KOSDAQ"),
    ]
    return pd.DataFrame(data, columns=["Code","Name","Marcap","Market"])

# ── 섹터 지수 코드 매핑 (KRX 업종 지수) ──
SECTOR_INDEX = {
    "반도체":    "KQ11001",  # 코스닥 IT
    "전기전자":  "KS11001",
    "화학":      "KS11003",
    "철강금속":  "KS11004",
    "기계":      "KS11005",
    "조선":      "KS11006",
    "건설":      "KS11007",
    "금융":      "KS11009",
    "증권":      "KS11010",
    "보험":      "KS11011",
    "운수장비":  "KS11008",
    "의약품":    "KS11012",
    "음식료":    "KS11002",
}

@st.cache_data(ttl=1800, show_spinner=False)
def get_sector_momentum(sector_code: str, days: int = 5) -> float:
    """
    섹터 지수 최근 N일 모멘텀 (수익률)
    양수 = 섹터 상승 중, 음수 = 섹터 하락 중
    """
    try:
        df = fdr.DataReader(sector_code, start=(datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d"))
        if df is None or len(df) < days+1: return 0.0
        df.columns = [c.lower() for c in df.columns]
        cl = df["close"].astype(float)
        return float((cl.iloc[-1] - cl.iloc[-days]) / cl.iloc[-days] * 100)
    except: return 0.0

@st.cache_data(ttl=86400, show_spinner=False)  # 업종 정보는 거의 안 바뀜
def get_stock_sector(code: str) -> str:
    """종목의 업종 코드 조회 — KIS API"""
    if not KIS_APP_KEY: return ""
    try:
        h = kis_headers("CTPF1002R")
        if not h: return ""
        r = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/search-stock-info",
            params={"PRDT_TYPE_CD":"300","PDNO":code},
            headers=h, timeout=3).json()
        return r.get("output",{}).get("bstp_kor_isnm","")  # 업종명
    except: return ""

def sector_momentum_score(code: str) -> int:
    """
    섹터 모멘텀 점수 — 종목이 속한 업종이 상승 중인지
    반환: -5 ~ +10점
    """
    sector_name = get_stock_sector(code)
    if not sector_name: return 0

    # 업종명에서 섹터 코드 매핑
    sector_code = ""
    for name, code_s in SECTOR_INDEX.items():
        if name in sector_name:
            sector_code = code_s
            break
    if not sector_code: return 0

    momentum = get_sector_momentum(sector_code, days=5)
    if momentum >= 3:   return 10  # 섹터 5일 +3% 이상 강세
    elif momentum >= 1: return 6   # 섹터 완만한 상승
    elif momentum >= 0: return 3   # 보합
    elif momentum >= -2: return 0  # 약한 하락
    else: return -5                # 섹터 급락 — 감점


@st.cache_data(ttl=3600, show_spinner=False)
def get_dart_disclosures(code: str, days: int = 3) -> list:
    """
    DART 최근 N일 주요 공시 조회
    수주, 자사주, 실적, 유상증자 등 주가 영향 공시 필터링
    """
    if not DART_API_KEY: return []
    try:
        end_dt   = datetime.now().strftime("%Y%m%d")
        start_dt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        r = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={
                "crtfc_key": DART_API_KEY,
                "corp_code": "",        # 전체
                "stock_code": code,     # 종목코드로 필터
                "bgn_de": start_dt,
                "end_de": end_dt,
                "page_count": 20,
            }, timeout=5).json()

        if r.get("status") != "000": return []

        # 주가 긍정 공시 키워드
        POS_KEYWORDS = ["수주","계약","자사주","실적","흑자","배당","공급","MOU","협약","인수"]
        # 주가 부정 공시 키워드
        NEG_KEYWORDS = ["유상증자","전환사채","신주인수권","감사의견","횡령","소송"]

        result = []
        for item in r.get("list", []):
            title = item.get("report_nm", "")
            pos = any(k in title for k in POS_KEYWORDS)
            neg = any(k in title for k in NEG_KEYWORDS)
            if pos or neg:
                result.append({
                    "date":  item.get("rcept_dt", ""),
                    "title": title,
                    "type":  "positive" if pos and not neg else "negative",
                })
        return result
    except: return []


def dart_score(code: str) -> tuple:
    """
    DART 공시 점수 + 공시 요약
    반환: (점수, 공시목록)
    """
    disclosures = get_dart_disclosures(code, days=3)
    if not disclosures: return 0, []

    score = 0
    for d in disclosures:
        if d["type"] == "positive":
            score += 5
            # 수주/계약은 추가 가산
            if any(k in d["title"] for k in ["수주","계약","공급"]):
                score += 3
        else:
            score -= 8  # 유상증자 등은 강한 감점

    return max(-20, min(score, 15)), disclosures


@st.cache_data(ttl=86400, show_spinner=False)
def us_tickers():
    tickers=[]
    for url, id_attr in [
        ("https://en.wikipedia.org/wiki/Nasdaq-100", "constituents"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", None),
    ]:
        try:
            kw = {"attrs":{"id":id_attr}} if id_attr else {}
            tables = pd.read_html(url,**kw)
            if tables:
                df = tables[0]
                col = next((c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower()),None)
                if col:
                    tickers.extend([t.replace(".","-") for t in df[col].dropna()
                                    if isinstance(t,str) and len(t)<=6])
        except: pass
    seen=set(); unique=[]
    for t in tickers:
        if t not in seen: seen.add(t); unique.append(t)
    if len(unique)>=100: return unique[:500]
    return ["NVDA","META","GOOGL","AMZN","MSFT","AMD","TSLA","AAPL","NFLX","AVGO",
            "PLTR","CRM","SNOW","DDOG","NET","CRWD","PANW","PYPL","SOFI","COIN",
            "HIMS","AXSM","MRNA","ASTS","RIVN","JPM","BAC","GS","V","MA"]

US_LIST = us_tickers()

# ============================================================
# 퀀트 예측 엔진
# ============================================================
def _sf(v, d=0.0):
    try: r=float(v); return r if np.isfinite(r) else d
    except: return d

def quant_predict(df, market="KR"):
    OUT={"score":0,"grade":"C","signals":[],"pass":False,
         "buy_min":0.0,"buy_max":0.0,"target":0.0,"stop":0.0,
         "rsi":50.0,"current":0.0,"s1":False,"s2":False,"s3":False,"s4":False,"s5":False,"s6":False,"s7":False}
    th=THRESHOLDS.get(market,THRESHOLDS["KR"])
    try:
        if df is None or len(df)<60: return OUT
        df=df.copy(); df.columns=[c.lower() for c in df.columns]
        cl=df["close"].astype(float); hi=df["high"].astype(float)
        lo=df["low"].astype(float);   vo=df["volume"].astype(float)
        cur=_sf(cl.iloc[-1]); OUT["current"]=cur
        if cur<=0: return OUT

        rejected=False
        avg_vol=_sf(vo.rolling(20).mean().iloc[-1])
        # 거래정지/상장폐지 감지 — 최근 5일 거래량 합계 0
        recent_vol = _sf(vo.iloc[-5:].sum())
        if recent_vol == 0:
            OUT["signals"].append("❌ 거래정지/상장폐지 의심"); rejected=True
        elif avg_vol<th["min_vol"]:
            OUT["signals"].append("❌ 유동성 부족"); rejected=True
        # MA 계산 (rejected 체크보다 먼저)
        _ma20_s  = cl.rolling(20).mean().replace(0, np.nan)
        _std20_s = cl.rolling(20).std()
        ma20 = _sf(_ma20_s.iloc[-1])
        ma5  = _sf(cl.rolling(5).mean().iloc[-1])
        ma60 = _sf(cl.rolling(60).mean().iloc[-1])

        if ma20>0 and cur>ma20*th["max_ma20_dev"]:
            OUT["signals"].append("❌ 이미 급등"); rejected=True
        p5=_sf(cl.iloc[-6]) if len(cl)>=6 else cur
        if p5>0 and (cur-p5)/p5>th["max_gain5"]:
            OUT["signals"].append("❌ 5일 급등"); rejected=True
        hi60=_sf(cl.rolling(60).max().iloc[-1])
        if hi60>0 and cur>=hi60*th["max_hi60"]:
            OUT["signals"].append("❌ 60일 고점권"); rejected=True
        delta=cl.diff()
        # Wilder RSI (EMA alpha=1/14) — 단순 rolling mean보다 정확
        gain=delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss=(-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rsi_s=100-100/(1+gain/loss.replace(0,np.nan))
        rsi=_sf(rsi_s.iloc[-1],50.0); OUT["rsi"]=rsi
        if rsi>th["max_rsi"]:
            OUT["signals"].append(f"❌ RSI 과열 ({rsi:.0f})"); rejected=True

        score=0; setup=0; strong=0; trigger=0

        # S1 BB수축 (_ma20_s, _std20_s 위에서 이미 계산됨)
        bbw=(_std20_s*2)/_ma20_s
        bw=_sf(bbw.iloc[-1]); bwavg=_sf(bbw.rolling(20).mean().iloc[-1])
        s1=False
        if bwavg>0 and bw>0:
            # percentile 기반 squeeze (60봉 중 하위 20%/10%)
            bbw_60 = bbw.iloc[-60:].dropna()
            pct20 = bbw_60.quantile(0.20) if len(bbw_60)>=20 else bwavg*0.85
            pct10 = bbw_60.quantile(0.10) if len(bbw_60)>=20 else bwavg*0.75
            if bw<=pct10: s1=True;setup+=1;strong+=1;score+=W["s1_strong"]; OUT["signals"].append(f"✅ [S1] BB강수축 (하위10%)")
            elif bw<=pct20: s1=True;setup+=1;score+=W["s1_weak"]; OUT["signals"].append(f"🔶 [S1] BB수축 (하위20%)")
            else: OUT["signals"].append("⬜ [S1] BB수축없음")
        OUT["s1"]=s1

        # S2 거래량눌림
        vm5=_sf(vo.rolling(5).mean().iloc[-1]); vm20=_sf(vo.rolling(20).mean().iloc[-1])
        vol_now=_sf(vo.iloc[-1]); s2=False
        if vm20>0:
            if vm5<vm20*0.65: s2=True;setup+=1;strong+=1;score+=W["s2_strong"]; OUT["signals"].append(f"✅ [S2] 거래량강눌림 ({vm5/vm20*100:.0f}%)")
            elif vm5<vm20*0.80: s2=True;setup+=1;score+=W["s2_weak"]; OUT["signals"].append(f"🔶 [S2] 거래량눌림 ({vm5/vm20*100:.0f}%)")
            else: OUT["signals"].append(f"⬜ [S2] 거래량눌림없음 ({vm5/vm20*100:.0f}%)" if vm20>0 else "⬜ [S2] 거래량데이터없음")
        if vm5>0:
            if vol_now>vm5*2.0: trigger+=1;score+=W["s2t_strong"]; OUT["signals"].append(f"➕ [S2T] 거래량폭발")
            elif vol_now>vm5*1.5: trigger+=1;score+=W["s2t_weak"]; OUT["signals"].append(f"➕ [S2T] 거래량증가")
        OUT["s2"]=s2

        # S3 정배열+눌림목
        aligned=ma5>0 and ma20>0 and ma60>0 and ma5>ma20>ma60
        mid_up=ma20>0 and ma60>0 and ma20>ma60
        near=ma20>0 and abs(cur-ma20)/ma20<=0.04; s3=False  # MA20 ±4% (3%는 과도, 5%는 과대)
        if aligned and near: s3=True;setup+=1;strong+=1;score+=W["s3_strong"]; OUT["signals"].append("✅ [S3] 정배열+눌림목 ★")
        elif mid_up and near: s3=True;setup+=1;score+=W["s3_weak"]; OUT["signals"].append("🔶 [S3] 중기상승+눌림목")
        else: OUT["signals"].append(f"⬜ [S3] 눌림목없음 (이격 {abs(cur-ma20)/ma20*100:.1f}%)" if ma20>0 else "⬜ [S3] MA20없음")
        OUT["s3"]=s3

        # S4 RSI다이버전스
        s4=False
        try:
            if len(cl)>=60:
                # RSI Divergence: 최근 60봉 전반(0~29)/후반(30~59)
                pw=cl.iloc[-60:].reset_index(drop=True)
                rw=rsi_s.iloc[-60:].reset_index(drop=True)
                p1_idx = int(pw.iloc[:30].idxmin())   # 0~29 범위
                p2_idx = int(pw.iloc[30:].idxmin())   # 30~59 범위 (이미 절대 인덱스)
                p1 = float(pw.iloc[p1_idx])
                p2 = float(pw.iloc[p2_idx])
                r1 = _sf(rw.iloc[p1_idx], 50.0)
                r2 = _sf(rw.iloc[p2_idx], 50.0)
                s4 = (p2 < p1 * 0.98) and (r2 > r1 + 5) and (r2 < 60)
                if s4: trigger+=1;score+=W["s4"]; OUT["signals"].append(f"✅ [S4] RSI다이버전스 (가격↓{p1:.0f}→{p2:.0f} RSI↑{r1:.0f}→{r2:.0f})")
                else: OUT["signals"].append("⬜ [S4] 다이버전스없음")
            elif len(cl)>=30:
                pw=cl.iloc[-30:].reset_index(drop=True)
                rw=rsi_s.iloc[-30:].reset_index(drop=True)
                p1_idx = int(pw.iloc[:15].idxmin())   # 0~14 범위
                p2_idx = int(pw.iloc[15:].idxmin())   # 15~29 범위
                p1 = float(pw.iloc[p1_idx])
                p2 = float(pw.iloc[p2_idx])
                r1 = _sf(rw.iloc[p1_idx], 50.0)
                r2 = _sf(rw.iloc[p2_idx], 50.0)
                s4 = (p2 < p1 * 0.98) and (r2 > r1 + 5) and (r2 < 60)
                if s4: trigger+=1;score+=W["s4"]; OUT["signals"].append("✅ [S4] RSI다이버전스 30봉")
                else: OUT["signals"].append("⬜ [S4] 다이버전스없음")
            elif len(cl)>=20:
                pw=cl.iloc[-20:].reset_index(drop=True)
                rw=rsi_s.iloc[-20:].reset_index(drop=True)
                p1_idx=int(pw.iloc[:10].idxmin())
                p2_idx=int(pw.iloc[10:].idxmin())  # 10~19 범위 절대 인덱스
                p1=float(pw.iloc[p1_idx]); p2=float(pw.iloc[p2_idx])
                r1=_sf(rw.iloc[p1_idx],50); r2=_sf(rw.iloc[p2_idx],50)
                s4=(p2<p1*0.98) and (r2>r1+5) and (r2<60)
                if s4: trigger+=1;score+=W["s4"]; OUT["signals"].append("✅ [S4] RSI다이버전스 ★")
                else: OUT["signals"].append("⬜ [S4] 다이버전스없음")
            else:
                OUT["signals"].append("⬜ [S4] 데이터부족")
        except: OUT["signals"].append("⬜ [S4] 계산실패")
        OUT["s4"]=s4

        # S5 캔들
        s5=False
        try:
            if "open" in df.columns and len(df)>=2:
                op=df["open"].astype(float)
                o1,c1=_sf(op.iloc[-1]),_sf(cl.iloc[-1])
                h1,l1=_sf(hi.iloc[-1]),_sf(lo.iloc[-1])
                o2,c2=_sf(op.iloc[-2]),_sf(cl.iloc[-2])
                if all(v>0 for v in [o1,c1,h1,l1,o2,c2]):
                    body=abs(c1-o1); lower=min(o1,c1)-l1; upper=h1-max(o1,c1)
                    hammer=body>0 and lower>body*2 and upper<body*0.5
                    bull=c2<o2 and c1>o1
                    s5=hammer or bull
                    if s5: trigger+=1;score+=W["s5"]; OUT["signals"].append(f"✅ [S5] {'망치형' if hammer else '양봉전환'}")
                    else: OUT["signals"].append("⬜ [S5] 캔들없음")
        except: OUT["signals"].append("⬜ [S5] 캔들실패")
        OUT["s5"]=s5

        # ── [S6] 거래량 폭발 후 눌림 — 가산점 전용 ──
        s6 = False
        try:
            vm20_s6 = vo.rolling(20).mean()
            burst_day = -1; burst_price = 0.0
            for k in range(3, 16):
                vm_k = _sf(vm20_s6.iloc[-k])
                if vm_k > 0 and _sf(vo.iloc[-k]) > vm_k * 1.5:
                    burst_day = k
                    burst_price = _sf(cl.iloc[-k])
                    break
            if burst_day >= 3 and burst_price > 0:
                # 폭발일 이후 눌림 확인 (최소 2일치 필요)
                vol_after = [_sf(vo.iloc[-j]) for j in range(1, burst_day)]
                vm_now    = _sf(vm20_s6.iloc[-1])
                vol_dried = len(vol_after) >= 2 and np.mean(vol_after) < vm_now * 0.85
                price_ok  = cur >= burst_price * 0.92
                if vol_dried and price_ok:
                    s6 = True
                    if s3:
                        score += 8
                        OUT["signals"].append(f"✅ [S6] 거래량폭발+정배열 ★ ({burst_day}일전)")
                    else:
                        score += 4
                        OUT["signals"].append(f"🔶 [S6] 거래량폭발후눌림 ({burst_day}일전)")
        except: pass
        OUT["s6"] = s6

        # ── [S7] OBV 매집 신호 ──
        # OBV가 MA20 위에 있고 5일 전보다 상승 중이면 조용한 매집
        s7 = False
        try:
            obv = (np.sign(cl.diff()) * vo).fillna(0).cumsum()
            obv_ma20 = obv.rolling(20).mean()
            obv_now  = _sf(obv.iloc[-1])
            obv_ma   = _sf(obv_ma20.iloc[-1])
            obv_5ago = _sf(obv.iloc[-6]) if len(obv)>=6 else obv_now
            obv_rising = obv_now > obv_ma       # OBV > MA20
            obv_trend  = obv_now > obv_5ago     # 5일 전보다 상승

            if obv_rising and obv_trend:
                s7 = True
                # S3+S7 조합: 정배열 눌림 + OBV 매집 → 강한 신호
                if s3:
                    score += 8
                    OUT["signals"].append("✅ [S7] OBV매집+정배열 ★")
                else:
                    score += 5
                    OUT["signals"].append("✅ [S7] OBV 매집 신호")
            elif obv_now < obv_ma and not obv_trend:
                OUT["signals"].append("⬜ [S7] OBV 분산 중")
            else:
                OUT["signals"].append("⬜ [S7] OBV 중립")
        except:
            OUT["signals"].append("⬜ [S7] OBV 계산실패")
        OUT["s7"] = s7

        # RSI 보너스
        if 40<=rsi<=55: score+=W["rsi_good"]; OUT["signals"].append(f"✅ RSI 매수구간 ({rsi:.0f})")
        elif 30<=rsi<40: score+=W["rsi_oversold"]; OUT["signals"].append(f"🔶 RSI 과매도 ({rsi:.0f})")
        elif rsi<30: score+=W["rsi_extreme"]; OUT["signals"].append(f"🔶 RSI 극과매도 ({rsi:.0f})")
        else: OUT["signals"].append(f"⬜ RSI 보너스없음 ({rsi:.0f})")

        # ATR 목표가/손절가
        _tgt=cur*1.08; _stp=cur*0.93; _blo=cur*0.97; _bhi=cur*1.02
        try:
            tr=pd.concat([hi-lo,(hi-cl.shift()).abs(),(lo-cl.shift()).abs()],axis=1).max(axis=1)
            atr=_sf(tr.rolling(14).mean().iloc[-1])
            if atr>0:
                std20=_sf(_std20_s.iloc[-1])  # 중복 계산 제거 (_std20_s 재사용)
                bb_top=ma20+std20*2 if ma20>0 and std20>0 else 0
                t_atr=cur+atr*2; t_bb=bb_top if bb_top>cur else cur+atr*2
                _tgt=min(t_atr,t_bb)
                _tgt=max(_tgt,cur*1.05); _tgt=min(_tgt,cur*1.15)
                _stp=cur-atr*1.5; _stp=max(_stp,cur*0.93); _stp=min(_stp,cur*0.97)
                _blo=max(cur*0.97,cur-atr*0.5); _bhi=min(cur*1.02,cur+atr*0.3)
        except: pass
        OUT["target"]=round(_tgt,4); OUT["stop"]=round(_stp,4)
        OUT["buy_min"]=round(_blo,4); OUT["buy_max"]=round(_bhi,4)
        OUT["score"]=int(score)

        # 통과 게이트
        # S3(정배열눌림목) 또는 S4(RSI다이버전스) 하나 이상 + 점수
        core = s3 or s4
        OUT["pass"]=(not rejected) and core and (score>=W["min_pass_score"])

        # 등급
        # 최대 score=63 기준 재조정
        if score>=55 and strong>=1 and trigger>=1: g="A+"
        elif score>=45 and setup>=1 and trigger>=1: g="A"
        elif score>=35 and setup>=1: g="B+"
        elif score>=28 and setup>=1: g="B"
        else: g="C"
        OUT["grade"]=g

    except Exception as e:
        OUT["signals"].append(f"오류:{e}")
    return OUT

# ============================================================
# 스캐너
# ============================================================
@st.cache_data(ttl=300, show_spinner=False)
def scan_kr():
    listing = krx_listing()
    kospi  = listing[listing["Market"].str.contains("KOSPI", na=False)]  if "Market" in listing.columns else listing
    kosdaq = listing[listing["Market"].str.contains("KOSDAQ",na=False)]  if "Market" in listing.columns else pd.DataFrame()
    kp = kospi[kospi["Marcap"]>3e11].nlargest(KR_SCAN_N,"Marcap")
    kq = kosdaq[kosdaq["Marcap"]>5e10].nlargest(KQ_SCAN_N,"Marcap") if not kosdaq.empty else pd.DataFrame()
    targets = pd.concat([kp,kq]).drop_duplicates("Code")
    codes   = list(zip(targets["Code"],targets["Name"]))
    # ② market_map 미리 생성 — kr_price listing 반복 검색 방지
    market_map = dict(zip(listing["Code"], listing.get("Market", pd.Series()))) if "Market" in listing.columns else {}
    # KIS 토큰 미리 발급 (ThreadPool 진입 전) — 스레드들이 캐시 토큰 재사용
    if KIS_APP_KEY and KIS_APP_SECRET:
        _pre_token = kis_token()
        if not _pre_token:
            st.sidebar.warning("⚠️ KIS 토큰 발급 실패 — yfinance로 대체됩니다")

    def _fetch(item):
        code,name = item
        df = ohlcv_kr(code)
        if df is None: return {"_skip":True,"why":"데이터없음"}
        r = quant_predict(df,"KR")
        if not r["pass"]:
            why=next((s for s in r["signals"] if "❌" in s),"조건미충족")
            return {"_skip":True,"why":why}
        p, src = kr_price(code, market_map)
        if p<=0: p=r["current"]
        tgt = int(r["target"]) if r["target"]>p*1.03 and r["target"]<=p*1.15 else int(p*1.08)
        stp = int(r["stop"])   if r["stop"]>p*0.85  and r["stop"]<p*0.98  else int(p*0.93)
        bmin = int(r["buy_min"]) if r["buy_min"]>p*0.90 and r["buy_min"]<p else int(p*0.97)
        bmax = int(r["buy_max"]) if r["buy_max"]>p and r["buy_max"]<p*1.05 else int(p*1.02)
        # 종목명: 캐시 → listing → 코드 그대로
        display_name = _KIS_NAME_CACHE.get(code) or name
        return {"_skip":False,"종목":display_name,"코드":code,"등급":r["grade"],"점수":r["score"],
                "현재가":int(p),"RSI":round(r["rsi"],1),
                "매수구간":f"₩{bmin:,}~₩{bmax:,}",
                "목표가":tgt,"손절가":stp,"signals":r["signals"],"source":src,
                "s_flags":[r["s1"],r["s2"],r["s3"],r["s4"],r["s5"],r.get("s6",False),r.get("s7",False)],
                "수급점수":0,"섹터점수":0,"공시점수":0,"공시목록":[],"종합점수":r["score"],"섹터강세":False}

    with ThreadPoolExecutor(max_workers=8) as ex:
        raw=list(ex.map(_fetch,codes))
    skips=[r for r in raw if r.get("_skip")]
    passed=[r for r in raw if not r.get("_skip")]

    # pass된 종목 KIS 가격+종목명 재조회 (ThreadPool 밖 — 토큰 안정적)
    if KIS_APP_KEY and KIS_APP_SECRET:
        for item in passed:
            try:
                p_kis, src_kis = kis_price(item["코드"])
                if p_kis > 0:
                    item["현재가"] = int(p_kis)
                    item["source"] = src_kis
                # 종목명 캐시에서 업데이트
                cached_name = _KIS_NAME_CACHE.get(item["코드"])
                if cached_name and cached_name != item["코드"]:
                    item["종목"] = cached_name
            except: pass

    # 수급 점수 추가 (KIS 있을 때만)
    if KIS_APP_KEY and KIS_APP_SECRET and passed:
        def _add_supply(r):
            try:
                sup  = supply_score(r["코드"])
                sec  = sector_momentum_score(r["코드"])
                dart_s, dart_list = dart_score(r["코드"]) if DART_API_KEY else (0, [])
                r["수급점수"] = sup
                r["섹터점수"] = sec
                r["공시점수"] = dart_s
                r["공시목록"] = dart_list[:2]  # 최대 2개만
                r["종합점수"] = r["점수"] + sup + sec + dart_s
                r["섹터강세"] = sec >= 6
            except:
                r["수급점수"] = 0; r["섹터점수"] = 0
                r["공시점수"] = 0; r["공시목록"] = []
                r["종합점수"] = r["점수"]
                r["섹터강세"] = False
            return r
        with ThreadPoolExecutor(max_workers=5) as ex:
            passed = list(ex.map(_add_supply, passed))
        # 종합점수(차트+수급)로 정렬
        top5 = sorted(passed, key=lambda x: x["종합점수"], reverse=True)[:5]
    else:
        for r in passed:
            r["수급점수"] = 0
            r["종합점수"] = r["점수"]
        top5 = sorted(passed, key=lambda x: x["점수"], reverse=True)[:5]

    return top5, skips

@st.cache_data(ttl=300, show_spinner=False)
def scan_us():
    rt = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs={t:ex.submit(us_price,t) for t in US_LIST}
        for t,f in futs.items():
            try: rt[t]=f.result()
            except: rt[t]=(0.0,"실패")

    def _fetch(ticker):
        df=ohlcv_us(ticker)
        if df is None: return {"_skip":True,"why":"데이터없음"}
        r=quant_predict(df,"US")
        if not r["pass"]:
            why=next((s for s in r["signals"] if "❌" in s),"조건미충족")
            return {"_skip":True,"why":why}
        p,src=rt.get(ticker,(0.0,"없음"))
        if p<=0: p,src=us_price(ticker)
        if p<=0: p=r["current"]
        def uf(v): return f"${v:,.4f}" if v<1 else f"${v:,.3f}" if v<10 else f"${v:,.2f}"
        tgt=round(r["target"],2) if r["target"]>p*1.03 and r["target"]<=p*1.15 else round(p*1.08,2)
        stp=round(r["stop"],2)   if r["stop"]>p*0.85  and r["stop"]<p*0.98  else round(p*0.93,2)
        return {"_skip":False,"종목":ticker,"등급":r["grade"],"점수":r["score"],
                "현재가":round(p,2),"RSI":round(r["rsi"],1),
                "매수구간":f"{uf(p*0.97)}~{uf(p*1.02)}",
                "목표가":tgt,"손절가":stp,"signals":r["signals"],"source":src,
                "s_flags":[r["s1"],r["s2"],r["s3"],r["s4"],r["s5"],r.get("s6",False),r.get("s7",False)],
                "수급점수":0,"섹터점수":0,"공시점수":0,"공시목록":[],"종합점수":r["score"],"섹터강세":False}

    with ThreadPoolExecutor(max_workers=8) as ex:
        raw=list(ex.map(_fetch,US_LIST))
    skips=[r for r in raw if r.get("_skip")]
    top5=sorted([r for r in raw if not r.get("_skip")],key=lambda x:x["점수"],reverse=True)[:5]
    return top5, skips

# ============================================================
# 포트폴리오 데이터
# ============================================================
def get_stock_name(code: str) -> str:
    """종목명 조회 — 캐시 → KIS API → listing 순서"""
    # 1. 가격 조회 시 저장된 캐시 (가장 빠름)
    if code in _KIS_NAME_CACHE:
        return _KIS_NAME_CACHE[code]
    # 2. KIS search-stock-info API
    if KIS_APP_KEY:
        n = kis_name(code)
        if n:
            _KIS_NAME_CACHE[code] = n
            return n
    # 3. krx_listing (pykrx/fdr/하드코딩)
    try:
        listing = krx_listing()
        row = listing[listing["Code"]==code]
        if not row.empty:
            n = row["Name"].values[0]
            _KIS_NAME_CACHE[code] = n
            return n
    except: pass
    # 4. 가격 조회하면서 이름 가져오기 (마지막 수단)
    if KIS_APP_KEY:
        p, _ = kis_price(code)
        if code in _KIS_NAME_CACHE:
            return _KIS_NAME_CACHE[code]
    return code

def portfolio_data(name: str) -> dict:
    FAIL = {"label":None,"curr":0,"score":0,"grade":"F","rsi":0,
            "currency":"KRW","stop":0,"target":0,"buy_min":0,"buy_max":0,
            "source":"실패","ok":False,"signals":[]}

    if name.isdigit() and len(name)==6:
        p, src = kr_price(name)
        df = ohlcv_kr(name)
        # 종목명: kis_name 직접 호출 (ttl=86400 캐시, 빠름)
        stock_name = ""
        if KIS_APP_KEY:
            stock_name = kis_name(name)  # KIS search-stock-info
        if not stock_name:
            stock_name = _KIS_NAME_CACHE.get(name, "")
        if not stock_name:
            try:
                lst = krx_listing()
                row = lst[lst["Code"]==name]
                if not row.empty:
                    stock_name = str(row["Name"].values[0])
            except: pass
        label = f"{stock_name} ({name})" if stock_name and stock_name != name else name

        if df is not None:
            r = quant_predict(df,"KR")
            curr = p if p>0 else r["current"]
            if curr <= 0: return FAIL
            tgt = int(r["target"]) if r["target"]>curr*1.03 and r["target"]<=curr*1.15 else int(curr*1.08)
            stp = int(r["stop"])   if r["stop"]>curr*0.85  and r["stop"]<curr*0.98  else int(curr*0.93)
            return {"label":label,"curr":curr,"score":r["score"],"grade":r["grade"],
                    "rsi":round(r["rsi"],1),"currency":"KRW","stop":stp,"target":tgt,
                    "buy_min":int(curr*0.97),"buy_max":int(curr*1.02),
                    "source":src,"ok":curr>0,"signals":r["signals"]}
        if p>0:
            return {"label":label,"curr":p,"score":0,"grade":"-","rsi":50,"currency":"KRW",
                    "stop":int(p*0.93),"target":int(p*1.08),
                    "buy_min":int(p*0.97),"buy_max":int(p*1.02),
                    "source":src,"ok":True,"signals":[]}
        return FAIL

    # 해외
    p, src = us_price(name)
    pp, pp_label = us_prepost(name)
    df = ohlcv_us(name)
    def ur(v): return round(v,4) if v<1 else round(v,3) if v<10 else round(v,2)

    if df is not None:
        r = quant_predict(df,"US")
        curr = p if p>0 else r["current"]
        if curr<=0: return FAIL
        tgt=ur(r["target"]) if r["target"]>curr*1.03 and r["target"]<=curr*1.15 else ur(curr*1.08)
        stp=ur(r["stop"])   if r["stop"]>curr*0.85  and r["stop"]<curr*0.98  else ur(curr*0.93)
        return {"label":f"{name} ({src})","curr":ur(curr),"score":r["score"],"grade":r["grade"],
                "rsi":round(r["rsi"],1),"currency":"USD","stop":stp,"target":tgt,
                "buy_min":ur(curr*0.97),"buy_max":ur(curr*1.02),
                "source":src,"ok":curr>0,"signals":r["signals"],
                "prepost":pp,"prepost_label":pp_label}
    if p>0:
        return {"label":f"{name} ({src})","curr":ur(p),"score":0,"grade":"-","rsi":50,
                "currency":"USD","stop":ur(p*0.93),"target":ur(p*1.08),
                "buy_min":ur(p*0.97),"buy_max":ur(p*1.02),
                "source":src,"ok":True,"signals":[],
                "prepost":pp if pp>0 else 0,"prepost_label":pp_label}
    return FAIL

# ============================================================
# UI 시작
# ============================================================
st.set_page_config(page_title="Tae Scanner v9", layout="wide")
if "portfolio" not in st.session_state:
    st.session_state.portfolio = load_portfolio()
if "my_portfolio" in st.session_state:
    # 구버전 마이그레이션
    st.session_state.portfolio = st.session_state.my_portfolio
    del st.session_state["my_portfolio"]

# 사이드바
st.sidebar.title("🛡️ Tae Scanner v9")
with st.sidebar.expander("🔑 API 상태"):
    kis_ok = bool(KIS_APP_KEY and KIS_APP_SECRET)
    kis_token_ok = bool(_KIS_TOKEN.get("token",""))
    st.write("KIS키:", "✅" if kis_ok else "❌ 키없음")
    if kis_ok:
        st.write("KIS토큰:", "✅발급됨" if kis_token_ok else "❌미발급(첫요청시자동)")
    st.write("KRX:",  "✅" if KRX_API_KEY  else "❌ (fallback)")
    st.write("Finnhub:", "✅" if FINNHUB_API_KEY else "❌")
    st.write("DART:",    "✅" if DART_API_KEY    else "❌ (공시 비활성)")
    st.write("GitHub:",  "✅ Gist 연동" if (GITHUB_TOKEN and GITHUB_GIST_ID) else
                         ("⚠️ Gist ID 없음" if GITHUB_TOKEN else "❌ (포트폴리오 휘발 위험)"))
    # 미국장 시간대 표시
    if ZoneInfo:
        try:
            now_et = datetime.now(ZoneInfo("America/New_York"))
            st.write(f"🇺🇸 ET: {now_et.strftime('%H:%M')} ({'장중' if is_us_open() else '장외'})")
        except: pass

try:
    fg = requests.get("https://api.alternative.me/fng/?limit=1",timeout=3).json()
    fgv = fg["data"][0]["value"]
    fgt = "극탐욕" if int(fgv)>=75 else "탐욕" if int(fgv)>=60 else "중립" if int(fgv)>=40 else "공포" if int(fgv)>=25 else "극공포"
    st.sidebar.metric("공포탐욕", f"{fgv} ({fgt})")
except: pass
st.sidebar.metric("🇺🇸 미국장", "OPEN" if is_us_open() else "CLOSED")

# 재스캔
# 버튼 3개: 추천 재스캔 / 관심종목 재스캔 / 전체 캐시 초기화
if st.sidebar.button("🔄 추천 재스캔", use_container_width=True, type="primary"):
    scan_kr.clear(); scan_us.clear()
    ohlcv_kr.clear(); ohlcv_us.clear()
    st.rerun()

if st.sidebar.button("👀 관심종목 재평가", use_container_width=True):
    # ohlcv 캐시만 날림 (portfolio_data는 원래 캐시 없음)
    ohlcv_kr.clear(); ohlcv_us.clear()
    kis_investor.clear()
    kis_investor_trend.clear()
    st.session_state["watch_refresh"] = True
    st.rerun()

if st.sidebar.button("🗑️ 전체 캐시 초기화", use_container_width=True):
    # 포트폴리오 데이터 제외 모든 캐시 초기화
    scan_kr.clear(); scan_us.clear()
    ohlcv_kr.clear(); ohlcv_us.clear()
    krx_listing.clear(); us_tickers.clear()
    kis_price.clear(); kis_investor.clear()
    kis_investor_trend.clear(); kis_name.clear()
    st.rerun()

st.title("🚀 Tae Scanner v9")
st.caption("S3 정배열눌림목 AND S4 RSI다이버전스 — 코스피+코스닥+나스닥+S&P500")

# ============================================================
# 자산관리
# ============================================================
st.header("💼 자산관리")

tab_watch, tab_hold = st.tabs(["👀 관심종목", "💰 보유종목"])

with tab_watch:
    st.caption("스캐너 추천 종목 등록 → 다음날 매수 여부 자동 판단")
    with st.form("watch_form", clear_on_submit=True):
        w1,w2,w3 = st.columns([2,2,1])
        wn = w1.text_input("종목코드", placeholder="005930 / AAPL")
        wm = w2.text_input("메모", placeholder="스캐너 추천, 관심 이유")
        if w3.form_submit_button("👀 추가"):
            if wn:
                nm = wn.strip().upper()
                if not any(p["name"]==nm for p in st.session_state.portfolio):
                    st.session_state.portfolio.append({"name":nm,"buy":0.0,"date":"","type":"watch","memo":wm.strip()})
                    save_portfolio(st.session_state.portfolio)
                    st.rerun()

with tab_hold:
    with st.form("hold_form", clear_on_submit=True):
        c1,c2,c3,c4 = st.columns([2,1,1,1])
        hn = c1.text_input("종목코드", placeholder="005930 / AAPL")
        hb = c2.number_input("평단가", min_value=0.0, step=0.01, format="%.4f")
        hd = c3.text_input("매수일자", placeholder="2024-01-15")
        if c4.form_submit_button("➕ 추가"):
            if hn and hb>0:
                nm = hn.strip().upper()
                # 관심→보유 업그레이드
                upgraded = False
                for p in st.session_state.portfolio:
                    if p["name"]==nm and p.get("type")=="watch":
                        p.update({"buy":float(hb),"date":hd.strip(),"type":"hold"})
                        upgraded = True; break
                if not upgraded:
                    st.session_state.portfolio.append({"name":nm,"buy":float(hb),"date":hd.strip(),"type":"hold","memo":""})
                save_portfolio(st.session_state.portfolio)
                st.rerun()

# 포트폴리오 카드
to_remove = None
for i, p in enumerate(st.session_state.portfolio):
    name = p["name"]; buy = p.get("buy",0); ptype = p.get("type","hold")
    d = portfolio_data(name)
    if not d["ok"] or d["curr"]<=0:
        st.error(f"⚠️ {name} 조회 실패")
        if st.button(f"❌ 삭제", key=f"del_err_{i}"): to_remove=i
        continue

    curr = d["curr"]; is_kr = d["currency"]=="KRW"
    profit = (curr-buy)/buy*100 if buy>0 else 0
    def uf(v): return f"${v:,.4f}" if v<1 else f"${v:,.3f}" if v<10 else f"${v:,.2f}"
    fmt = (lambda v: f"₩{int(v):,}") if is_kr else uf
    gc = {"A+":"#f59e0b","A":"#10b981","B+":"#3b82f6","B":"#94a3b8","C":"#64748b"}.get(d["grade"],"#64748b")
    sigs = d.get("signals",[])
    s3_on = any("S3" in s and "✅" in s for s in sigs)
    s4_on = any("S4" in s and "✅" in s for s in sigs)

    # ── 관심종목 카드 ──
    if ptype == "watch":
        # 갭 계산
        gap_pct = 0.0
        try:
            df_tmp = ohlcv_kr(name) if is_kr else ohlcv_us(name)
            if df_tmp is not None and len(df_tmp)>=2:
                prev = float(df_tmp["close"].iloc[-2])
                if prev>0: gap_pct = (curr-prev)/prev*100
        except: pass

        if abs(gap_pct)<3: gv="🟢 갭 양호"; gc2="#10b981"; gd=f"갭 {gap_pct:+.1f}% — 매수 검토"
        elif abs(gap_pct)<5: gv="🟡 소폭 갭"; gc2="#f59e0b"; gd=f"갭 {gap_pct:+.1f}% — 눌림 기다려"
        else: gv="🔴 갭 과다"; gc2="#ef4444"; gd=f"갭 {gap_pct:+.1f}% — 추격 위험"

        signal_ok = s3_on or s4_on  # quant_predict core와 동일 조건

        # 수급
        sup_html=""
        if is_kr and KIS_APP_KEY:
            sup = supply_signal(name)
            if sup.get("ok"):
                sup_sigs = " / ".join(sup.get("signals",[])[:3])
                sup_html = f'<div style="background:#0f172a;padding:8px;border-radius:6px;margin:6px 0;font-size:12px;">수급: <span style="color:{sup["color"]};font-weight:bold;">{sup["verdict"]}</span> <span style="color:#64748b;">{sup_sigs}</span></div>'

        # 종합 판단
        bs = 0
        if abs(gap_pct)<3: bs+=2
        elif abs(gap_pct)<5: bs+=1
        if signal_ok: bs+=2
        if is_kr and KIS_APP_KEY:
            sup2 = supply_signal(name)
            if sup2.get("ok") and sup2.get("score",0)>=3: bs+=2
        if bs>=5: bv="🟢 매수 적극 고려"; bc="#10b981"
        elif bs>=3: bv="🟡 조건부 매수"; bc="#f59e0b"
        else: bv="🔴 매수 보류"; bc="#ef4444"

        memo = p.get("memo","")
        st.markdown(f"""
<div style="background:#1e293b;padding:14px;border-radius:10px;border-left:5px solid {gc};margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <b>👀 {d['label']}</b>
    <span style="background:{gc};color:#000;font-size:11px;padding:2px 6px;border-radius:4px;">{d['grade']} {d['score']}점</span>
  </div>
  {f'<div style="font-size:11px;color:#64748b;margin-top:4px;">📝 {memo}</div>' if memo else ''}
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px;">
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">현재가</div>
      <div style="font-weight:bold;">{fmt(curr)}</div>
    </div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">갭</div>
      <div style="color:{'#10b981' if gap_pct>=0 else '#ef4444'};font-weight:bold;">{gap_pct:+.1f}%</div>
    </div>
  </div>
  <div style="background:#0f172a;padding:8px;border-radius:6px;margin-top:6px;font-size:12px;">
    <span style="color:{gc2};font-weight:bold;">{gv}</span>
    <span style="color:#64748b;margin-left:8px;">{gd}</span>
  </div>
  <div style="background:#0f172a;padding:8px;border-radius:6px;margin-top:6px;font-size:12px;">
    신호: <span style="font-weight:bold;">{'✅ S3+S4 유지' if signal_ok else ('⚠️ S3만' if s3_on else '❌ 신호소멸')}</span>
  </div>
  {sup_html}
  <div style="background:#0f172a;padding:10px;border-radius:6px;margin-top:6px;border-left:3px solid {bc};">
    <span style="font-size:13px;font-weight:bold;color:{bc};">{bv}</span>
  </div>
  <div style="font-size:10px;color:#475569;margin-top:6px;">📡 {d['source']}</div>
</div>""", unsafe_allow_html=True)

        col_b, col_d = st.columns([2,1])
        if col_b.button("➕ 매수 확정", key=f"buy_{i}"):
            st.session_state[f"bc_{i}"] = True
        if st.session_state.get(f"bc_{i}"):
            bp = st.number_input("평단가", min_value=0.0, step=0.01, format="%.4f", key=f"bp_{i}")
            bd = st.text_input("매수일자", value=datetime.now().strftime("%Y-%m-%d"), key=f"bd_{i}")
            if st.button("✅ 확정", key=f"bok_{i}") and bp>0:
                p["buy"]=float(bp); p["date"]=bd; p["type"]="hold"
                st.session_state[f"bc_{i}"]=False
                save_portfolio(st.session_state.portfolio)
                st.rerun()
        if col_d.button("🗑️", key=f"dw_{i}"): to_remove=i
        continue

    # ── 보유종목 카드 ──
    # 보유일수
    hold_days=0
    if p.get("date"):
        try: hold_days=(datetime.now()-datetime.strptime(p["date"],"%Y-%m-%d")).days
        except: pass

    fixed_stop  = buy*0.93
    fixed_tgt   = buy*1.08
    rsi_v = d["rsi"]
    trend_broken = not s3_on

    if curr<=fixed_stop: act="🔴 즉시 손절"; ac="#ef4444"; ar=f"평단 -7% 이탈 ({profit:.1f}%)"
    elif trend_broken and profit<-3: act="🔴 손절 고려"; ac="#ef4444"; ar=f"정배열붕괴+손실 {profit:.1f}%"
    elif rsi_v>70 and profit>5: act="🟡 익절 고려"; ac="#f59e0b"; ar=f"RSI과열({rsi_v:.0f})+수익{profit:.1f}%"
    elif curr>=fixed_tgt: act="🟡 익절 고려"; ac="#f59e0b"; ar=f"목표가 도달"
    elif hold_days>=10 and trend_broken: act="🟡 재검토"; ac="#f59e0b"; ar=f"보유{hold_days}일+추세약화"
    elif s3_on and 40<=rsi_v<=60 and -3<=profit<=0: act="🟢 추가매수 검토"; ac="#10b981"; ar=f"정배열+RSI여유({rsi_v:.0f})+눌림"
    elif s3_on and profit>0: act="⚪ 홀딩"; ac="#94a3b8"; ar=f"정배열유지+수익{profit:.1f}%"
    elif hold_days>0 and hold_days<=3: act="⚪ 관망"; ac="#94a3b8"; ar=f"매수{hold_days}일차"
    else: act="⬜ 관망"; ac="#64748b"; ar="신호 대기"

    hold_str = f"{hold_days}일째" if hold_days>0 else ("날짜미입력" if not p.get("date") else "오늘")
    pc = "#10b981" if profit>=0 else "#ef4444"

    # 장외가 (해외) — 장외가격 + 장외기준 수익률
    pp_html=""
    if not is_kr and d.get("prepost",0)>0:
        pp=d["prepost"]; pl=d.get("prepost_label","")
        pp_profit=(pp-buy)/buy*100 if buy>0 and pp>0 else 0
        pp_reg_profit=(curr-buy)/buy*100 if buy>0 and curr>0 else 0
        pp_color="#10b981" if pp_profit>=0 else "#ef4444"
        pp_html=f'''<div style="background:#1a2744;border:1px solid #3b82f6;padding:8px;border-radius:6px;margin:6px 0;">
  <span style="color:#3b82f6;font-size:11px;font-weight:bold;">{pl}</span>
  <span style="color:{pp_color};font-size:13px;font-weight:bold;margin-left:8px;">{uf(pp)}</span>
  <span style="color:{pp_color};font-size:12px;margin-left:6px;">({pp_profit:+.1f}%)</span>
</div>'''

    # 수급 (국내)
    sup_html2=""
    if is_kr and KIS_APP_KEY:
        sup=supply_signal(name)
        if sup.get("ok"):
            sigs2=" / ".join(sup.get("signals",[])[:2])
            sup_html2=f'<div style="background:#0f172a;padding:8px;border-radius:6px;margin:6px 0;font-size:11px;">수급: <span style="color:{sup["color"]};font-weight:bold;">{sup["verdict"]}</span> <span style="color:#64748b;">{sigs2}</span></div>'

    st.markdown(f"""
<div style="background:#1e293b;padding:14px;border-radius:10px;border-left:5px solid {gc};margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <b>📈 {d['label']}</b>
    <span style="background:{gc};color:#000;font-size:11px;padding:2px 6px;border-radius:4px;">{d['grade']} {d['score']}점</span>
  </div>
  {pp_html}
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:10px;">
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">평단가</div>
      <div style="font-weight:bold;font-size:13px;">{fmt(buy)}</div>
    </div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">현재가</div>
      <div style="font-weight:bold;font-size:13px;">{fmt(curr)}</div>
    </div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">수익률</div>
      <div style="color:{pc};font-weight:bold;font-size:13px;">{profit:+.1f}%</div>
    </div>
  </div>
  <div style="background:#0f172a;padding:10px;border-radius:8px;margin-top:6px;border-left:3px solid {ac};">
    <span style="font-weight:bold;color:{ac};">{act}</span>
    <span style="color:#64748b;font-size:11px;margin-left:8px;">{ar}</span>
  </div>
  {sup_html2}
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;margin-top:6px;">
    <div style="background:#0f172a;padding:6px;border-radius:6px;text-align:center;">
      <div style="font-size:9px;color:#94a3b8;">고정손절</div>
      <div style="color:#ef4444;font-size:11px;font-weight:bold;">{fmt(fixed_stop)}</div>
    </div>
    <div style="background:#0f172a;padding:6px;border-radius:6px;text-align:center;">
      <div style="font-size:9px;color:#94a3b8;">목표가</div>
      <div style="color:#3b82f6;font-size:11px;font-weight:bold;">{fmt(fixed_tgt)}</div>
    </div>
    <div style="background:#0f172a;padding:6px;border-radius:6px;text-align:center;">
      <div style="font-size:9px;color:#94a3b8;">RSI</div>
      <div style="font-size:11px;font-weight:bold;">{rsi_v}</div>
    </div>
    <div style="background:#0f172a;padding:6px;border-radius:6px;text-align:center;">
      <div style="font-size:9px;color:#94a3b8;">보유</div>
      <div style="font-size:11px;font-weight:bold;">{hold_str}</div>
    </div>
  </div>
  <div style="display:flex;justify-content:space-between;margin-top:6px;">
    <span style="font-size:10px;color:#475569;">📡 {d['source']}</span>
    <span style="font-size:11px;">{'🟢정배열' if s3_on else '🔴추세약화'}</span>
  </div>
</div>""", unsafe_allow_html=True)

    if st.button("🗑️ 삭제", key=f"del_{i}"): to_remove=i

if to_remove is not None:
    st.session_state.portfolio.pop(to_remove)
    save_portfolio(st.session_state.portfolio)
    st.rerun()

if st.button("🚨 전체 초기화"):
    st.session_state.portfolio=[]
    save_portfolio([])
    st.rerun()

st.divider()

# ============================================================
# 스캔 결과
# ============================================================
with st.spinner("스캔 중..."):
    kr_top, kr_skip = scan_kr()
    us_top, us_skip = scan_us()

with st.sidebar.expander(f"국내 제외 ({len(kr_skip)})"):
    cnt=Counter()
    for s in kr_skip:
        k=s.get("why","기타").split("(")[0].strip().lstrip("❌").strip()
        cnt[k]+=1
    for k,v in cnt.most_common(): st.write(f"- {k}: {v}")

S_LABELS=["S1:BB","S2:거래량","S3:정배열","S4:RSI","S5:캔들","S6:폭발","S7:OBV"]

def render(title, data, currency):
    st.header(title)
    if not data:
        st.info("조건 충족 종목 없음")
        return
    medals=["🥇","🥈","🥉","4️⃣","5️⃣"]
    for i,item in enumerate(data):
        gc={"A+":"#f59e0b","A":"#10b981","B+":"#3b82f6","B":"#94a3b8","C":"#64748b"}.get(item.get("등급","C"),"#64748b")
        is_kr=currency=="KRW"
        def ff(v): return f"${v:,.4f}" if v<1 else f"${v:,.3f}" if v<10 else f"${v:,.2f}"
        fmt2=(lambda v:f"₩{int(v):,}") if is_kr else ff
        flags=item.get("s_flags",[False]*5)
        badges=" ".join(
            f"<span style='background:{'#10b981' if ok else '#1e293b'};color:{'#fff' if ok else '#475569'};font-size:9px;padding:2px 4px;border-radius:3px;'>{lbl}</span>"
            for ok,lbl in zip(flags,S_LABELS))
        sigs_html="".join(f"<li style='font-size:11px;margin:2px 0;'>{s}</li>" for s in item.get("signals",[]))
        if True:  # 1열 세로 나열 (폰 최적화)
            # 점수 표시 조립
            score_parts = f"차트 <b>{item['점수']}점</b>"
            if item.get('수급점수',0) > 0:
                score_parts += f" + 수급 <b style='color:#10b981'>{item['수급점수']}</b>"
            if item.get('섹터점수',0) > 0:
                score_parts += f" + 섹터 <b style='color:#a78bfa'>{item['섹터점수']}</b>"
            if item.get('공시점수',0) != 0:
                score_parts += f" + 공시 <b style='color:#f59e0b'>{item['공시점수']}</b>"
            total_score = item.get('종합점수', item['점수'])
            score_parts += f" = <b style='color:#f59e0b'>{total_score}점</b>"

            # 공시/섹터 뱃지
            extra_badges = ""
            if item.get('공시목록'):
                title_short = item['공시목록'][0]['title'][:18]
                extra_badges += f"<span style='font-size:10px;color:#f59e0b;'>📢 {title_short}</span> "
            if item.get('섹터강세'):
                extra_badges += "<span style='font-size:10px;color:#a78bfa;'>🔥 섹터강세</span>"

            st.markdown(f"""
<div style="background:#1e293b;padding:14px;border-radius:10px;border-left:4px solid {gc};margin-bottom:8px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
    <b>{medals[i]} {item['종목']}</b>{f" <span style='font-size:11px;color:#64748b;font-weight:normal;'>({item['코드']})</span>" if item.get('코드') and item['코드'] != item['종목'] else ""}
    <span style="background:{gc};color:#000;font-size:11px;padding:2px 6px;border-radius:4px;">{item.get('등급','?')}</span>
  </div>
  <div style="margin-bottom:6px;">{badges}</div>
  {f'<div style="margin-bottom:4px;">{extra_badges}</div>' if extra_badges else ''}
  <div style="font-size:12px;line-height:1.8;">
    🎯 {score_parts} | RSI <b>{item['RSI']}</b><br>
    💰 <b>{fmt2(item['현재가'])}</b> <span style="font-size:10px;color:#64748b;">({item.get('source','')})</span><br>
    🟢 {item['매수구간']}<br>
    📈 <span style="color:#3b82f6;">{fmt2(item['목표가'])}</span>
    📉 <span style="color:#ef4444;">{fmt2(item['손절가'])}</span>
  </div>
  <details><summary style="font-size:11px;color:#94a3b8;cursor:pointer;">신호 상세</summary>
    <ul style="padding-left:14px;margin-top:4px;">{sigs_html}</ul>
  </details>
</div>""", unsafe_allow_html=True)

render("🔥 국내 폭등 예측 TOP 5", kr_top, "KRW")
render("🇺🇸 해외 폭등 예측 TOP 5", us_top, "USD")

# ============================================================
# 백테스트 (탭 맨 뒤 — 기존 유지)
# ============================================================
st.divider()
st.header("🔬 백테스트")
st.caption("백테스트 기능은 별도 탭에서 실행하세요 (무거워서 필요할 때만)")

with st.expander("⚙️ 백테스트 실행", expanded=False):
    import importlib.util
    st.info("백테스트는 기존 v8 코드 참고 — 필요시 별도 파일로 분리 권장")
