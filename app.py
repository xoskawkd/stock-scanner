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
    # Railway(os.environ) 우선 → Streamlit Secrets fallback
    import os
    v = os.environ.get(key, "")
    if v: return v
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

# ── 미국장 여부 조기 정의 (사이드바에서 사용) ──
_US_HOLIDAYS_EARLY = {
    2025:{(1,1),(1,20),(2,17),(4,18),(5,26),(6,19),(7,4),(9,1),(11,27),(12,25)},
    2026:{(1,1),(1,19),(2,16),(4,3),(5,25),(6,19),(7,4),(9,7),(11,26),(12,25)},
    2027:{(1,1),(1,18),(2,15),(4,2),(5,31),(6,19),(7,4),(9,6),(11,25),(12,24)},
}
def is_us_open() -> bool:
    try:
        from zoneinfo import ZoneInfo as _ZI
        _n = datetime.now(_ZI("America/New_York"))
        if _n.weekday() >= 5: return False
        if (_n.month, _n.day) in _US_HOLIDAYS_EARLY.get(_n.year, set()): return False
        return _n.replace(hour=9,minute=30,second=0,microsecond=0) <= _n <= _n.replace(hour=16,minute=0,second=0,microsecond=0)
    except: return True

@st.cache_data(ttl=300, show_spinner=False)
def get_etf_supply() -> dict:
    """외국인 ETF(레버/인버스) + 현물 + 선물 수급 — FESI"""
    result = {}
    if not KIS_APP_KEY: return result

    ETFs = {
        "KODEX 레버리지":          ("122630", "lev_kospi"),
        "KODEX 인버스":            ("114800", "inv_kospi"),
        "KODEX 코스닥150레버리지": ("233740", "lev_kosdaq"),
        "KODEX 코스닥150인버스":   ("251340", "inv_kosdaq"),
    }
    for name, (code, key) in ETFs.items():
        try:
            trend = kis_investor_trend(code, 5)
            if trend and len(trend) >= 2:
                today = trend[0].get("외국인", 0)
                prev  = trend[1].get("외국인", 0)
                d3    = sum(t.get("외국인",0) for t in trend[:3])
                result[key] = {
                    "name": name, "code": code,
                    "today": today, "prev": prev, "d3": d3,
                    "flip_buy":  today > 0 and prev <= 0,
                    "flip_sell": today < 0 and prev >= 0,
                    "is_buying": today > 0,
                }
        except: pass

    # 코스피 현물 외국인 수급
    try:
        h = kis_headers("FHKST01010900")
        if h:
            r = requests.get(
                f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-investor",
                params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":"0001",
                        "fid_begin_date":(datetime.now()-timedelta(days=7)).strftime("%Y%m%d"),
                        "fid_end_date":datetime.now().strftime("%Y%m%d"),
                        "fid_period_div_code":"D"},
                headers=h, timeout=5).json()
            rows = r.get("output2", r.get("output", []))
            if rows and len(rows) >= 2:
                st = int(rows[0].get("frgn_ntby_qty", 0) or 0)
                sp = int(rows[1].get("frgn_ntby_qty", 0) or 0)
                d3 = sum(int(row.get("frgn_ntby_qty",0) or 0) for row in rows[:3])
                result["spot_kospi"] = {
                    "name":"코스피 현물", "today":st, "prev":sp, "d3":d3,
                    "is_buying": st > 0,
                    "flip_buy":  st > 0 and sp <= 0,
                    "flip_sell": st < 0 and sp >= 0,
                }
    except: pass

    # 코스피 야간선물 + S&P500 선물
    try:
        _ks = yf.Ticker("^KS11").history(period="2d", interval="1h")
        if _ks is not None and len(_ks) >= 2:
            _kr = (float(_ks["Close"].iloc[-1]) - float(_ks["Close"].iloc[-2])) / float(_ks["Close"].iloc[-2]) * 100
            result["kospi_fut"] = {"name":"코스피 야간","ret":_kr,"bullish":_kr>0.3,"bearish":_kr<-0.3}
        _sp = yf.Ticker("ES=F").history(period="2d", interval="1h")
        if _sp is not None and len(_sp) >= 2:
            _sr = (float(_sp["Close"].iloc[-1]) - float(_sp["Close"].iloc[-2])) / float(_sp["Close"].iloc[-2]) * 100
            result["sp_fut"] = {"name":"S&P500 선물","ret":_sr,"bullish":_sr>0.3,"bearish":_sr<-0.3}
    except: pass

    return result


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
        # KST 기준 날짜 (Railway는 UTC)
        if ZoneInfo:
            today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        else:
            today = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y%m%d")
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
            if n:
                _KIS_NAME_CACHE[code] = n  # 캐시 저장
                return n
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

# ============================================================
# 시장 상태 진단 (Market Regime)
# ============================================================
@st.cache_data(ttl=300, show_spinner=False)  # 5분 캐시 (당일 등락 반영)
def get_kospi_today() -> dict:
    """KIS API로 코스피 당일 등락률 조회"""
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        return {"ok": False, "ret1": 0}
    try:
        h = kis_headers("FHPUP02100000")
        if not h: return {"ok": False, "ret1": 0}
        r = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-index-price",
            params={"fid_cond_mrkt_div_code": "U", "fid_input_iscd": "0001"},  # 코스피
            headers=h, timeout=4).json()
        out = r.get("output", {})
        # 당일 등락률
        ret1 = float(out.get("bstp_nmix_prdy_ctrt", 0) or 0)  # 전일 대비 등락률
        cur  = float(out.get("bstp_nmix_prpr", 0) or 0)       # 현재 지수
        return {"ok": True, "ret1": ret1, "cur": cur}
    except:
        return {"ok": False, "ret1": 0}


@st.cache_data(ttl=300, show_spinner=False)
def get_sector_index_kis(sector_code: str) -> dict:
    """KIS API로 업종 지수 당일 등락 조회"""
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        return {"ok": False, "ret1": 0}
    try:
        h = kis_headers("FHPUP02100000")
        if not h: return {"ok": False, "ret1": 0}
        r = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-index-price",
            params={"fid_cond_mrkt_div_code": "U", "fid_input_iscd": sector_code},
            headers=h, timeout=4).json()
        out = r.get("output", {})
        # 등락률 필드 여러 개 시도
        ret1 = float(out.get("bstp_nmix_prdy_ctrt", 0) or
                     out.get("prdy_ctrt", 0) or 0)
        cur  = float(out.get("bstp_nmix_prpr", 0) or
                     out.get("prpr", 0) or 0)
        if cur > 0:
            return {"ok": True, "ret1": ret1, "cur": cur}
        return {"ok": False, "ret1": 0}
    except:
        return {"ok": False, "ret1": 0}


# KIS 업종 지수 코드
# KIS 업종 지수 코드 (0001~0031, 코스닥 Q001~Q006)
KIS_SECTOR_CODE = {
    # 코스피 업종
    "음식료":    "0010", "음식료품": "0010", "식품": "0010",
    "섬유":      "0011", "의류": "0011",
    "종이":      "0012", "목재": "0012",
    "화학":      "0013", "케미칼": "0013", "에너지": "0013",
    "의약품":    "0014", "제약": "0014", "헬스케어": "0014",
    "비금속":    "0015",
    "철강":      "0016", "철강금속": "0016", "금속": "0016",
    "기계":      "0017",
    "전기전자":  "0018", "전기·전자": "0018", "반도체": "0018",
    "전자": "0018", "IT": "0018", "소프트웨어": "0018", "게임": "0018",
    "의료":      "0019", "정밀": "0019",
    "운수장비":  "0020", "자동차": "0020", "항공": "0020",
    "유통":      "0021", "서비스": "0030", "일반서비스": "0030",
    "미디어": "0030", "엔터": "0030",
    "전기가스":  "0022",
    "건설":      "0023",
    "운수":      "0024", "운수창고": "0024", "운송창고": "0024",
    "운송 창고": "0024", "창고": "0024",
    "물류": "0024", "해운": "0024", "조선": "0024",
    "통신":      "0025",
    "금융":      "0026", "은행": "0027",
    "증권":      "0028",
    "보험":      "0029",
    "제조":      "0031",
    # 코스닥 업종
    "바이오":    "Q006", "바이오헬스": "Q006",
    "오락":      "Q004", "문화": "Q004",
    "IT부품":    "Q003",
}


@st.cache_data(ttl=300, show_spinner=False)
def get_market_regime() -> dict:
    """
    코스피 시장 상태 진단
    당일 등락률 최우선 → 5일 흐름 보조
    """
    # 1. KIS로 당일 코스피 등락률 (최우선)
    today_data = get_kospi_today()
    ret1 = today_data.get("ret1", 0) if today_data.get("ok") else 0

    # 2. fdr로 5일 흐름
    ret5 = 0; ma5 = 0; ma20 = 0; down_days = 0; dd_from_hi = 0
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader("KS11", start=(datetime.now()-timedelta(days=120)).strftime("%Y-%m-%d"))
        if df is not None and len(df) >= 10:
            df.columns = [c.lower() for c in df.columns]
            cl = df["close"].astype(float)
            cur = float(cl.iloc[-1])
            ma5  = float(cl.rolling(5).mean().iloc[-1])
            ma20 = float(cl.rolling(20).mean().iloc[-1])
            ret5 = (cur-float(cl.iloc[-6]))/float(cl.iloc[-6])*100 if len(cl)>=6 else 0
            hi52 = float(cl.rolling(252).max().iloc[-1]) if len(cl)>=252 else float(cl.max())
            dd_from_hi = (cur-hi52)/hi52*100
            for i in range(1, 6):
                if float(cl.iloc[-i]) < float(cl.iloc[-i-1]): down_days+=1
                else: break
    except: pass

    # ── 당일 등락률 최우선 판단 ──
    if ret1 <= -3:
        regime="🔴 당일 급락"; score=0; color="#ef4444"
        desc=f"오늘 {ret1:+.1f}% — 매수 절대 금지"
    elif ret1 <= -1.5:
        regime="🟠 당일 약세"; score=0; color="#f97316"
        desc=f"오늘 {ret1:+.1f}% — 매수 자제"
    elif ret1 >= 1.5:
        regime="🟢 당일 강세"; score=3; color="#10b981"
        desc=f"오늘 {ret1:+.1f}%"
    elif ret1 >= 0:
        regime="🟡 당일 보합"; score=2; color="#f59e0b"
        desc=f"오늘 {ret1:+.1f}%"
    else:
        regime="🟡 당일 소폭하락"; score=1; color="#f59e0b"
        desc=f"오늘 {ret1:+.1f}%"

    # 5일 흐름으로 보정 (당일 판단과 같은 방향이면 강화)
    if ret5 < -3 and score > 0: score = max(0, score-1)
    if ret5 > 3 and score < 3:  score = min(3, score+1)
    if down_days >= 3 and score > 0: score = max(0, score-1)

    # 극공포 (당일 -5% 이상)
    if ret1 <= -5:
        regime="🔥 극공포"; score=0; color="#ef4444"
        desc=f"오늘 {ret1:+.1f}% 급락 — 역발상 기회 탐색"

    return {
        "ok": True, "regime": regime, "score": score,
        "color": color, "desc": desc,
        "ret1": round(ret1,2), "ret5": round(ret5,2),
        "ma_bull": ma5 > ma20 if ma5>0 and ma20>0 else True,
        "down_days": down_days, "dd_from_hi": round(dd_from_hi,1),
    }


# 종목코드 → 섹터명 매핑 (KIS API 기반, 캐시)
_STOCK_SECTOR_CACHE = {}  # {code: sector_name}

# 섹터명 → KRX 지수 코드 매핑
# fdr fallback용 섹터 지수 코드
SECTOR_INDEX_MAP = {
    "전기전자": "KS11018", "전기·전자": "KS11018", "반도체": "KS11018",
    "화학":     "KS11013", "음식료":   "KS11010",
    "철강금속": "KS11016", "기계":     "KS11017",
    "운수장비": "KS11020", "건설":     "KS11023",
    "의약품":   "KS11014", "바이오":   "KS11014",
    "통신":     "KS11025", "금융":     "KS11026",
    "증권":     "KS11028", "보험":     "KS11029",
    "유통":     "KS11021", "서비스":   "KS11030",
    "일반서비스":"KS11030",
}

@st.cache_data(ttl=86400, show_spinner=False)
def get_stock_sector_name(code: str) -> str:
    """종목 섹터명 조회 — KIS inquire-price → 하드코딩"""
    if code in _STOCK_SECTOR_CACHE:
        return _STOCK_SECTOR_CACHE[code]

    # 1. KIS inquire-price 응답에서 업종 추출 (가장 안정적)
    if KIS_APP_KEY:
        try:
            h = kis_headers("FHKST01010100")
            if h:
                r = requests.get(
                    f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                    params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":code},
                    headers=h, timeout=3).json()
                out = r.get("output",{})
                sec = (out.get("bstp_kor_isnm","") or
                       out.get("idx_bztp_lcls_cd_name","") or "")
                if sec:
                    _STOCK_SECTOR_CACHE[code] = sec.strip()
                    return sec.strip()
        except: pass

    # 2. 하드코딩 (주요 종목 fallback)
    SECTOR_HARDCODE = {
        "005930":"전기전자","000660":"전기전자","353200":"전기전자",
        "005380":"운수장비","000270":"운수장비","012330":"운수장비",
        "207940":"의약품","068270":"의약품","196170":"의약품",
        "373220":"전기전자","006400":"전기전자","247540":"화학",
        "086520":"화학","010130":"철강금속","329180":"조선",
        "042660":"조선","105560":"금융","055550":"금융",
        "086790":"금융","138040":"금융","316140":"금융",
        "035420":"통신","035720":"통신","017670":"통신",
        "051910":"화학","096770":"화학","011170":"화학",
        "230360":"일반서비스","064290":"기계","211050":"전기전자",
    }
    sec = SECTOR_HARDCODE.get(code,"")
    if sec:
        _STOCK_SECTOR_CACHE[code] = sec
    return sec


@st.cache_data(ttl=300, show_spinner=False)
def get_sector_regime(sector_name: str) -> dict:
    """섹터명으로 섹터 지수 당일 등락 조회 — KIS API 우선"""
    # KIS 업종 코드 매핑
    kis_code = ""
    for key, code in KIS_SECTOR_CODE.items():
        if key in sector_name:
            kis_code = code
            break

    # 1. KIS 업종 지수 (당일 등락률) — 여러 코드 시도
    if kis_code and KIS_APP_KEY:
        # 코드 변형 시도 (0030 → 030 → 30 등)
        codes_to_try = [kis_code, kis_code.lstrip("0") or kis_code,
                        f"{int(kis_code):04d}" if kis_code.isdigit() else kis_code]
        codes_to_try = list(dict.fromkeys(codes_to_try))  # 중복 제거
        for try_code in codes_to_try:
            data = get_sector_index_kis(try_code)
            if data.get("ok"):
                ret1 = data.get("ret1", 0)
                if ret1 >= 2:   status="🟢 강세"; score=2
                elif ret1 >= 0: status="🟡 보합"; score=1
                elif ret1 >= -2: status="🟠 약세"; score=0
                else:           status="🔴 급락"; score=-1
                return {"ok":True,"status":status,"score":score,
                        "ret1":round(ret1,2),"sector_name":sector_name}

    # 2. fdr fallback (5일 기준)
    fdr_code = ""
    for key, code in SECTOR_INDEX_MAP.items():
        if key in sector_name:
            fdr_code = code
            break
    if fdr_code:
        try:
            import FinanceDataReader as fdr
            df = fdr.DataReader(fdr_code,
                               start=(datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d"))
            if df is not None and len(df) >= 5:
                df.columns = [c.lower() for c in df.columns]
                cl = df["close"].astype(float)
                ret5 = (float(cl.iloc[-1])-float(cl.iloc[-6]))/float(cl.iloc[-6])*100 if len(cl)>=6 else 0
                if ret5 >= 3:   status="🟢 강세"; score=2
                elif ret5 >= 0: status="🟡 보합"; score=1
                elif ret5 >= -3: status="🟠 약세"; score=0
                else:           status="🔴 급락"; score=-1
                return {"ok":True,"status":status,"score":score,
                        "ret5":round(ret5,2),"sector_name":sector_name}
        except: pass

    return {"ok":False,"status":"조회실패","score":1,"ret5":0}


def get_stock_full_regime(code: str) -> dict:
    """
    종목의 시장 + 섹터 종합 상태
    반환: {market, sector, combined_score, summary}
    """
    mkt    = get_market_regime()
    sec_nm = get_stock_sector_name(code)
    sec = get_sector_regime(sec_nm) if sec_nm else {"ok":False,"score":1,"status":"섹터확인불가","ret5":0}

    mkt_score = mkt.get("score", 2)
    # 섹터 조회 실패 시 중립(1)로 고정 — 강세/약세 판단 금지
    sec_ok    = sec.get("ok", False)
    sec_score = sec.get("score", 1) if sec_ok else 1
    combined  = mkt_score + sec_score  # 0~5

    # 종합 판단 — 섹터 확인 불가 시 시장만으로 판단
    if not sec_ok:
        if mkt_score >= 3:
            summary = "🟡 시장 강세 (섹터확인불가)"
            color   = "#f59e0b"; buy_adj = 1
        elif mkt_score <= 1:
            summary = "🟠 시장 약세 (섹터확인불가)"
            color   = "#f97316"; buy_adj = -1
        else:
            summary = "⬜ 시장 중립 (섹터확인불가)"
            color   = "#64748b"; buy_adj = 0
    elif combined >= 4:
        summary = "🟢 시장+섹터 강세"
        color   = "#10b981"; buy_adj = 2
    elif combined >= 3:
        summary = "🟡 시장 or 섹터 중립"
        color   = "#f59e0b"; buy_adj = 0
    elif combined >= 2:
        summary = "🟠 주의"
        color   = "#f97316"; buy_adj = -1
    else:
        summary = "🔴 시장+섹터 약세"
        color   = "#ef4444"; buy_adj = -3

    return {
        "market":         mkt,
        "sector":         sec,
        "sector_name":    sec_nm,
        "combined_score": combined,
        "summary":        summary,
        "color":          color,
        "buy_adj":        buy_adj,
        "mkt_regime":     mkt.get("regime",""),
        "sec_status":     sec.get("status",""),
    }


def get_dynamic_pass_score(market: dict) -> int:
    """
    시장 상태에 따라 pass score 동적 조정
    상승장 → 완화 (더 많은 종목 추천)
    하락장 → 강화 (고품질만 추천)
    """
    base = 12
    regime_score = market.get("score", 2)
    if regime_score == 3:   return max(base - 4, 8)   # 상승장: 8점
    elif regime_score == 2: return base                # 중립: 12점
    elif regime_score == 1: return base + 6            # 조정장: 18점 (기존 20→18)
    else:                   return base + 11           # 하락장: 23점 (기존 27→23)


@st.cache_data(ttl=300, show_spinner=False)
def get_tomorrow_outlook() -> dict:
    """
    내일 매수 환경 종합 판단
    나스닥 등락 + 코스피200 선물 + 환율
    """
    result = {
        "nasdaq_ret": 0.0, "nasdaq_ok": False,
        "futures_ret": 0.0, "futures_ok": False,
        "usd_krw": 0.0, "fx_ok": False,
        "score": 0, "verdict": "알수없음", "color": "#64748b",
    }

    # 1. 나스닥 등락 (yfinance)
    try:
        import yfinance as yf
        nq = yf.Ticker("^IXIC").history(period="2d")
        if len(nq) >= 2:
            ret = (float(nq["Close"].iloc[-1]) - float(nq["Close"].iloc[-2])) / float(nq["Close"].iloc[-2]) * 100
            result["nasdaq_ret"] = round(ret, 2)
            result["nasdaq_ok"] = ret > 0
    except: pass

    # 2. 코스피200 선물 (yfinance — KS200F)
    try:
        fut = yf.Ticker("ES=F").history(period="1d", interval="5m")  # S&P500 선물로 대체
        if not fut.empty:
            p_now  = float(fut["Close"].iloc[-1])
            p_prev = float(fut["Close"].iloc[0])
            ret_f  = (p_now - p_prev) / p_prev * 100
            result["futures_ret"] = round(ret_f, 2)
            result["futures_ok"]  = ret_f > 0
    except: pass

    # 3. 원달러 환율
    try:
        fx = yf.Ticker("KRW=X").history(period="2d")
        if len(fx) >= 2:
            usd_krw = float(fx["Close"].iloc[-1])
            result["usd_krw"] = round(usd_krw, 1)
            # 환율 하락(원화강세) = 외국인 매수 우호적
            fx_ret = (usd_krw - float(fx["Close"].iloc[-2])) / float(fx["Close"].iloc[-2]) * 100
            result["fx_ok"] = fx_ret < 0  # 환율 하락 = 긍정
            result["fx_ret"] = round(fx_ret, 2)
    except: pass

    # 종합 점수
    score = 0
    nq = result["nasdaq_ret"]
    fx = result.get("fx_ret", 0)

    if nq >= 2:    score += 3
    elif nq >= 1:  score += 2
    elif nq >= 0:  score += 1
    elif nq >= -1: score -= 1
    else:          score -= 3

    if result["futures_ok"]: score += 1
    else:                    score -= 1

    if result["fx_ok"]:  score += 1
    else:                score -= 1

    result["score"] = score

    if score >= 4:
        result["verdict"] = "✅ 내일 매수 적합"
        result["color"]   = "#10b981"
    elif score >= 2:
        result["verdict"] = "🟡 내일 조건부 매수"
        result["color"]   = "#f59e0b"
    elif score >= 0:
        result["verdict"] = "🟠 내일 신중하게"
        result["color"]   = "#f97316"
    else:
        result["verdict"] = "🔴 내일 매수 보류"
        result["color"]   = "#ef4444"

    return result




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
def get_krx_caution_stocks() -> set:
    """KRX 투자주의/경고/위험 종목 코드 집합"""
    codes = set()
    if not KRX_API_KEY: return codes
    try:
        # KRX 투자주의/경고/위험 종목 (실제 API 경로)
        # KST 기준 날짜 (Railway는 UTC)
        if ZoneInfo:
            today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        else:
            today = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y%m%d")
        for url_path in [
            "sto/invst_caution_isu",   # 투자주의
            "sto/invst_wrnng_isu",     # 투자경고
            "sto/invst_risk_isu",      # 투자위험
        ]:
            try:
                r = requests.get(
                    f"http://data-dbg.krx.co.kr/svc/apis/{url_path}",
                    params={"basDd": today},
                    headers={"AUTH_KEY": KRX_API_KEY}, timeout=5).json()
                for row in r.get("OutBlock_1", []):
                    code = (row.get("ISU_SRT_CD","") or
                            row.get("SHRT_ISU_CD","") or "")
                    if code and len(code)==6: codes.add(code)
            except: pass
    except: pass
    return codes


def get_caution_label(code: str, caution_set: set) -> str:
    """투자주의 라벨 반환"""
    if code in caution_set:
        return "⚠️ 투자주의"
    return ""


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
    disclosures = get_dart_disclosures(code, days=5)
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
         "rsi":50.0,"current":0.0,"s1":False,"s2":False,"s3":False,"s4":False,"s5":False,"s6":False,"s7":False,
         "atr_pct":0.0,"s3_streak":0,"s4_streak":0}
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
        # 거래정지/상장폐지 감지
        recent_vol = _sf(vo.iloc[-5:].sum())
        recent_days_zero = sum(1 for v in vo.iloc[-5:] if _sf(v) == 0)
        if recent_vol == 0:
            OUT["signals"].append("❌ 거래정지 의심 (5일 거래량 0)"); rejected=True
        elif recent_days_zero >= 2:
            OUT["signals"].append(f"❌ 간헐적 거래정지 의심 ({recent_days_zero}일 거래량 0)"); rejected=True
        elif avg_vol<th["min_vol"]:
            OUT["signals"].append("❌ 유동성 부족"); rejected=True

        # 거래대금 필터 (5억원 이상, 국내만 적용)
        if market == "KR" and not rejected:
            daily_amount = _sf((cl * vo).rolling(20).mean().iloc[-1])
            if daily_amount < 500_000_000:
                OUT["signals"].append(f"❌ 거래대금 부족 ({daily_amount/1e8:.1f}억)"); rejected=True
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
            if bw<=pct10: s1=True;setup+=1;strong+=1;score+=8; OUT["signals"].append(f"✅ [S1] BB강수축 (하위10%)")
            elif bw<=pct20: s1=True;setup+=1;score+=4; OUT["signals"].append(f"🔶 [S1] BB수축 (하위20%)")
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
        near=ma20>0 and abs(cur-ma20)/ma20<=0.04; s3=False
        if aligned and near: s3=True;setup+=1;strong+=1;score+=W["s3_strong"]
        elif mid_up and near: s3=True;setup+=1;score+=W["s3_weak"]

        # S3 연속 유지일수 계산 (노이즈성 신호 vs 안정된 신호 구분)
        s3_streak = 0
        try:
            _ma5s  = cl.rolling(5).mean()
            _ma20s = cl.rolling(20).mean()
            _ma60s = cl.rolling(60).mean()
            for k in range(1, 11):  # 최대 10일 전까지 확인
                if len(cl) <= k: break
                _c   = _sf(cl.iloc[-1-k])
                _m5  = _sf(_ma5s.iloc[-1-k]); _m20 = _sf(_ma20s.iloc[-1-k]); _m60 = _sf(_ma60s.iloc[-1-k])
                _al  = _m5>0 and _m20>0 and _m60>0 and _m5>_m20>_m60
                _mu  = _m20>0 and _m60>0 and _m20>_m60
                _nr  = _m20>0 and abs(_c-_m20)/_m20<=0.04
                if (_al and _nr) or (_mu and _nr): s3_streak += 1
                else: break
        except: pass
        OUT["s3_streak"] = s3_streak

        if s3:
            if s3_streak >= 2:
                score += 3; OUT["signals"].append(f"✅ [S3] 정배열+눌림목 ★ ({s3_streak+1}일째 유지)")
            else:
                OUT["signals"].append("✅ [S3] 정배열+눌림목 ★ (오늘 첫 발생)")
        else:
            OUT["signals"].append(f"⬜ [S3] 눌림목없음 (이격 {abs(cur-ma20)/ma20*100:.1f}%)" if ma20>0 else "⬜ [S3] MA20없음")
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
                if s4:
                    trigger+=1;score+=W["s4"]
                    # S4는 다이버전스 직후(저점 형성 후 며칠 이내)가 골든타임
                    # 저점에서 너무 멀어졌으면(이미 많이 반등) 신뢰도 낮음
                    days_since_low = len(pw) - 1 - p2_idx
                    if days_since_low <= 3:
                        OUT["signals"].append(f"✅ [S4] RSI다이버전스 (가격↓{p1:.0f}→{p2:.0f} RSI↑{r1:.0f}→{r2:.0f}) 신선함")
                    else:
                        score -= 3  # 저점 형성 후 시간 지남 → 신뢰도 감점
                        OUT["signals"].append(f"🔶 [S4] RSI다이버전스 (저점 {days_since_low}일 전, 신뢰도↓)")
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
                    if s5: OUT["signals"].append(f"✅ [S5] {'망치형' if hammer else '양봉전환'}")  # 점수 제외 (노이즈)
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
        atr_pct = 0.0  # ATR 비율 (변동성 지표) — 보유종목 판단에도 재사용
        try:
            tr=pd.concat([hi-lo,(hi-cl.shift()).abs(),(lo-cl.shift()).abs()],axis=1).max(axis=1)
            atr=_sf(tr.rolling(14).mean().iloc[-1])
            if atr>0 and cur>0:
                atr_pct = atr/cur*100  # ATR이 현재가 대비 몇 %인지
                std20=_sf(_std20_s.iloc[-1])
                bb_top=ma20+std20*2 if ma20>0 and std20>0 else 0
                t_atr=cur+atr*2; t_bb=bb_top if bb_top>cur else cur+atr*2
                _tgt=min(t_atr,t_bb)
                _tgt=max(_tgt,cur*1.05); _tgt=min(_tgt,cur*1.15)

                # ATR 기반 손절 — 변동성 따라 -4%~-10% 범위에서 결정 (고정 -7% 캡 제거)
                _stp=cur-atr*2.0
                _stp=max(_stp,cur*0.92)   # 손절 하한 -8% (모순 방지용 캡)
                _stp=min(_stp,cur*0.96)   # 손절 상한 -4%
                _blo=max(cur*0.97,cur-atr*0.5); _bhi=min(cur*1.02,cur+atr*0.3)
        except: pass
        OUT["atr_pct"]=round(atr_pct,2)
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
@st.cache_data(ttl=86400, show_spinner=False)
def get_sector_leaders() -> list:
    """
    섹터별 시총 1위 종목 자동 추출
    pykrx → krx_listing fallback
    반환: [(code, name, sector), ...]
    """
    try:
        from pykrx import stock as pk
        # KST 기준 날짜 (Railway는 UTC)
        if ZoneInfo:
            today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        else:
            today = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y%m%d")

        # 전체 종목 시총 데이터
        df_cap = pk.get_market_cap(today, market="KOSPI")
        df_cap.index.name = "Code"
        df_cap = df_cap.reset_index()

        # 업종별 시총 상위 종목 (pykrx 업종 분류 사용)
        sectors = {}
        try:
            # 업종별 종목 한번에 가져오기
            df_sector = pk.get_market_sector_classifications(today, market="KOSPI")
            if not df_sector.empty:
                for _, row in df_sector.iterrows():
                    sec_name = row.get("업종명","기타")
                    code = row.get("티커","") or str(row.name)
                    sectors.setdefault(sec_name,[]).append(str(code))
        except: pass  # pykrx 로그인 필요 시 무시

        # 섹터별 시총 1위
        leaders = []
        for sec_name, codes in sectors.items():
            best_code = None; best_cap = 0
            for code in codes:
                row = df_cap[df_cap["Code"]==code]
                if not row.empty:
                    cap = float(row["시가총액"].values[0] if "시가총액" in row.columns else 0)
                    if cap > best_cap:
                        best_cap = cap
                        best_code = code
            if best_code:
                try:
                    name = pk.get_market_ticker_name(best_code)
                except: name = best_code
                leaders.append((best_code, name, sec_name))

        if leaders:
            return leaders
    except: pass

    # fallback: krx_listing 기반 섹터 추정
    try:
        listing = krx_listing()
        if "Market" in listing.columns:
            kospi = listing[listing["Market"].str.contains("KOSPI", na=False)]
        else:
            kospi = listing

        # 종목명 기반 업종 추정 (간략)
        sector_keywords = {
            "반도체":   ["전자","하이닉스","반도체","마이크론"],
            "바이오":   ["바이오","제약","생명","헬스"],
            "자동차":   ["자동차","현대차","기아","모비스"],
            "2차전지":  ["에너지솔루션","SDI","에코프로","배터리"],
            "조선":     ["중공업","조선","해운"],
            "금융":     ["금융","은행","증권","보험","지주"],
            "화학":     ["화학","케미칼","솔루션"],
            "철강":     ["철강","스틸","포스코"],
            "건설":     ["건설","건영","엔지니어링"],
            "IT서비스": ["NAVER","카카오","네이버","크래프톤"],
        }

        used_codes = set()
        leaders = []
        for sec_name, keywords in sector_keywords.items():
            best = None; best_cap = 0
            for _, row in kospi.iterrows():
                code = row.get("Code","")
                name = str(row.get("Name",""))
                cap  = float(row.get("Marcap", 0))
                if code in used_codes: continue
                if any(kw in name for kw in keywords):
                    if cap > best_cap:
                        best_cap = cap
                        best = (code, name, sec_name)
            if best:
                leaders.append(best)
                used_codes.add(best[0])

        return leaders if leaders else []
    except: return []


@st.cache_data(ttl=300, show_spinner=False)
def scan_kr_sector() -> tuple:
    """섹터별 대장주 스캔 — 각 섹터 1위 종목에 동일 로직 적용"""
    leaders = get_sector_leaders()
    if not leaders:
        return [], []

    listing  = krx_listing()
    market_map = dict(zip(listing["Code"], listing.get("Market", pd.Series())))                  if "Market" in listing.columns else {}

    if KIS_APP_KEY and KIS_APP_SECRET:
        kis_token()  # 토큰 미리 발급

    def _fetch_leader(item):
        code, name, sector = item
        df = ohlcv_kr(code)
        if df is None: return {"_skip": True, "why": "데이터없음", "sector": sector}
        r = quant_predict(df, "KR")
        p, src = kr_price(code, market_map)
        if p <= 0: p = r["current"]
        if p <= 0: return {"_skip": True, "why": "가격없음", "sector": sector}

        # pass 여부와 관계없이 섹터 대장은 항상 포함
        display_name = _KIS_NAME_CACHE.get(code) or name
        bmin = int(r["buy_min"]) if r["buy_min"]>p*0.90 and r["buy_min"]<p else int(p*0.97)
        bmax = int(r["buy_max"]) if r["buy_max"]>p and r["buy_max"]<p*1.05 else int(p*1.02)
        tgt  = int(r["target"]) if r["target"]>p*1.03 and r["target"]<=p*1.15 else int(p*1.08)
        stp  = int(r["stop"])   if r["stop"]>p*0.85   and r["stop"]<p*0.98   else int(p*0.93)

        return {
            "_skip":   False,
            "종목":    f"{display_name} ({code})",
            "코드":    code,
            "섹터":    sector,
            "등급":    r["grade"],
            "점수":    r["score"],
            "pass":    r["pass"],
            "현재가":  int(p),
            "RSI":     round(r["rsi"], 1),
            "매수구간":f"₩{bmin:,}~₩{bmax:,}",
            "목표가":  tgt,
            "손절가":  stp,
            "signals": r["signals"],
            "source":  src,
            "s_flags": [r["s1"],r["s2"],r["s3"],r["s4"],r["s5"],
                        r.get("s6",False),r.get("s7",False)],
            "수급점수": 0, "섹터점수": 0, "공시점수": 0,
            "공시목록": [], "종합점수": r["score"], "섹터강세": False,
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        raw = list(ex.map(_fetch_leader, leaders))

    passed = [x for x in raw if not x.get("_skip") and isinstance(x, dict)]
    skips  = [x for x in raw if x.get("_skip") and isinstance(x, dict)]

    # KIS 가격 재조회 — 상위 20개만 순차 처리 (rate limit 방지)
    if KIS_APP_KEY and KIS_APP_SECRET:
        import time as _time
        for item in passed[:20]:
            try:
                p_kis, src_kis = kis_price(item["코드"])
                if p_kis > 0:
                    item["현재가"] = int(p_kis)
                    item["source"] = src_kis
                cached = _KIS_NAME_CACHE.get(item["코드"])
                if cached: item["종목"] = cached
                _time.sleep(0.05)
            except: pass

    top5 = sorted(passed, key=lambda x: x.get("종합점수", x.get("점수", 0)), reverse=True)[:5]
    return top5, skips


@st.cache_data(ttl=3600, show_spinner=False)
def scan_contrarian() -> tuple:
    """
    역발상 매수 스캐너
    과매도 + 거래량 급증 + 수급 개선 종목 탐색
    보유기간 2~5일 단기 기술적 반등 전략
    """
    listing  = krx_listing()
    caution_set = get_krx_caution_stocks()
    kospi  = listing[listing["Market"].str.contains("KOSPI",na=False)] if "Market" in listing.columns else listing
    kosdaq = listing[listing["Market"].str.contains("KOSDAQ",na=False)] if "Market" in listing.columns else pd.DataFrame()
    kp = kospi[kospi["Marcap"]>1e11].nlargest(400,"Marcap")
    kq = kosdaq[kosdaq["Marcap"]>3e10].nlargest(200,"Marcap") if not kosdaq.empty else pd.DataFrame()
    targets = pd.concat([kp,kq]).drop_duplicates("Code")
    codes   = list(zip(targets["Code"],targets["Name"]))
    market_map = dict(zip(listing["Code"],listing.get("Market",pd.Series()))) if "Market" in listing.columns else {}

    # 코스피 20일 수익률 (상대강도 계산용)
    kospi_ret20 = 0.0
    try:
        mkt = get_market_regime()
        # 코스피 20일 수익률 직접 계산
        import yfinance as yf
        ks = yf.Ticker("^KS11").history(period="25d")
        if len(ks) >= 20:
            kospi_ret20 = (float(ks["Close"].iloc[-1]) - float(ks["Close"].iloc[-20])) / float(ks["Close"].iloc[-20]) * 100
        else:
            kospi_ret20 = mkt.get("ret5", 0) * 3  # fallback
    except:
        try:
            kospi_ret20 = get_market_regime().get("ret5", 0) * 3
        except: pass

    def _fetch_ct(item):
        code, name = item
        if code in caution_set: return {"_skip":True,"why":"투자주의/경고"}
        df = ohlcv_kr(code)
        if df is None or len(df)<25: return {"_skip":True,"why":"데이터부족"}
        df = df.copy(); df.columns=[c.lower() for c in df.columns]
        cl = df["close"].astype(float)
        vo = df["volume"].astype(float)

        cur = float(cl.iloc[-1])
        if cur <= 0: return {"_skip":True,"why":"가격없음"}

        # 평균 거래량
        avg_vol = float(vo.rolling(20).mean().iloc[-1])
        if avg_vol < 50000: return {"_skip":True,"why":"유동성부족"}
        # 거래정지 감지
        zero_days = sum(1 for v in vo.iloc[-5:] if float(v) == 0)
        if zero_days >= 2: return {"_skip":True,"why":f"거래정지의심({zero_days}일 거래량0)"}

        # 거래대금 조건 (5억 이상)
        if "close" in df.columns:
            daily_amount = float((cl * vo).rolling(20).mean().iloc[-1])
            if daily_amount < 500_000_000:
                return {"_skip":True,"why":f"거래대금부족({daily_amount/1e8:.1f}억)"}

        # 데이터 기준: 장 마감 후면 오늘 종가, 장 중이면 오늘 봉 포함
        # (역발상은 급락 당일 포착이 핵심 — 오늘 봉 제외하면 당일 급락 못 잡음)
        ref_cl = cl
        ref_vo = vo

        # 거래량 조건 — 오늘 포함 최근 3일 중 최대 (급락 당일 거래량 폭발 포착)
        avg_vol_ref = float(ref_vo.rolling(20).mean().iloc[-1])
        vol_max3 = float(ref_vo.iloc[-3:].max())
        vol_ratio = vol_max3 / avg_vol_ref if avg_vol_ref > 0 else 0
        if vol_ratio < 1.2: return {"_skip":True,"why":f"거래량부족({vol_ratio:.1f}배)"}

        # RSI (전일 기준)
        delta = ref_cl.diff()
        gain  = delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
        rsi_s = 100 - 100/(1+gain/loss.replace(0,np.nan))
        rsi   = float(rsi_s.iloc[-1])
        if rsi > 35: return {"_skip":True,"why":f"RSI미충족({rsi:.0f})"}

        cur_ref = float(ref_cl.iloc[-1])

        # 20일 수익률
        ret20 = (cur_ref - float(ref_cl.iloc[-21]))/float(ref_cl.iloc[-21])*100 if len(ref_cl)>=21 else 0
        # 20일 낙폭 -15% 이상 OR 오늘 단일 낙폭 -5% 이상 (급락 당일 포착)
        today_ret = (float(cl.iloc[-1]) - float(cl.iloc[-2])) / float(cl.iloc[-2]) * 100 if len(cl) >= 2 else 0
        if ret20 > -15 and today_ret > -5:
            return {"_skip":True,"why":f"낙폭부족(20일:{ret20:.1f}% 오늘:{today_ret:.1f}%)"}
        if ret20 < -35: return {"_skip":True,"why":f"급락악재제외({ret20:.1f}%)"}

        # 5일 수익률 (급락 제외)
        ret5 = (cur_ref - float(ref_cl.iloc[-6]))/float(ref_cl.iloc[-6])*100 if len(ref_cl)>=6 else 0
        if ret5 <= -25: return {"_skip":True,"why":f"급락제외({ret5:.1f}%)"}

        # ── 가산점수 ──
        score = 0; signals = []

        # 수급 점수
        if KIS_APP_KEY:
            try:
                trend = kis_investor_trend(code, 5)
                if trend:
                    fore_today = trend[0].get("외국인",0)
                    inst_today = trend[0].get("기관",0)
                    pension    = trend[0].get("연기금",0)
                    fore_prev  = trend[1].get("외국인",0) if len(trend)>1 else 0
                    inst_prev  = trend[1].get("기관",0)  if len(trend)>1 else 0
                    if fore_today > 0 and fore_prev <= 0:
                        score += 7; signals.append("✅ 외국인 순매수 전환 +7")
                    elif fore_today > 0:
                        score += 4; signals.append("✅ 외국인 순매수 +4")
                    if inst_today > 0 and inst_prev <= 0:
                        score += 6; signals.append("✅ 기관 순매수 전환 +6")
                    elif inst_today > 0:
                        score += 3; signals.append("✅ 기관 순매수 +3")
                    if pension > 0:
                        score += 4; signals.append("✅ 연기금 순매수 +4")
            except: pass

        # OBV 3일 연속 상승
        try:
            obv = (np.sign(cl.diff())*vo).fillna(0).cumsum()
            obv_rising3 = (float(obv.iloc[-1]) > float(obv.iloc[-2]) > float(obv.iloc[-3]))
            if obv_rising3:
                score += 6; signals.append("✅ OBV 3일 연속 상승 +6")
            elif float(obv.iloc[-1]) > float(obv.iloc[-6]):
                score += 3; signals.append("✅ OBV 반등 +3")
        except: pass

        # 52주 신저가 구간
        try:
            lo52 = float(cl.rolling(252).min().iloc[-1]) if len(cl)>=252 else float(cl.min())
            if cur <= lo52 * 1.05:
                score += 5; signals.append("✅ 52주 신저가 ±5% +5")
        except: pass

        # 전일 양봉 + 거래량 증가
        try:
            if "open" in df.columns:
                op = df["open"].astype(float)
                # 전일 기준 (ref_cl)
                ref_op = op.iloc[:-1] if len(op)>=2 else op
                is_bull = float(ref_cl.iloc[-1]) > float(ref_op.iloc[-1])
                vol_inc = float(ref_vo.iloc[-1]) > float(ref_vo.iloc[-2]) if len(ref_vo)>=2 else False
                if is_bull and vol_inc:
                    score += 5; signals.append("✅ 양봉+거래량증가 +5")
                elif is_bull:
                    score += 3; signals.append("✅ 양봉 마감 +3")
        except: pass

        # 코스피 대비 상대강도
        try:
            rel_str = ret20 - kospi_ret20
            if rel_str > 0:
                score += 4; signals.append(f"✅ 코스피 대비 강함 +4 ({rel_str:+.1f}%)")
        except: pass

        # DART 공시 제외 조건
        if DART_API_KEY:
            try:
                disc = get_dart_disclosures(code, days=3)
                bad_kw = ["유상증자","전환사채","감자","거래정지","상장폐지","BW"]
                for d in disc:
                    if any(k in d["title"] for k in bad_kw):
                        return {"_skip":True,"why":f"악재공시:{d['title'][:10]}"}
            except: pass

        # pass 기준 — 시장 상태 연동
        _mkt_r = get_market_regime()
        _r1 = _mkt_r.get("ret1", 0)
        if _r1 <= -5:   ct_pass = 8    # 극공포 (-5%↓): 완화
        elif _r1 <= -3: ct_pass = 10   # 급락장 (-3%↓): 기본
        elif _r1 <= -1: ct_pass = 12   # 약세장 (-1%↓): 중간
        else:           ct_pass = 15   # 중립 이상: 엄격
        if score < ct_pass: return {"_skip":True,"why":f"점수부족({score}/{ct_pass}점)"}

        # 가격 조회
        p, src = kr_price(code, market_map)
        if p <= 0: p = cur

        # 등급
        if score >= 25: grade="A+"
        elif score >= 18: grade="A"
        elif score >= 13: grade="B+"
        else: grade="B"

        tgt = int(p * 1.10)   # 목표 +10%
        stp = int(p * 0.95)   # 손절 -5%

        return {
            "_skip":   False,
            "종목":    _KIS_NAME_CACHE.get(code) or name,
            "코드":    code,
            "등급":    grade,
            "점수":    score,
            "현재가":  int(p),
            "RSI":     round(rsi,1),
            "낙폭20일": round(ret20,1),
            "낙폭5일":  round(ret5,1),
            "거래량배율": round(vol_ratio,1),
            "목표가":  tgt,
            "손절가":  stp,
            "signals": signals,
            "source":  src,
            "caution": False,
        }

    if KIS_APP_KEY and KIS_APP_SECRET:
        kis_token()

    with ThreadPoolExecutor(max_workers=8) as ex:
        raw = list(ex.map(_fetch_ct, codes))

    passed = [r for r in raw if not r.get("_skip")]
    skips  = [r for r in raw if r.get("_skip")]

    # KIS 가격 재조회 — 순차 처리 (rate limit 방지)
    if KIS_APP_KEY and KIS_APP_SECRET:
        import time
        for item in passed[:20]:
            try:
                p_kis, src_kis = kis_price(item["코드"])
                if p_kis > 0:
                    item["현재가"] = int(p_kis)
                    item["source"] = src_kis
                cached = _KIS_NAME_CACHE.get(item["코드"])
                if cached: item["종목"] = cached
                time.sleep(0.05)
            except: pass

    top5 = sorted(passed, key=lambda x: x["점수"], reverse=True)[:5]
    return top5, skips


@st.cache_data(ttl=3600, show_spinner=False)  # 1시간 — 🔄 재스캔 버튼으로만 갱신
def scan_kr():
    # 시장 상태 진단 → 동적 pass score
    market = get_market_regime()
    dynamic_score = get_dynamic_pass_score(market)

    listing = krx_listing()
    caution_set = get_krx_caution_stocks()  # 투자주의 종목
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
        # 시장 상태에 따라 pass 기준 동적 적용
        r_pass = (r["pass"] or
                  ((r["s3"] or r["s4"]) and r["score"] >= dynamic_score))
        if not r_pass:
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
                "섹터명":get_stock_sector_name(code),
                "섹터상태":"","시장섹터":"",
                "caution": code in caution_set,
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
                # 섹터 진단 (종목별 섹터 상태)
                full = get_stock_full_regime(r["코드"])
                sec_ok_val    = full["sector"].get("ok", False)
                sec_score_val = full["sector"].get("score", 1) if sec_ok_val else 1
                sec_status    = full.get("sec_status","")
                dart_s, dart_list = dart_score(r["코드"]) if DART_API_KEY else (0, [])
                r["수급점수"] = sup
                # 섹터 조회 실패 시 0점 (잘못된 가산점 방지)
                r["섹터점수"] = (sec_score_val - 1) * 5 if sec_ok_val else 0  # -5~+5점
                r["섹터명"]   = full.get("sector_name","")
                r["섹터상태"] = sec_status
                r["시장섹터"] = full.get("summary","")
                r["공시점수"] = dart_s
                r["공시목록"] = dart_list[:2]
                r["종합점수"] = r["점수"] + sup + dart_s  # 섹터 중복 제거
                r["섹터강세"] = sec_score_val >= 2
            except:
                r["수급점수"] = 0; r["섹터점수"] = 0
                r["섹터명"]   = r.get("섹터명","")
                r["섹터상태"] = ""; r["시장섹터"] = ""
                r["공시점수"] = 0; r["공시목록"] = []
                r["종합점수"] = r["점수"]
                r["섹터강세"] = False
            return r
        with ThreadPoolExecutor(max_workers=5) as ex:
            passed = list(ex.map(_add_supply, passed))
    else:
        for r in passed:
            r["수급점수"] = 0
            r["종합점수"] = r.get("점수", 0)

    top5 = sorted(passed, key=lambda x: x.get("종합점수", x.get("점수", 0)), reverse=True)[:5]
    return top5, skips

@st.cache_data(ttl=3600, show_spinner=False)  # 1시간
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
                "섹터명":"","섹터상태":"","시장섹터":"",
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
        df = portfolio_ohlcv_kr(name)
        # 종목명: kis_name 직접 호출 (ttl=86400 캐시, 빠름)
        # 종목명 조회: KIS 캐시 → KIS API → KRX
        stock_name = _KIS_NAME_CACHE.get(name, "")
        if not stock_name and KIS_APP_KEY:
            stock_name = kis_name(name)
            if stock_name: _KIS_NAME_CACHE[name] = stock_name
        if not stock_name:
            try:
                lst = krx_listing()
                row = lst[lst["Code"]==name]
                if not row.empty:
                    stock_name = str(row["Name"].values[0])
                    if stock_name: _KIS_NAME_CACHE[name] = stock_name
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
                    "source":src,"ok":curr>0,"signals":r["signals"],
                    "atr_pct":r.get("atr_pct",0),"s3_streak":r.get("s3_streak",0)}
        if p>0:
            return {"label":label,"curr":p,"score":0,"grade":"-","rsi":50,"currency":"KRW",
                    "stop":int(p*0.93),"target":int(p*1.08),
                    "buy_min":int(p*0.97),"buy_max":int(p*1.02),
                    "source":src,"ok":True,"signals":[]}
        return FAIL

    # 해외
    p, src = us_price(name)
    pp, pp_label = us_prepost(name)
    df = portfolio_ohlcv_us(name)
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

# 코스피 기반 시장 심리
try:
    _mkt_sb = get_market_regime()
    _r1 = _mkt_sb.get("ret1", 0)
    _r5 = _mkt_sb.get("ret5", 0)
    _dd = _mkt_sb.get("dd_from_hi", 0)
    _dn = _mkt_sb.get("down_days", 0)
    # 점수 계산
    _sc = 50
    _sc += min(_r1 * 4, 20)
    _sc += min(_r5 * 1.5, 10)
    _sc += max(_dd * 0.3, -15)
    _sc -= _dn * 3
    _sc = max(0, min(100, int(_sc)))
    fgt = "극탐욕" if _sc>=75 else "탐욕" if _sc>=60 else "중립" if _sc>=40 else "공포" if _sc>=25 else "극공포"
    st.sidebar.metric("코스피 심리", f"{_sc} ({fgt})")
except: pass
st.sidebar.metric("🇺🇸 미국장", "OPEN" if is_us_open() else "CLOSED")

# 재스캔
# 버튼 3개: 추천 재스캔 / 관심종목 재스캔 / 전체 캐시 초기화
if st.sidebar.button("🔄 추천 재스캔", use_container_width=True, type="primary"):
    scan_kr.clear(); scan_us.clear(); scan_kr_sector.clear(); scan_contrarian.clear()
    ohlcv_kr.clear(); ohlcv_us.clear()
    st.session_state["scan_done"] = True
    st.rerun()

if st.sidebar.button("👀 관심종목 재평가", use_container_width=True):
    # portfolio 전용 캐시 초기화 (scan_kr/us 캐시 유지)
    portfolio_ohlcv_kr.clear()
    portfolio_ohlcv_us.clear()
    kis_investor_trend.clear()
    st.session_state["watch_refresh"] = True
    st.rerun()

if st.sidebar.button("🗑️ 전체 캐시 초기화", use_container_width=True):
    scan_kr.clear(); scan_us.clear(); scan_kr_sector.clear(); scan_contrarian.clear()
    ohlcv_kr.clear(); ohlcv_us.clear()
    krx_listing.clear(); us_tickers.clear()
    kis_investor_trend.clear(); kis_name.clear()
    _KIS_NAME_CACHE.clear()
    st.session_state["scan_done"] = False
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
                    st.session_state.portfolio.append({"name":nm,"buy":0.0,"date":"","type":"watch","memo":wm.strip(),
                                                          "bottom_date":"","bottom_low":0.0})
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
# 시장 상태 + 포트폴리오 섹터 미리 계산 (루프 밖 1회)
_mkt_regime = get_market_regime()
_mkt_down   = _mkt_regime.get("score", 2) <= 1
_caution_set = get_krx_caution_stocks()  # 투자주의 종목

# 보유종목 섹터 미리 캐싱 (루프 안에서 반복 호출 방지)
_portfolio_sectors = {}
for _p in st.session_state.portfolio:
    _code = _p.get("name","")
    if _code.isdigit() and len(_code)==6:
        try:
            _portfolio_sectors[_code] = get_stock_full_regime(_code)
        except: pass

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
            df_tmp = portfolio_ohlcv_kr(name) if is_kr else portfolio_ohlcv_us(name)
            if df_tmp is not None and len(df_tmp)>=2:
                prev = float(df_tmp["close"].iloc[-2])
                if prev>0: gap_pct = (curr-prev)/prev*100
        except: pass

        if is_kr_open():
            # 장 중 — 실제 갭
            if abs(gap_pct)<3: gv="🟢 갭 양호"; gc2="#10b981"; gd=f"갭 {gap_pct:+.1f}% — 매수 검토"
            elif abs(gap_pct)<5: gv="🟡 소폭 갭"; gc2="#f59e0b"; gd=f"갭 {gap_pct:+.1f}% — 눌림 기다려"
            else: gv="🔴 갭 과다"; gc2="#ef4444"; gd=f"갭 {gap_pct:+.1f}% — 추격 위험"
        else:
            # 장 마감 후 — 갭 의미 없음
            gv="📋 장 마감"; gc2="#64748b"; gd="내일 시초가로 갭 확인하세요"
            gap_pct = 0.0

        signal_ok = s3_on or s4_on  # quant_predict core와 동일 조건

        # 신호 소멸 시 — 바닥 형성 중인지 역발상 체크 (이미 검증된 종목이므로)
        bottom_check = ""
        if not signal_ok and is_kr:
            try:
                _df_b = portfolio_ohlcv_kr(name)
                if _df_b is not None and len(_df_b) >= 25:
                    _cl_b = _df_b["close"].astype(float)
                    _vo_b = _df_b["volume"].astype(float)
                    _ref_cl = _cl_b.iloc[:-1] if is_kr_open() and len(_cl_b)>=2 else _cl_b
                    _ref_vo = _vo_b.iloc[:-1] if is_kr_open() and len(_vo_b)>=2 else _vo_b

                    _delta = _ref_cl.diff()
                    _gain  = _delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
                    _loss  = (-_delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
                    _rsi_b = float((100-100/(1+_gain/_loss.replace(0,np.nan))).iloc[-1])

                    _cur_b  = float(_ref_cl.iloc[-1])
                    _ret20_b = (_cur_b-float(_ref_cl.iloc[-21]))/float(_ref_cl.iloc[-21])*100 if len(_ref_cl)>=21 else 0
                    # 거래량: 5일 최대값 → 3일 평균으로 변경 (노이즈 감소)
                    _avgvol_b = float(_ref_vo.rolling(20).mean().iloc[-1])
                    _vol3avg_b = float(_ref_vo.iloc[-3:].mean())
                    _volratio_b = _vol3avg_b/_avgvol_b if _avgvol_b>0 else 0

                    # 악재 공시 체크 (최우선 — 충족해도 무효화)
                    _has_bad_news = False
                    if DART_API_KEY:
                        try:
                            _disc_b = get_dart_disclosures(name, days=5)
                            _bad_kw = ["유상증자","전환사채","감자","거래정지","상장폐지","BW","관리종목","회생절차",
                                       "횡령","배임","감사의견","영업정지"]
                            _resolve_kw = ["해소","무혐의","철회","취하"]  # "정정"은 제외 (조건변경일 뿐 취소 아님)
                            _has_bad_news = any(
                                any(k in d["title"] for k in _bad_kw) and
                                not any(rk in d["title"] for rk in _resolve_kw)
                                for d in _disc_b
                            )
                        except: pass

                    # 시장 급락 체크
                    _mkt_b = _mkt_regime.get("ret1", 0)
                    _mkt_severe_panic = _mkt_b <= -3   # 패닉 — 바닥체크 무효화
                    _mkt_warn = -3 < _mkt_b <= -2       # 경고만

                    if _has_bad_news:
                        bottom_check = "🚫 악재 공시 확인 — 바닥 체크 무효 (유상증자/전환사채/횡령 등)"
                    elif _mkt_severe_panic:
                        bottom_check = f"🚫 시장 패닉(-3%↓) — 바닥 체크 무효 (코스피 {_mkt_b:+.1f}%, 개별판단 무의미)"
                    elif _volratio_b < 1.0:
                        # 거래량 Hard Filter — 평균 이하 거래량은 "관심 없는 종목"으로 즉시 제외
                        pass  # 신호 표시 안 함 (관심없는 소외주 가능성)
                    else:
                        # Soft Score (Hard Filter 통과 종목만 RSI+낙폭으로 강도 측정)
                        _bscore = 0
                        if _rsi_b <= 25: _bscore += 3
                        elif _rsi_b <= 30: _bscore += 2
                        elif _rsi_b <= 35: _bscore += 1
                        if _ret20_b <= -25: _bscore += 4
                        elif _ret20_b <= -15: _bscore += 3
                        elif _ret20_b <= -10: _bscore += 1

                        # RSI 상승전환 — PASS점수 아닌 신뢰도 라벨로만 사용
                        _rsi_up = False
                        try:
                            _rsi_prev = float((100-100/(1+_gain/_loss.replace(0,np.nan))).iloc[-2])
                            _rsi_up = _rsi_b > _rsi_prev
                        except: pass

                        if _bscore >= 6:
                            _conf = "강한 바닥 신호" if _bscore == 7 else "바닥 신호"
                        elif _bscore >= 4:
                            _conf = "약한 과매도"
                        else:
                            _conf = None

                        if _conf:
                            _warn = " ⚠️시장약세주의" if _mkt_warn else ""
                            _trust = " 🔵RSI반등정황(참고용)" if (_rsi_up and _bscore>=7) else ""
                            bottom_check = f"⚡ {_conf}({_bscore}/7점) — RSI {_rsi_b:.0f} + 20일 {_ret20_b:.0f}% + 거래량{_volratio_b:.1f}배{_warn}{_trust}"
                        elif _ret20_b <= -10:
                            bottom_check = f"🔍 과매도 관찰 구간({_bscore}/7점) — RSI {_rsi_b:.0f} + 20일 {_ret20_b:.0f}%"
            except: pass

        # 섹터 추이
        watch_sec_nm = ""; watch_sec_st = ""
        if is_kr:
            try:
                _wfull = get_stock_full_regime(name)
                watch_sec_nm = _wfull.get("sec_name","")
                _wss = _wfull.get("sec_score", 1)
                watch_sec_st = "🟢 강세" if _wss>=2 else "🟡 보합" if _wss==1 else "🟠 약세" if _wss==0 else "🔴 급락"
            except: pass

        # 수급
        sup_html=""
        if is_kr and KIS_APP_KEY:
            sup = supply_signal(name)
            if sup.get("ok"):
                sup_sigs = " / ".join(sup.get("signals",[])[:3])
                sup_html = f'<div style="background:#0f172a;padding:8px;border-radius:6px;margin:6px 0;font-size:12px;">수급: <span style="color:{sup["color"]};font-weight:bold;">{sup["verdict"]}</span> <span style="color:#64748b;">{sup_sigs}</span></div>'

        # 종합 판단
        # 가중치 재조정: S3/S4(4) > 시장(3) > 수급(2) > 갭(1)
        bs = 0
        mkt_w = _mkt_regime
        mkt_score_w = mkt_w.get("score", 2)
        if signal_ok: bs+=4              # S3/S4 핵심 신호 — 최우선
        if mkt_score_w == 3:   bs+=3     # 상승장
        elif mkt_score_w <= 0: bs-=4     # 하락장 페널티 강화
        if is_kr and KIS_APP_KEY:
            sup2 = supply_signal(name)
            if sup2.get("ok") and sup2.get("score",0)>=3: bs+=2
        if abs(gap_pct)<3: bs+=1         # 갭은 보조 지표로 축소
        elif abs(gap_pct)<5: bs+=0.5

        if is_kr_open():
            bv="⚠️ 장 중 — 내일 시초가 확인 후 판단"; bc="#f59e0b"
        elif bs>=8: bv="🟢 매수 적극 고려"; bc="#10b981"
        elif bs>=5: bv="🟡 조건부 매수 (시장 확인)"; bc="#f59e0b"
        elif mkt_score_w <= 0: bv="🔴 하락장 — 매수 자제"; bc="#ef4444"
        else: bv="🔴 매수 보류"; bc="#ef4444"

        memo = p.get("memo","")
        st.markdown(f"""
<div style="background:#1e293b;padding:14px;border-radius:10px;border-left:5px solid {gc};margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <b>👀 {d['label']}</b>{f" <span style='color:#ef4444;font-size:10px;'>⚠️ 투자주의</span>" if name in _caution_set else ""}
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
  {f'<div style="background:#0f172a;padding:8px;border-radius:6px;margin-top:6px;font-size:11px;"><span style="color:#64748b;">섹터:</span> <span style="color:#94a3b8;">{watch_sec_nm}</span> <span style="margin-left:6px;">{watch_sec_st}</span></div>' if watch_sec_nm else ''}
  {f'<div style="background:#1a2744;border:1px solid #6366f1;padding:8px;border-radius:6px;margin-top:6px;font-size:12px;color:#a5b4fc;">{bottom_check}</div>' if bottom_check else ''}
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

    # ATR 기반 손절 (변동성 반영) — 없으면 기존 -7%/+8% fallback
    _atr_pct = d.get("atr_pct", 0)
    if _atr_pct > 0:
        _stop_pct = min(max(_atr_pct * 2 / 100, 0.04), 0.08)  # 4~8% 범위 (모순 방지)
        fixed_stop = buy * (1 - _stop_pct)
    else:
        fixed_stop = buy * 0.93
    fixed_tgt = buy * 1.08
    rsi_v = d["rsi"]
    trend_broken = not s3_on

    # 정배열 붕괴 시 — 바닥 형성 중인지 확인 (이미 검증된 종목이므로)
    hold_bottom_check = ""
    hold_bottom_invalid = False  # 악재 등으로 무효화 여부
    ar_override_lowbreak = ""    # 저가갱신 시 손절고려 메시지
    if not trend_broken and (p.get("bottom_date") or p.get("bottom_low")):
        # 정배열 회복 시 추적 리셋
        p["bottom_date"] = ""; p["bottom_low"] = 0.0
        save_portfolio(st.session_state.portfolio)

    if trend_broken and is_kr:
        try:
            _df_b2 = portfolio_ohlcv_kr(name)
            if _df_b2 is not None and len(_df_b2) >= 25:
                _cl_b2 = _df_b2["close"].astype(float)
                _vo_b2 = _df_b2["volume"].astype(float)
                _ref_cl2 = _cl_b2.iloc[:-1] if is_kr_open() and len(_cl_b2)>=2 else _cl_b2
                _ref_vo2 = _vo_b2.iloc[:-1] if is_kr_open() and len(_vo_b2)>=2 else _vo_b2

                _delta2 = _ref_cl2.diff()
                _gain2  = _delta2.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
                _loss2  = (-_delta2.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
                _rsi_b2 = float((100-100/(1+_gain2/_loss2.replace(0,np.nan))).iloc[-1])

                _cur_b2  = float(_ref_cl2.iloc[-1])
                _ret20_b2 = (_cur_b2-float(_ref_cl2.iloc[-21]))/float(_ref_cl2.iloc[-21])*100 if len(_ref_cl2)>=21 else 0
                _avgvol_b2 = float(_ref_vo2.rolling(20).mean().iloc[-1])
                _vol3avg_b2 = float(_ref_vo2.iloc[-3:].mean())
                _volratio_b2 = _vol3avg_b2/_avgvol_b2 if _avgvol_b2>0 else 0

                # 악재 공시 체크 (최우선)
                _has_bad_news2 = False
                if DART_API_KEY:
                    try:
                        _disc_b2 = get_dart_disclosures(name, days=5)
                        _bad_kw2 = ["유상증자","전환사채","감자","거래정지","상장폐지","BW","관리종목","회생절차",
                                         "횡령","배임","감사의견","영업정지"]
                        _resolve_kw2 = ["해소","무혐의","철회","취하"]
                        _has_bad_news2 = any(
                            any(k in d["title"] for k in _bad_kw2) and
                            not any(rk in d["title"] for rk in _resolve_kw2)
                            for d in _disc_b2
                        )
                    except: pass

                _mkt_b2 = _mkt_regime.get("ret1", 0)
                _mkt_severe_panic2 = _mkt_b2 <= -3
                _mkt_warn2 = -3 < _mkt_b2 <= -2

                if _has_bad_news2 or _mkt_severe_panic2:
                    hold_bottom_invalid = True
                elif _volratio_b2 < 1.0:
                    pass  # 거래량 Hard Filter — 평균 이하면 신호 표시 안 함
                else:
                    _bscore2 = 0
                    if _rsi_b2 <= 25: _bscore2 += 3
                    elif _rsi_b2 <= 30: _bscore2 += 2
                    elif _rsi_b2 <= 35: _bscore2 += 1
                    if _ret20_b2 <= -25: _bscore2 += 4
                    elif _ret20_b2 <= -15: _bscore2 += 3
                    elif _ret20_b2 <= -10: _bscore2 += 1

                    _rsi_up2 = False
                    try:
                        _rsi_prev2 = float((100-100/(1+_gain2/_loss2.replace(0,np.nan))).iloc[-2])
                        _rsi_up2 = _rsi_b2 > _rsi_prev2
                    except: pass

                    if _bscore2 >= 6:
                        _w2 = " ⚠️시장약세주의" if _mkt_warn2 else ""
                        _trust2 = " 🔵RSI반등정황(참고용)" if _rsi_up2 else ""

                        # 시간 필터 — 신규 저가 갱신 체크
                        _today_str = datetime.now().strftime("%Y-%m-%d")
                        if not p.get("bottom_date"):
                            # 바닥신호 최초 발생 — 기준 저가 기록
                            p["bottom_date"] = _today_str
                            p["bottom_low"] = _cur_b2
                            save_portfolio(st.session_state.portfolio)
                            hold_bottom_check = f"과매도 반등 가능성({_bscore2}/7점) — RSI {_rsi_b2:.0f} + 20일{_ret20_b2:.0f}%+거래량{_volratio_b2:.1f}배{_w2}{_trust2}. 추세이탈 아닌 일시조정일 수 있음(확정 아님)"
                        else:
                            try:
                                _days_since = (datetime.now() - datetime.strptime(p["bottom_date"],"%Y-%m-%d")).days
                            except: _days_since = 0
                            _ref_low = p.get("bottom_low", _cur_b2)

                            if _cur_b2 < _ref_low:
                                # 신규 저가 갱신 → 바닥신호 무효, 기준 저가 갱신
                                p["bottom_low"] = _cur_b2
                                save_portfolio(st.session_state.portfolio)
                                hold_bottom_check = ""  # 무효화
                                hold_bottom_invalid = True
                                ar_override_lowbreak = f"⏰ 바닥신호 후 저가 갱신 ({_days_since}일 경과) — 반등 실패, 재평가 필요"
                            elif _days_since >= 3:
                                hold_bottom_check = f"과매도 반등 가능성({_bscore2}/7점) — {_days_since}일째 저가 유지 중. RSI {_rsi_b2:.0f}{_w2}{_trust2}"
                            else:
                                hold_bottom_check = f"과매도 반등 가능성({_bscore2}/7점) — RSI {_rsi_b2:.0f} + 20일{_ret20_b2:.0f}%+거래량{_volratio_b2:.1f}배{_w2}{_trust2} ({_days_since}일째)"
        except: pass

    mkt = _mkt_regime
    mkt_score  = mkt.get("score", 2)
    mkt_down   = mkt_score <= 1
    mkt_bull   = mkt_score >= 3

    # 섹터 상태 (국내만)
    sec_score = 1; sec_down = False; sec_bull = False
    combined_down = False; combined_bull = False
    sec_nm_d = ""; sec_st_d = ""
    if is_kr:
        full_h    = _portfolio_sectors.get(name) or get_stock_full_regime(name)
        sec_score = full_h["sector"].get("score", 1)
        sec_down  = sec_score < 0
        sec_bull  = sec_score >= 2
        combined_down = mkt_down and sec_down
        combined_bull = mkt_bull and sec_bull
        sec_nm_d  = full_h.get("sector_name","")
        sec_st_d  = full_h.get("sec_status","")

    # ── 포지션 판단 (시장+섹터+차트 종합) ──
    # ── 보유종목 판단 v10 (5단계 단순화) ──
    if curr <= fixed_stop:
        act="🔴 즉시 손절"; ac="#ef4444"
        ar=f"평단 -7% 이탈 ({profit:.1f}%)"
    elif combined_down and profit < -3:
        act="🔴 즉시 손절 고려"; ac="#ef4444"
        ar=f"시장+섹터 동반하락({sec_nm_d})+손실{profit:.1f}%"
    elif hold_bottom_check and not hold_bottom_invalid:
        act="🟣 과매도 반등 가능성 — 신중 관찰"; ac="#a855f7"
        ar=hold_bottom_check
    elif ar_override_lowbreak and profit < -5:
        act="🔴 손절 고려"; ac="#ef4444"
        ar=ar_override_lowbreak
    elif (mkt_down and profit < -5) or (trend_broken and profit < -5 and hold_days >= 5):
        act="🔴 손절 고려"; ac="#ef4444"
        ar=f"{'하락장' if mkt_down else '정배열붕괴'}+손실{profit:.1f}%" + (" [악재공시 확인됨]" if hold_bottom_invalid else "")
    elif curr >= fixed_tgt and s3_on and rsi_v <= 60 and combined_bull:
        act="🚀 홀딩 (추가 상승 여지)"; ac="#10b981"
        ar=f"목표가 도달({profit:.1f}%) + 정배열유지 + RSI{rsi_v:.0f} + 시장섹터강세 — 추가 상승 가능"
    elif curr >= fixed_tgt and s3_on and rsi_v <= 60:
        act="🟡 일부 익절 검토"; ac="#f59e0b"
        ar=f"목표가 도달({profit:.1f}%) + 정배열유지 — 일부 익절 후 나머지 홀딩 고려"
    elif curr >= fixed_tgt or (rsi_v > 70 and profit > 5):
        act="🟡 익절 고려"; ac="#f59e0b"
        ar=f"{'목표가 도달+추세약화' if curr>=fixed_tgt else f'RSI과열({rsi_v:.0f})'}({profit:.1f}%)"
    elif profit > 3 and trend_broken:
        act="🟡 익절 고려 (추세약화)"; ac="#f59e0b"
        ar=f"수익 {profit:.1f}% + 정배열 붕괴 — 수익 확정 검토"
    elif profit > 5 and not s3_on and rsi_v > 60:
        act="🟡 익절 고려 (차트 피크)"; ac="#f59e0b"
        ar=f"수익 {profit:.1f}% + 신호 소멸 + RSI {rsi_v:.0f} — 고점 가능성"
    elif combined_bull and s3_on and 35<=rsi_v<=60 and profit>=-5:
        act="🟢 추가매수 검토"; ac="#10b981"
        ar=f"시장+섹터강세+정배열+RSI({rsi_v:.0f})"
    elif s3_on and profit > 0:
        act="⚪ 홀딩"; ac="#94a3b8"
        ar=f"정배열유지+수익{profit:.1f}%"
    elif hold_days > 0 and hold_days <= 3:
        act="⚪ 관망"; ac="#94a3b8"
        ar=f"매수{hold_days}일차 — 판단 유보"
    else:
        act="⬜ 관망"; ac="#64748b"
        ar=f"{'하락장' if mkt_down else '섹터약세' if sec_down else '신호 대기'}"
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

    # 섹터 + 수급 (국내)
    sup_html2=""
    if is_kr and KIS_APP_KEY:
        # 미리 계산된 섹터 사용
        full_r = _portfolio_sectors.get(name) or get_stock_full_regime(name)
        sec_nm_h  = full_r.get("sector_name","")
        sec_st_h  = full_r.get("sec_status","")
        mkt_sum_h = full_r.get("summary","")
        sec_color_h = full_r.get("color","#64748b")
        # 섹터명 없어도 시장+섹터 종합 상태는 표시
        display_sec = sec_nm_h or "업종확인중"
        sup_html2 += f'<div style="background:#0f172a;padding:6px 10px;border-radius:6px;margin:4px 0;font-size:11px;border-left:3px solid {sec_color_h};">🏭 <span style="color:#94a3b8;">{display_sec}</span> <span style="color:{sec_color_h};font-weight:bold;"> {sec_st_h}</span> <span style="color:#64748b;font-size:10px;">{mkt_sum_h}</span></div>'
        # 수급
        sup=supply_signal(name)
        if sup.get("ok"):
            sigs2=" / ".join(sup.get("signals",[])[:2])
            sup_html2+=f'<div style="background:#0f172a;padding:8px;border-radius:6px;margin:6px 0;font-size:11px;">수급: <span style="color:{sup["color"]};font-weight:bold;">{sup["verdict"]}</span> <span style="color:#64748b;">{sigs2}</span></div>'

    st.markdown(f"""
<div style="background:#1e293b;padding:14px;border-radius:10px;border-left:5px solid {gc};margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <b>📈 {d['label']}</b>{f" <span style='color:#ef4444;font-size:10px;'>⚠️ 투자주의</span>" if name in _caution_set else ""}
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
      <div style="font-size:9px;color:#94a3b8;">{'ATR손절' if _atr_pct>0 else '고정손절'}</div>
      <div style="color:#ef4444;font-size:11px;font-weight:bold;">{fmt(fixed_stop)}</div>
      <div style="color:#64748b;font-size:8px;">({(buy-fixed_stop)/buy*100:.1f}%)</div>
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

    # 평단가 수정 버튼 (물타기/추가매수 후 재설정)
    if ptype == "hold":
        _ekey = f"pedit_{i}"  # 위젯 key 충돌 방지
        if st.button("✏️ 평단가 수정", key=f"pbtn_{i}"):
            st.session_state[_ekey] = not st.session_state.get(_ekey, False)
        if st.session_state.get(_ekey):
            new_buy = st.number_input("새 평단가", min_value=0.0,
                value=float(p.get("buy",0) or 0),
                step=100.0, format="%.0f", key=f"pnbuy_{i}")
            new_date = st.text_input("매수일자",
                value=p.get("date", datetime.now().strftime("%Y-%m-%d")),
                key=f"pndate_{i}")
            col_ok, col_cancel = st.columns(2)
            if col_ok.button("✅ 저장", key=f"pnsave_{i}") and new_buy > 0:
                p["buy"] = float(new_buy)
                p["date"] = new_date
                st.session_state[_ekey] = False
                save_portfolio(st.session_state.portfolio)
                st.rerun()
            if col_cancel.button("❌ 취소", key=f"pncancel_{i}"):
                st.session_state[_ekey] = False
                st.rerun()

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
# 시장 상태 진단
market_regime = get_market_regime()

# 시장 상태 배너
reg_color = market_regime.get("color","#64748b")
reg_name  = market_regime.get("regime","알수없음")
reg_desc  = market_regime.get("desc","")
reg_score = market_regime.get("score", 2)

if reg_score == 3:
    banner_msg = "✅ 상승장 — 적극 매수 적합"
elif reg_score == 2:
    banner_msg = "🟡 중립장 — 선별적 접근"
elif reg_score == 1:
    banner_msg = "⚠️ 조정장 — 고품질 종목만 / 소량 진입"
else:
    if "극공포" in reg_name:
        banner_msg = "🔥 극공포 — 역발상 매수 기회 탐색 중"
    else:
        banner_msg = "🔴 하락장 — 매수 자제 / 현금 보유 권장"

st.markdown(f"""
<div style="background:{reg_color}22;border:1px solid {reg_color};
            border-radius:8px;padding:10px 14px;margin-bottom:12px;">
  <span style="color:{reg_color};font-weight:bold;font-size:14px;">
    📊 코스피 {reg_name}
  </span>
  <span style="color:#94a3b8;font-size:12px;margin-left:8px;">{reg_desc}</span><br>
  <span style="font-size:12px;color:{reg_color};">{banner_msg}</span>
  <span style="font-size:10px;color:#64748b;margin-left:8px;">
    (pass기준 {get_dynamic_pass_score(market_regime)}점 적용)
  </span>
</div>""", unsafe_allow_html=True)

# 스캔 지연 로딩 — 버튼 클릭 or 캐시 있을 때만 실행
if "scan_done" not in st.session_state:
    st.session_state["scan_done"] = False

if not st.session_state["scan_done"]:
    st.info("📊 추천 종목을 보려면 **🔄 추천 재스캔** 버튼을 눌러주세요.")
    kr_top, kr_skip, us_top, us_skip = [], [], [], []
else:
    with st.spinner("스캔 중..."):
        kr_top, kr_skip = scan_kr()
        us_top, us_skip = scan_us()

# FESI 계산
_fesi_data = get_etf_supply()
_spot      = _fesi_data.get("spot_kospi", {})
_lev       = _fesi_data.get("lev_kospi", {})
_inv       = _fesi_data.get("inv_kospi", {})
_kfut      = _fesi_data.get("kospi_fut", {})
_sfut      = _fesi_data.get("sp_fut", {})
_spot_buy  = _spot.get("is_buying", False)
_lev_buy   = _lev.get("is_buying", False)
_inv_buy   = _inv.get("is_buying", False)
_lev_flip  = _lev.get("flip_buy", False)
_fut_bull  = _kfut.get("bullish", False) or _sfut.get("bullish", False)
_fut_bear  = _kfut.get("bearish", False) and _sfut.get("bearish", False)

_fesi_boost = False
if _fesi_data and _spot_buy and (_lev_flip or _lev_buy) and not _inv_buy:
    _fesi_signal = "🟢🟢 강한 상승"; _fesi_ok = True; _fesi_boost = True
elif _fesi_data and _spot_buy and not _lev_buy and not _inv_buy:
    _fesi_signal = "🟢 현물 매수"; _fesi_ok = True
elif _fesi_data and not _spot_buy and _lev_buy and not _inv_buy:
    _fesi_signal = "🟡 지수 베팅(개별주 주의)"; _fesi_ok = False
elif _fesi_data and _inv_buy and not _spot_buy:
    _fesi_signal = "🔴 하락 헤지"; _fesi_ok = False
elif _fut_bear:
    _fesi_signal = "🔴 선물 하락"; _fesi_ok = False
else:
    _fesi_signal = ""; _fesi_ok = True

# FESI 기반 TOP5 재조정
try:
    if _fesi_boost and len(kr_top) < 5:
        _extra = sorted([x for x in kr_skip if isinstance(x,dict) and x.get("종합점수",0)>0],
                        key=lambda x: x.get("종합점수",0), reverse=True)
        for _e in _extra:
            if len(kr_top) >= 5: break
            kr_top.append(_e)
    elif not _fesi_ok and kr_top:
        kr_top = kr_top[:3]
except: pass

# FESI 사전 계산 (TOP5 카드 연동용)
_fesi_data = get_etf_supply()
_fesi_spot = _fesi_data.get("spot_kospi", {})
_fesi_lev  = _fesi_data.get("lev_kospi", {})
_fesi_inv  = _fesi_data.get("inv_kospi", {})
_spot_buy  = _fesi_spot.get("is_buying", False)
_lev_buy   = _fesi_lev.get("is_buying", False)
_inv_buy   = _fesi_inv.get("is_buying", False)
_lev_flip  = _fesi_lev.get("flip_buy", False)

if _fesi_data and _spot_buy and (_lev_flip or _lev_buy) and not _inv_buy:
    _fesi_signal = "🟢🟢 강한 상승"; _fesi_ok = True
elif _fesi_data and _spot_buy and not _lev_buy:
    _fesi_signal = "🟢 현물 매수"; _fesi_ok = True
elif _fesi_data and not _spot_buy and _lev_buy and not _inv_buy:
    _fesi_signal = "🟡 지수 베팅(개별주 주의)"; _fesi_ok = False
elif _fesi_data and _inv_buy and not _spot_buy:
    _fesi_signal = "🔴 하락 헤지"; _fesi_ok = False
else:
    _fesi_signal = ""; _fesi_ok = True  # 중립 or 데이터 없음


# ── 내일 매수 환경 판단 (스캔 완료 후 1회만 렌더링) ──
tomorrow = get_tomorrow_outlook()
t_color  = tomorrow["color"]
t_verdict= tomorrow["verdict"]
nq_ret   = tomorrow["nasdaq_ret"]
fx_ret   = tomorrow.get("fx_ret", 0)
usd_krw  = tomorrow["usd_krw"]
fut_ret  = tomorrow["futures_ret"]

nq_emoji  = "🟢" if nq_ret >= 1 else "🔴" if nq_ret <= -1 else "🟡"
fx_emoji  = "🟢" if fx_ret < 0 else "🔴" if fx_ret > 0.5 else "🟡"
fut_emoji = "🟢" if fut_ret >= 0.3 else "🔴" if fut_ret <= -0.3 else "🟡"

st.markdown(f"""
<div style="background:#0f172a;border:1px solid {t_color};
            border-radius:8px;padding:10px 14px;margin-bottom:12px;">
  <div style="font-weight:bold;color:{t_color};font-size:13px;margin-bottom:6px;">
    📅 내일 매수 환경 — <span>{t_verdict}</span>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;font-size:11px;">
    <div style="background:#1e293b;padding:6px;border-radius:6px;text-align:center;">
      <div style="color:#64748b;font-size:9px;">나스닥</div>
      <div style="color:{'#10b981' if nq_ret>=0 else '#ef4444'};font-weight:bold;">{nq_emoji} {nq_ret:+.1f}%</div>
    </div>
    <div style="background:#1e293b;padding:6px;border-radius:6px;text-align:center;">
      <div style="color:#64748b;font-size:9px;">S&P선물</div>
      <div style="color:{'#10b981' if fut_ret>=0 else '#ef4444'};font-weight:bold;">{fut_emoji} {fut_ret:+.1f}%</div>
    </div>
    <div style="background:#1e293b;padding:6px;border-radius:6px;text-align:center;">
      <div style="color:#64748b;font-size:9px;">원/달러</div>
      <div style="color:{'#10b981' if fx_ret<0 else '#ef4444'};font-weight:bold;">{fx_emoji} {usd_krw:.0f}원</div>
    </div>
  </div>
</div>""", unsafe_allow_html=True)

# 장 중 경고
if is_kr_open():
    st.warning("""
⚠️ **지금 장 중이에요 (09:00~15:30)**

장 중 신호는 미완성봉 기준이라 **오탐이 많아요.**

✅ 올바른 사용법: **장 마감 후 (15:30 이후)** 재스캔
- 관심종목 등록 → 다음날 시초가 보고 매수 결정
- 장 중 추천 종목은 참고만 하고 즉시 매수 자제
""")

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
            # 투자주의 표시
            if item.get("caution"):
                extra_badges += "<span style='font-size:10px;color:#ef4444;background:#1e293b;padding:2px 6px;border-radius:3px;'>⚠️ 투자주의</span> "
            # 섹터 상태 표시
            sec_nm_card  = item.get("섹터명","")
            sec_st_card  = item.get("섹터상태","")
            mkt_sec_card = item.get("시장섹터","")
            if sec_nm_card:
                sec_color = "#10b981" if "강세" in sec_st_card else "#f59e0b" if "보합" in sec_st_card else "#ef4444"
                extra_badges += f"<span style='font-size:10px;color:{sec_color};'>🏭 {sec_nm_card} {sec_st_card}</span> "
            if item.get('공시목록'):
                title_short = item['공시목록'][0]['title'][:18]
                extra_badges += f"<span style='font-size:10px;color:#f59e0b;'>📢 {title_short}</span> "
            if mkt_sec_card:
                extra_badges += f"<span style='font-size:10px;color:#a78bfa;'>{mkt_sec_card}</span>"

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

tab_kr, tab_sector, tab_us, tab_etf = st.tabs(["🔥 국내 TOP5", "🏆 섹터 대장 TOP5", "🇺🇸 해외 TOP5", "📊 ETF수급"])
with tab_kr:
    # FESI 연동 배너
    if _fesi_signal:
        _fc = "#10b981" if _fesi_ok else "#ef4444" if "🔴" in _fesi_signal else "#f59e0b"
        _msg = "✅ 스캐너 종목 진입 고려" if _fesi_ok else "⛔ 신규매수 자제 — ETF 수급 부정적"
        st.markdown(f"""<div style="background:#0f172a;border:1px solid {_fc};border-radius:6px;
padding:8px 12px;margin-bottom:8px;font-size:12px;">
<span style="color:{_fc};font-weight:bold;">📊 FESI {_fesi_signal}</span>
&nbsp;—&nbsp; {_msg}
</div>""", unsafe_allow_html=True)
    render("국내 폭등 예측 TOP 5", kr_top, "KRW")
with tab_sector:
    st.caption("각 업종 시총 1위 종목 중 신호 강도 순 TOP5")
    if not st.session_state.get("scan_done"):
        st.info("📊 추천 재스캔 버튼을 먼저 눌러주세요.")
        sector_top = []
    else:
        with st.spinner("섹터 대장주 스캔 중..."):
            sector_top, sector_skip = scan_kr_sector()
    medals2 = ["🥇","🥈","🥉","4️⃣","5️⃣"]
    for i, item in enumerate(sector_top):
        gc = {"A+":"#f59e0b","A":"#10b981","B+":"#3b82f6","B":"#94a3b8","C":"#64748b"}.get(item.get("등급","C"),"#64748b")
        pass_badge = "✅ 신호발화" if item.get("pass") else "⬜ 신호없음"
        pass_color = "#10b981" if item.get("pass") else "#64748b"
        flags = item.get("s_flags",[False]*7)
        badges = " ".join(
            f"<span style='background:{'#10b981' if ok else '#1e293b'};color:{'#fff' if ok else '#475569'};font-size:9px;padding:2px 4px;border-radius:3px;'>{lbl}</span>"
            for ok,lbl in zip(flags, S_LABELS))
        score_str = f"차트 <b>{item['점수']}점</b>"
        if item.get("수급점수",0)>0: score_str += f" + 수급 <b style='color:#10b981'>{item['수급점수']}</b>"
        total = item.get("종합점수", item["점수"])
        score_str += f" = <b style='color:#f59e0b'>{total}점</b>"
        sigs_html = "".join(f"<li style='font-size:11px;margin:2px 0;'>{s}</li>" for s in item.get("signals",[]))
        def ff2(v): return f"${v:,.2f}" if v>1 else f"${v:,.4f}"
        fmt3 = lambda v: f"₩{int(v):,}"
        st.markdown(f"""
<div style="background:#1e293b;padding:14px;border-radius:10px;border-left:4px solid {gc};margin-bottom:8px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
    <b>{medals2[i]} {item['종목']}</b>
    <span style="background:{gc};color:#000;font-size:11px;padding:2px 6px;border-radius:4px;">{item.get('등급','?')}</span>
  </div>
  <div style="font-size:10px;color:#a78bfa;margin-bottom:6px;">🏭 {item.get('섹터','')}</div>
  <div style="margin-bottom:6px;">{badges}</div>
  <div style="font-size:12px;line-height:1.8;">
    🎯 {score_str} | RSI <b>{item['RSI']}</b><br>
    <span style="color:{pass_color};font-size:11px;">{pass_badge}</span><br>
    💰 <b>{fmt3(item['현재가'])}</b> <span style="font-size:10px;color:#64748b;">({item.get('source','')})</span><br>
    🟢 {item['매수구간']}<br>
    📈 <span style="color:#3b82f6;">{fmt3(item['목표가'])}</span>
    📉 <span style="color:#ef4444;">{fmt3(item['손절가'])}</span>
  </div>
  <details><summary style="font-size:11px;color:#94a3b8;cursor:pointer;">신호 상세</summary>
    <ul style="padding-left:14px;margin-top:4px;">{sigs_html}</ul>
  </details>
</div>""", unsafe_allow_html=True)
    if not sector_top:
        st.info("섹터 대장주 데이터를 가져오는 중이에요. 잠시 후 재스캔해보세요.")
with tab_us:
    render("해외 폭등 예측 TOP 5", us_top, "USD")
with tab_etf:
    st.caption("📊 FESI — 외국인 ETF/현물/선물 종합 수급")
    if not _fesi_data:
        st.info("KIS API 연결 필요")
    else:
        # ── 종합 신호 배너 ──
        _spot2   = _fesi_data.get("spot_kospi", {})
        _lev2    = _fesi_data.get("lev_kospi", {})
        _inv2    = _fesi_data.get("inv_kospi", {})
        _kfut2   = _fesi_data.get("kospi_fut", {})
        _sfut2   = _fesi_data.get("sp_fut", {})
        _sb2     = _spot2.get("is_buying", False)
        _lb2     = _lev2.get("is_buying", False)
        _ib2     = _inv2.get("is_buying", False)
        _lf2     = _lev2.get("flip_buy", False)
        _if2     = _inv2.get("flip_sell", False)

        if _sb2 and (_lf2 or _lb2) and not _ib2:
            _fsig = "🟢🟢 강한 상승 — 스캐너 종목 적극 진입"
            _fcol = "#10b981"
        elif _sb2 and not _lb2 and not _ib2:
            _fsig = "🟢 현물 매수 — 스캐너 종목 진입 고려"
            _fcol = "#10b981"
        elif not _sb2 and _lb2 and not _ib2:
            _fsig = "🟡 지수 베팅 — 개별주 주의"
            _fcol = "#f59e0b"
        elif _ib2 and not _sb2:
            _fsig = "🔴 하락 헤지 — 신규매수 자제"
            _fcol = "#ef4444"
        else:
            _fsig = "⬜ 관망 — 고점수 종목만"
            _fcol = "#64748b"

        st.markdown(f"""<div style="background:#0f172a;border:2px solid {_fcol};
border-radius:10px;padding:12px;margin-bottom:10px;">
<div style="font-size:14px;font-weight:bold;color:{_fcol};">{_fsig}</div>
</div>""", unsafe_allow_html=True)

        # ── 현물 수급 ──
        if _spot2:
            _st2 = _spot2.get("today",0)
            _sc2 = "#10b981" if _st2>0 else "#ef4444"
            _sfl = " 🔄매수전환" if _spot2.get("flip_buy") else " 🔄매도전환" if _spot2.get("flip_sell") else ""
            st.markdown(f"""<div style="background:#1a2744;border:1px solid {_sc2};
padding:8px 12px;border-radius:8px;margin-bottom:6px;">
<span style="font-size:12px;font-weight:bold;">🏛️ 코스피 현물</span>
<span style="color:{_sc2};font-size:11px;float:right;">{_st2:+,.0f}주{_sfl}</span>
<div style="font-size:10px;color:#64748b;margin-top:2px;">전일:{_spot2.get("prev",0):+,.0f} | 3일:{_spot2.get("d3",0):+,.0f}주</div>
</div>""", unsafe_allow_html=True)

        # ── ETF 카드 ──
        for _ek, _ed in _fesi_data.items():
            if _ek in ("spot_kospi","kospi_fut","sp_fut") or not _ed: continue
            _et = _ed.get("today",0); _ep = _ed.get("prev",0); _ed3 = _ed.get("d3",0)
            _ec = "#10b981" if _et>0 else "#ef4444" if _et<0 else "#64748b"
            _efl = " 🔄매수전환" if _ed.get("flip_buy") else " 🔄매도전환" if _ed.get("flip_sell") else ""
            st.markdown(f"""<div style="background:#1e293b;padding:8px 12px;
border-radius:8px;margin-bottom:4px;border-left:3px solid {_ec};">
<div style="display:flex;justify-content:space-between;">
<span style="font-size:11px;font-weight:bold;">{_ed.get("name","")}</span>
<span style="color:{_ec};font-size:11px;">{_et:+,.0f}주{_efl}</span>
</div>
<div style="font-size:10px;color:#64748b;">전일:{_ep:+,.0f} | 3일:{_ed3:+,.0f}주</div>
</div>""", unsafe_allow_html=True)

        # ── 선물 카드 ──
        if _kfut2 or _sfut2:
            st.markdown("---"); st.caption("선물 지표")
            _c1, _c2 = st.columns(2)
            if _kfut2:
                _kr2 = _kfut2.get("ret",0); _kc2 = "#10b981" if _kr2>0 else "#ef4444"
                _c1.markdown(f"""<div style="background:#1e293b;padding:10px;border-radius:8px;
border-left:3px solid {_kc2};text-align:center;">
<div style="font-size:10px;color:#64748b;">코스피 야간</div>
<div style="color:{_kc2};font-weight:bold;">{_kr2:+.2f}%</div></div>""", unsafe_allow_html=True)
            if _sfut2:
                _sr2 = _sfut2.get("ret",0); _sc3 = "#10b981" if _sr2>0 else "#ef4444"
                _c2.markdown(f"""<div style="background:#1e293b;padding:10px;border-radius:8px;
border-left:3px solid {_sc3};text-align:center;">
<div style="font-size:10px;color:#64748b;">S&P500 선물</div>
<div style="color:{_sc3};font-weight:bold;">{_sr2:+.2f}%</div></div>""", unsafe_allow_html=True)

        st.markdown("---")
        st.caption("""FESI 활용법
🟢🟢 강한 상승 → TOP5 종목 수 확장 + 적극 진입
🟢 현물 매수   → TOP5 정상 진입 고려
🔴 하락 헤지   → TOP5 상위 3개만 / 신규매수 자제
📊 선물 하락 시 갭하락 주의 — 다음날 시초가 확인 후 진입""")

    def is_kr_open() -> bool:
        """한국 장 중 여부 (09:00~15:30) — KST 기준"""
        try:
            if ZoneInfo:
                now = datetime.now(ZoneInfo("Asia/Seoul"))
            else:
                # UTC+9 수동 변환
                now = datetime.utcnow() + timedelta(hours=9)
            if now.weekday() >= 5: return False
            open_t  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
            close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
            return open_t <= now <= close_t
        except: return False


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
    @st.cache_data(ttl=1800, show_spinner=False)
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

    @st.cache_data(ttl=300, show_spinner=False)
    def portfolio_ohlcv_kr(code):
        """보유/관심종목 전용 ohlcv — scan_kr과 독립 캐시"""
        try:
            import FinanceDataReader as fdr
            df = fdr.DataReader(code, start="2024-01-01")
            if df is not None and len(df)>=60:
                df.columns=[c.lower() for c in df.columns]
                return df
        except: pass
        return ohlcv_kr(code)

    @st.cache_data(ttl=300, show_spinner=False)
    def portfolio_ohlcv_us(ticker):
        return ohlcv_us(ticker)

    @st.cache_data(ttl=1800, show_spinner=False)
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
            # KST 기준 날짜 (Railway는 UTC)
            if ZoneInfo:
                today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
            else:
                today = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y%m%d")
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


    st.markdown(f"""
<div style="background:#0f172a;border:2px solid {fesi_color};
            border-radius:10px;padding:14px;margin-bottom:10px;">
  <div style="font-size:15px;font-weight:bold;color:{fesi_color};margin-bottom:6px;">
    {fesi}
  </div>
  <div style="font-size:13px;margin-bottom:4px;">{fesi_action}</div>
  {f'<div style="font-size:11px;color:#64748b;margin-top:4px;">{fesi_ref}</div>' if fesi_ref else ''}
</div>""", unsafe_allow_html=True)

    # 현물 수급 표시
    if spot:
        spot_today = spot.get("today", 0)
        spot_color = "#10b981" if spot_today > 0 else "#ef4444" if spot_today < 0 else "#64748b"
        spot_flip_label = " 🔄매수전환" if spot.get("flip_buy") else " 🔄매도전환" if spot.get("flip_sell") else ""
        st.markdown(f"""
<div style="background:#1a2744;border:2px solid {spot_color};padding:10px 14px;border-radius:8px;margin-bottom:8px;">
  <div style="display:flex;justify-content:space-between;">
    <span style="font-size:12px;font-weight:bold;">🏛️ 코스피 현물</span>
    <span style="color:{spot_color};font-size:11px;">{spot_today:+,.0f}주{spot_flip_label}</span>
  </div>
  <div style="font-size:10px;color:#64748b;margin-top:3px;">
    전일: {spot.get('prev',0):+,.0f}주 &nbsp;|&nbsp; 3일누적: {spot.get('d3',0):+,.0f}주
  </div>
</div>""", unsafe_allow_html=True)

        st.caption("외국인 ETF 수급 상세")
        for key, d in etf_data.items():
            if key == "spot_kospi": continue  # 현물은 위에서 표시
            if not d: continue
            today = d.get("today", 0)
            prev  = d.get("prev", 0)
            d3    = d.get("d3", 0)
            flip_b = d.get("flip_buy", False)
            flip_s = d.get("flip_sell", False)
            color = "#10b981" if today > 0 else "#ef4444" if today < 0 else "#64748b"
            flip_label = " 🔄매수전환" if flip_b else " 🔄매도전환" if flip_s else ""
            st.markdown(f"""
<div style="background:#1e293b;padding:10px 14px;border-radius:8px;margin-bottom:5px;
            border-left:3px solid {color};">
  <div style="display:flex;justify-content:space-between;">
    <span style="font-size:12px;font-weight:bold;">{d.get('name','')}</span>
    <span style="color:{color};font-size:11px;">{today:+,.0f}주{flip_label}</span>
  </div>
  <div style="font-size:10px;color:#64748b;margin-top:3px;">
    전일: {prev:+,.0f}주 &nbsp;|&nbsp; 3일누적: {d3:+,.0f}주
  </div>
</div>""", unsafe_allow_html=True)

        st.markdown("---")
        st.caption("""**B안 활용법** — FESI는 개별 종목 진입 타이밍 판단 보조지표
🟢 강세 신호 → 국내 TOP5 스캐너 PASS 종목 진입 고려
🔴 약세 신호 → 신규매수 자제, 기존 보유 종목 관리 집중
🟡 중립 → 점수 최상위 종목만 소량 진입
📊 레버리지/인버스 ETF 직접 매매는 참고용 — 진입 신중""")


