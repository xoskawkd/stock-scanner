"""
Tae Scanner — 퀀트 폭등 예측 엔진 (v7)
==================================
v6 기반, v7 변경사항:

1. [기준] THRESHOLDS — 추격 가드 소폭 완화 (예측 감도 개선)
2. [구조] quant_predict() — 셋업(예측형)/트리거(초기확인) 완전 분리
   - 셋업: S1 변동성수축 / S2 거래량눌림 / S3 추세눌림목  → 단계별 가점
   - 트리거: 거래량증가(S2T) / S4 RSI다이버전스 / S5 반등캔들 → 가점 전용
3. [게이트] 통과 조건: 셋업 ≥1 + 점수 ≥38 (2-of-3 강제 → 완화)
4. [등급] 셋업강도 + 트리거유무로 계층화 (A+/A/B+/B/C)
5. [캡션] 실제 로직과 일치하도록 수정
6. [라벨] S_LABELS — S2 의미 변경 반영
"""

import streamlit as st
import pyupbit
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
except Exception:
    ZoneInfo = None

# ============================================================
# ★ API 키 설정
# ============================================================
KRX_API_KEY     = "08810EEE8F724ED7BB7D35A2B79190956C2FFCB7"   # ← data.krx.co.kr AUTH_KEY (KIS 실패 시 fallback)
FINNHUB_API_KEY = "e196a49253d0408cadf883e01f6b78d9"   # ← Finnhub 키 (없으면 yfinance)

# 한국투자증권 (KIS) Open API
KIS_APP_KEY    = "PSmEd1aPpxC4GtQ5k23MW8iI4IdvwKRhnXiF"    # ← 한국투자증권 앱키
KIS_APP_SECRET = "Pvmawb5cs8oIDi6KEgMbqx+115iKoUjKdMMj2DmcmdjyPmMtordm2EEfUoA+q15+23cUg2/7piYXimu+O42ZCS/tpJ2YpNAraf8W6TRV2cuwAgToJEWs8xBNHJeqFob6JUiVFhLbSGObuh1Z9ziXISrXBIF61+l/ZWoULdaIqAdYcjV2EIA="    # ← 한국투자증권 앱시크릿
KIS_ACCOUNT_NO = ""    # ← 계좌번호 (XXXXXXXXXX-XX, 시세조회는 불필요)
KIS_IS_REAL    = True  # ← True: 실전, False: 모의투자
KIS_BASE_URL   = "https://openapi.koreainvestment.com:9443" if KIS_IS_REAL else "https://openapivts.koreainvestment.com:29443"

# KIS 토큰 (앱 시작 시 자동 발급)
_KIS_TOKEN = {"access_token": "", "expires": None}

# ============================================================
# ★ 스캔/필터 튜닝값
# ============================================================
KR_SCAN_TOP_N     = 300
CRYPTO_SCAN_LIMIT = 80

# ============================================================
# [v7] THRESHOLDS — 추격 가드는 소폭만 완화, 감도 개선 중심
# ============================================================
THRESHOLDS = {
    "KR":     {"min_vol": 50_000,  "max_rsi": 83, "max_gain5": 0.18,
               "max_ma20_dev": 1.18, "max_hi60": 0.98, "min_pass_score": 38},
    "US":     {"min_vol": 500_000, "max_rsi": 83, "max_gain5": 0.18,
               "max_ma20_dev": 1.18, "max_hi60": 0.98, "min_pass_score": 38},
    "CRYPTO": {"min_value": 1_000_000_000, "max_rsi": 83, "max_gain5": 0.30,
               "max_ma20_dev": 1.25, "max_hi60": 0.98, "min_pass_score": 38},
}

# ============================================================
# 0-A. 시그널 가중치 외부 파일 관리 (weights.json)
# ============================================================
WEIGHTS_FILE = "weights.json"

DEFAULT_WEIGHTS = {
    # 백테스트 2회 반복 결과 반영 (KRX 199종목, 149,653봉, 2023~)
    # S1/S2/S3/S5 모두 음수 리프트 일관 확인 → 단계적 하향
    # S4 리프트 방향 미확인 (p>0.05) → 유지
    "s1_strong": 12, "s1_weak": 6,   # 18→12 (리프트 -0.12% 2회 확인)
    "s2_strong": 9,  "s2_weak": 5,   # 14→9  (리프트 -0.12% 2회 확인)
    "s2t_strong": 5, "s2t_weak": 3,  # 유지
    "s3_strong": 17, "s3_weak": 10,  # 25→17 (리프트 -0.14% 2회 확인)
    "s4": 12,                         # 유지 (p=0.325, 방향 미확인)
    "s5": 3,                          # 5→3   (리프트 -0.18% 2회 확인)
    "rsi_good": 5, "rsi_oversold": 3, "rsi_extreme": 2,
    "min_pass_score": 38,
    "_updated": "2차 백테스트 반영",
    "_note": "KRX 199종목 149,653봉 2회 검증"
}

def load_weights() -> dict:
    if os.path.exists(WEIGHTS_FILE):
        try:
            with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
                w = json.load(f)
            # 누락된 키는 기본값으로 채움
            for k, v in DEFAULT_WEIGHTS.items():
                if k not in w:
                    w[k] = v
            return w
        except:
            pass
    return DEFAULT_WEIGHTS.copy()

def save_weights(w: dict):
    w["_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
        json.dump(w, f, ensure_ascii=False, indent=2)

# 앱 시작 시 가중치 로딩
W = load_weights()

# ============================================================
# 0. 포트폴리오 영구 저장
# ============================================================
DATA_FILE = "portfolio.json"

def load_portfolio():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []

def save_portfolio(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

# ============================================================
# 1. 페이지 초기화
# ============================================================
st.set_page_config(page_title="Tae Scanner", layout="wide")
if "my_portfolio" not in st.session_state:
    st.session_state.my_portfolio = load_portfolio()

# ============================================================
# 2. 국내 기준가 — KRX 공식 OpenAPI
# ============================================================
@st.cache_data(ttl=600, show_spinner=False)
def get_krx_daily_snapshot() -> tuple:
    if not KRX_API_KEY:
        return pd.DataFrame(), ""
    d = datetime.now()
    for _ in range(5):
        ds = d.strftime("%Y%m%d")
        try:
            res = requests.get(
                "http://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
                params={"basDd": ds},
                headers={"AUTH_KEY": KRX_API_KEY},
                timeout=6,
            ).json()
            rows = res.get("OutBlock_1", [])
            if rows:
                return pd.DataFrame(rows), ds
        except:
            pass
        d -= timedelta(days=1)
    return pd.DataFrame(), ""


def get_krx_price(code: str) -> tuple:
    if not KRX_API_KEY:
        return 0.0, 0.0, "KRX키없음"
    snap, basdd = get_krx_daily_snapshot()
    if snap.empty:
        return 0.0, 0.0, "KRX응답없음"
    key_col = "ISU_SRT_CD" if "ISU_SRT_CD" in snap.columns else None
    if key_col is None:
        return 0.0, 0.0, "KRX필드불일치"
    row = snap[snap[key_col] == code]
    if row.empty:
        return 0.0, 0.0, "KRX미발견"
    try:
        price = float(str(row.iloc[0].get("TDD_CLSPRC", "0")).replace(",", ""))
        vol   = float(str(row.iloc[0].get("ACC_TRDVOL", "0")).replace(",", ""))
        return price, vol, f"KRX확정종가({basdd})"
    except:
        return 0.0, 0.0, "KRX파싱오류"


@st.cache_data(ttl=30, show_spinner=False)
def get_kr_price_with_fallback(code: str) -> tuple:
    """KIS → KRX → yfinance → OHLCV 안전망"""
    # KIS 우선 (가장 빠르고 정확)
    if KIS_APP_KEY and KIS_APP_SECRET:
        price, src = kis_get_price(code)
        if price > 0:
            return price, 0.0, src

    # KRX fallback
    price, vol, src = get_krx_price(code)
    if price > 0:
        return price, vol, src

    try:
        suffix = ".KS" if code[:2] in ["00","01","02","03","04","05","06"] else ".KQ"
        t  = yf.Ticker(f"{code}{suffix}")
        p  = getattr(t.fast_info, "last_price", 0)
        p  = float(p) if p and float(p) > 0 else 0.0
        if p > 0:
            return p, 0.0, "yfinance"
        df = t.history(period="1d", interval="5m")
        if not df.empty:
            return float(df["Close"].iloc[-1]), float(df["Volume"].iloc[-1]), "yfinance"
    except:
        pass

    df_ohlcv = load_ohlcv_kr(code)
    if df_ohlcv is not None and not df_ohlcv.empty:
        return float(df_ohlcv["close"].iloc[-1]), float(df_ohlcv["volume"].iloc[-1]), "OHLCV종가(안전망)"
    return 0.0, 0.0, "실패"


# ============================================================
# 3. 해외 가격
# ============================================================
def is_us_market_open() -> bool:
    if ZoneInfo is None:
        return True
    try:
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except:
        return True
    if now_et.weekday() >= 5:
        return False
    open_t  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now_et <= close_t


def _fh_fetch_raw(ticker: str) -> dict:
    if not FINNHUB_API_KEY:
        return {"c": 0.0, "pc": 0.0}
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=4,
        ).json()
        return {"c": float(r.get("c", 0) or 0), "pc": float(r.get("pc", 0) or 0)}
    except:
        return {"c": 0.0, "pc": 0.0}


def _yf_fresh_price(ticker: str) -> tuple:
    """
    정규장 가격 반환 (스캔용 — 장외 제외)
    """
    try:
        t = yf.Ticker(ticker)
        try:
            df_min = t.history(period="1d", interval="1m")
            if not df_min.empty:
                last_close = df_min["Close"].dropna()
                if not last_close.empty:
                    p = float(last_close.iloc[-1])
                    if p > 0:
                        return p, "yfinance(1분봉)"
        except: pass
        try:
            p = getattr(t.fast_info, "last_price", 0)
            if p and float(p) > 0:
                return float(p), "yfinance(실시간)"
        except: pass
        try:
            df_day = t.history(period="5d", interval="1d")
            if not df_day.empty:
                last_close = df_day["Close"].dropna()
                if not last_close.empty and float(last_close.iloc[-1]) > 0:
                    last_idx = df_day.index[-1]
                    try:
                        last_date = last_idx.tz_localize(None) if last_idx.tzinfo else last_idx
                        days_old = (datetime.now() - last_date.to_pydatetime()).days
                    except: days_old = 0
                    if days_old > 3:
                        return None, f"yfinance(일봉{days_old}일전·stale차단)"
                    return float(last_close.iloc[-1]), "yfinance(일봉종가)"
        except: pass
    except: pass
    return None, "실패"


def _yf_prepost_price(ticker: str) -> tuple:
    """
    장외 가격 반환 (포트폴리오 전용 — prepost=True)
    반환: (장외가격, 장외구분, 정규장종가)
    """
    try:
        t = yf.Ticker(ticker)

        # 정규장 종가 (1분봉 기준)
        regular_price = 0.0
        try:
            df_reg = t.history(period="1d", interval="1m", prepost=False)
            if not df_reg.empty:
                lc = df_reg["Close"].dropna()
                if not lc.empty: regular_price = float(lc.iloc[-1])
        except: pass

        # 장외 포함 1분봉
        try:
            df_pp = t.history(period="1d", interval="1m", prepost=True)
            if not df_pp.empty and len(df_pp) > 0:
                last_price = float(df_pp["Close"].dropna().iloc[-1])
                last_time  = df_pp.index[-1]

                # 시간대 판별
                try:
                    if ZoneInfo:
                        now_et = datetime.now(ZoneInfo("America/New_York"))
                        reg_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
                        reg_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
                        pre_start = now_et.replace(hour=4,  minute=0,  second=0, microsecond=0)
                        aft_end   = now_et.replace(hour=20, minute=0,  second=0, microsecond=0)
                        if reg_open <= now_et <= reg_close:
                            session = "🏛️ 정규장"
                        elif pre_start <= now_et < reg_open:
                            session = "🌅 프리마켓"
                        elif reg_close < now_et <= aft_end:
                            session = "🌙 애프터마켓"
                        else:
                            session = "🌙 애프터마켓(연장)"
                    else:
                        session = "장외"
                except: session = "장외"

                if last_price > 0:
                    return last_price, session, regular_price
        except: pass

    except: pass
    return 0.0, "실패", 0.0


@st.cache_data(ttl=60, show_spinner=False)
def get_us_price(ticker: str) -> tuple:
    market_open = is_us_market_open()
    q = _fh_fetch_raw(ticker)
    c, pc = q["c"], q["pc"]

    if market_open and c > 0:
        return c, "Finnhub(정규장)"
    if not market_open and pc > 0:
        return pc, "Finnhub(전일정규장종가)"
    if c > 0:
        return c, "Finnhub(시간외·참고용)"

    price, src = _yf_fresh_price(ticker)
    if price is not None and price > 0:
        return price, src

    return 0.0, src if price is None else "실패"


@st.cache_data(ttl=60, show_spinner=False)
def get_us_price_batch(tickers: tuple) -> dict:
    with ThreadPoolExecutor(max_workers=min(len(tickers), 10)) as ex:
        futs = {t: ex.submit(get_us_price, t) for t in tickers}
        return {t: fut.result() for t, fut in futs.items()}


# ============================================================
# 4. OHLCV 로더
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def load_krx_listing():
    return fdr.StockListing("KRX")

@st.cache_data(ttl=600, show_spinner=False)
def load_ohlcv_kr(code: str) -> pd.DataFrame | None:
    try:
        df = fdr.DataReader(code, start="2024-01-01")
        if df is not None and len(df) >= 60:
            df.columns = [c.lower() for c in df.columns]
            return df
    except:
        pass
    return None

@st.cache_data(ttl=600, show_spinner=False)
def load_ohlcv_us(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.Ticker(ticker).history(period="1y")
        if not df.empty and len(df) >= 60:
            df.columns = [c.lower() for c in df.columns]
            return df
    except:
        pass
    try:
        df = fdr.DataReader(ticker, start="2024-01-01")
        if df is not None and len(df) >= 60:
            df.columns = [c.lower() for c in df.columns]
            return df
    except:
        pass
    return None

# ============================================================
# 5. 마켓 현황
# ============================================================
@st.cache_data(ttl=1800, show_spinner=False)
def get_market_status():
    try:
        fg  = requests.get("https://api.alternative.me/fng/?limit=1", timeout=3).json()
        val = fg["data"][0]["value"]
        txt = ("극단적 탐욕" if int(val)>=75 else "탐욕" if int(val)>=60
               else "중립" if int(val)>=40 else "공포" if int(val)>=25 else "극단적 공포")
        usd = yf.Ticker("KRW=X").history(period="1d")["Close"].iloc[-1]
        return val, txt, f"{usd:,.2f}"
    except:
        return "50", "중립", "1,350.00"

# ============================================================
# 5-B. 한국투자증권 (KIS) API
# ============================================================
def kis_get_token() -> str:
    """KIS 접근토큰 발급 (1일 유효, 캐시)"""
    global _KIS_TOKEN
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        return ""
    now = datetime.now()
    if _KIS_TOKEN["access_token"] and _KIS_TOKEN["expires"] and now < _KIS_TOKEN["expires"]:
        return _KIS_TOKEN["access_token"]
    try:
        res = requests.post(
            f"{KIS_BASE_URL}/oauth2/tokenP",
            json={"grant_type": "client_credentials",
                  "appkey": KIS_APP_KEY,
                  "appsecret": KIS_APP_SECRET},
            timeout=5,
        ).json()
        token = res.get("access_token", "")
        if token:
            _KIS_TOKEN["access_token"] = token
            _KIS_TOKEN["expires"] = now + timedelta(hours=23)
        return token
    except:
        return ""


@st.cache_data(ttl=30, show_spinner=False)
def kis_get_price(code: str) -> tuple:
    """KIS 현재가 조회 (국내 주식)"""
    token = kis_get_token()
    if not token:
        return 0.0, "KIS토큰없음"
    try:
        res = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "FHKST01010100",
            },
            timeout=5,
        ).json()
        output = res.get("output", {})
        price  = float(output.get("stck_prpr", 0) or 0)
        return price, "KIS(실시간)"
    except:
        return 0.0, "KIS실패"


@st.cache_data(ttl=1800, show_spinner=False)
def kis_get_investor(code: str) -> dict:
    """
    KIS 투자자별 매매동향 (외국인/기관/개인)
    """
    token = kis_get_token()
    if not token:
        return {"ok": False, "reason": "KIS토큰없음"}
    try:
        res = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor",
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "FHKST01010900",
            },
            timeout=5,
        ).json()
        output = res.get("output", [])
        if not output:
            return {"ok": False, "reason": "KIS데이터없음"}

        result = {"ok": True, "외국인": 0, "기관": 0, "개인": 0, "연기금": 0,
                  "외국인비율": 0.0, "기관비율": 0.0}
        total = 0
        for row in output:
            inv  = row.get("invst_nm", "")
            try: net = int(row.get("netbuy_qty", 0) or 0)
            except: net = 0
            try: buy = int(row.get("buy_qty", 0) or 0)
            except: buy = 0
            total += buy

            if "외국인" in inv:   result["외국인"] = net
            elif "기관계" in inv: result["기관"]   = net
            elif "개인" in inv:   result["개인"]   = net
            elif "연기금" in inv: result["연기금"] = net

        if total > 0:
            result["외국인비율"] = result["외국인"] / total * 100
            result["기관비율"]   = result["기관"]   / total * 100
        return result
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@st.cache_data(ttl=1800, show_spinner=False)
def kis_get_investor_trend(code: str, days: int = 5) -> list:
    """KIS 최근 N일 수급 트렌드"""
    token = kis_get_token()
    if not token:
        return []
    try:
        end_dt   = datetime.now().strftime("%Y%m%d")
        start_dt = (datetime.now() - timedelta(days=days*2)).strftime("%Y%m%d")
        res = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-investor",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": code,
                "fid_begin_dt": start_dt,
                "fid_end_dt": end_dt,
            },
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "FHKST01010600",
            },
            timeout=5,
        ).json()
        rows = res.get("output", [])
        trend = []
        for row in rows[:days]:
            try:
                trend.append({
                    "ok": True,
                    "date": row.get("stck_bsop_date", ""),
                    "외국인": int(row.get("frgn_ntby_qty", 0) or 0),
                    "기관":   int(row.get("orgn_ntby_qty", 0) or 0),
                    "개인":   int(row.get("prsn_ntby_qty", 0) or 0),
                })
            except: pass
        return trend
    except:
        return []


# ============================================================
# 5-C. KRX 투자자별 수급 데이터 (KIS 실패 시 fallback)
# ============================================================
@st.cache_data(ttl=1800, show_spinner=False)
def get_krx_investor_data(code: str, date_str: str = "") -> dict:
    """
    KRX 투자자별 매매동향 조회
    반환: {외국인순매수, 기관순매수, 개인순매수, 연기금순매수, 날짜}
    """
    if not KRX_API_KEY:
        return {"ok": False, "reason": "KRX키없음"}

    # 날짜 없으면 오늘/어제
    if not date_str:
        d = datetime.now()
        for _ in range(5):
            date_str = d.strftime("%Y%m%d")
            try:
                res = requests.get(
                    "http://data-dbg.krx.co.kr/svc/apis/sto/invst_bydd_trd",
                    params={"basDd": date_str, "isuCd": code},
                    headers={"AUTH_KEY": KRX_API_KEY},
                    timeout=6,
                ).json()
                rows = res.get("OutBlock_1", [])
                if rows:
                    break
            except:
                rows = []
            d -= timedelta(days=1)
    else:
        try:
            res = requests.get(
                "http://data-dbg.krx.co.kr/svc/apis/sto/invst_bydd_trd",
                params={"basDd": date_str, "isuCd": code},
                headers={"AUTH_KEY": KRX_API_KEY},
                timeout=6,
            ).json()
            rows = res.get("OutBlock_1", [])
        except:
            return {"ok": False, "reason": "API오류"}

    if not rows:
        return {"ok": False, "reason": "데이터없음"}

    result = {"ok": True, "date": date_str,
              "외국인": 0, "기관": 0, "개인": 0, "연기금": 0,
              "외국인비율": 0.0, "기관비율": 0.0}
    total_vol = 0

    for row in rows:
        inv  = row.get("INVST_NM", "")
        net  = 0
        try: net = int(str(row.get("NETBUY_TRDVAL", "0")).replace(",", "").replace("-", "").strip())
            # 부호 처리
        except: pass
        try:
            net_signed = int(str(row.get("NETBUY_TRDVAL", "0")).replace(",", ""))
        except: net_signed = 0

        if "외국인" in inv:   result["외국인"] = net_signed
        elif "기관계" in inv: result["기관"]   = net_signed
        elif "개인" in inv:   result["개인"]   = net_signed
        elif "연기금" in inv: result["연기금"] = net_signed

        # 전체 거래대금 합산 (비율 계산용)
        try:
            buy = int(str(row.get("BUY_TRDVAL", "0")).replace(",", ""))
            total_vol += buy
        except: pass

    # 비율 계산
    if total_vol > 0:
        result["외국인비율"] = result["외국인"] / total_vol * 100
        result["기관비율"]   = result["기관"]   / total_vol * 100

    return result


@st.cache_data(ttl=1800, show_spinner=False)
def get_krx_investor_trend(code: str, days: int = 5) -> list:
    """
    최근 N일 수급 트렌드
    반환: [{date, 외국인, 기관, 개인}, ...]
    """
    if not KRX_API_KEY:
        return []

    trend = []
    d = datetime.now()
    count = 0
    while count < days * 2 and len(trend) < days:
        date_str = d.strftime("%Y%m%d")
        data = get_krx_investor_data(code, date_str)
        if data.get("ok"):
            trend.append(data)
            count += 1
        d -= timedelta(days=1)
        count += 1

    return trend[:days]


def calc_supply_signal(code: str) -> dict:
    """
    수급 신호 종합 계산 — KIS 우선, KRX fallback
    반환: {판단, 색상, 외국인연속, 기관전환, 상세}
    """
    # KIS 우선
    if KIS_APP_KEY and KIS_APP_SECRET:
        trend = kis_get_investor_trend(code, days=5)
        if not trend:
            trend = get_krx_investor_trend(code, days=5)
    else:
        trend = get_krx_investor_trend(code, days=5)

    if not trend:
        return {"ok": False, "reason": "수급데이터없음"}

    today = trend[0] if trend else {}
    fore_today  = today.get("외국인", 0)
    inst_today  = today.get("기관",   0)
    fore_ratio  = today.get("외국인비율", 0)
    inst_ratio  = today.get("기관비율",  0)

    # 외국인 연속 순매수 일수
    fore_streak = 0
    for t in trend:
        if t.get("외국인", 0) > 0: fore_streak += 1
        else: break

    # 기관 오늘 전환 (어제 매도 → 오늘 매수)
    inst_reversal = False
    if len(trend) >= 2:
        inst_reversal = (trend[0].get("기관", 0) > 0 and
                         trend[1].get("기관", 0) <= 0)

    # 연기금 순매수
    pension_buy = today.get("연기금", 0) > 0

    # 종합 판단
    signals = []
    score = 0

    if fore_today > 0:
        signals.append(f"외국인 순매수 ({fore_ratio:+.1f}%)")
        score += 2
        if fore_streak >= 3:
            signals.append(f"외국인 {fore_streak}일 연속 순매수 ★")
            score += 3
    elif fore_today < 0:
        signals.append(f"외국인 순매도 ({fore_ratio:+.1f}%)")
        score -= 2

    if inst_today > 0:
        signals.append(f"기관 순매수")
        score += 2
        if inst_reversal:
            signals.append("기관 매도→매수 전환 ★")
            score += 2
    elif inst_today < 0:
        signals.append(f"기관 순매도")
        score -= 1

    if pension_buy:
        signals.append("연기금 순매수")
        score += 1

    if score >= 5:
        verdict = "🔥 강한 매수세"; color = "#10b981"
    elif score >= 3:
        verdict = "🟢 매수세 우위"; color = "#10b981"
    elif score >= 1:
        verdict = "🟡 중립"; color = "#f59e0b"
    elif score <= -2:
        verdict = "🔴 매도세 우위"; color = "#ef4444"
    else:
        verdict = "⚪ 수급 중립"; color = "#64748b"

    return {
        "ok": True,
        "verdict": verdict,
        "color": color,
        "score": score,
        "signals": signals,
        "fore_streak": fore_streak,
        "fore_today": fore_today,
        "inst_today": inst_today,
        "fore_ratio": fore_ratio,
        "inst_ratio": inst_ratio,
        "date": today.get("date", ""),
    }


# ============================================================
# 6. 공통 유틸
# ============================================================
def _safe_float(val, default=0.0) -> float:
    try:
        v = float(val)
        return v if np.isfinite(v) else default
    except:
        return default

# ============================================================
# 7. ★ 퀀트 폭등 예측 엔진 (v7)
#    셋업(예측형) / 트리거(초기확인) 완전 분리
# ============================================================
def quant_predict(df: pd.DataFrame, market: str = "KR") -> dict:
    OUT = {
        "score": 0, "grade": "F", "signals": [],
        "pass": False, "buy_min": 0.0, "buy_max": 0.0,
        "target": 0.0, "stop": 0.0,
        "rsi": 50.0, "current": 0.0,
        "s1": False, "s2": False, "s3": False, "s4": False, "s5": False,
    }
    th = THRESHOLDS.get(market, THRESHOLDS["KR"])
    try:
        if df is None or len(df) < 60:
            OUT["signals"].append("❌ 데이터 부족")
            return OUT

        df.columns = [c.lower() for c in df.columns]
        cl = df["close"].astype(float)
        hi = df["high"].astype(float)
        lo = df["low"].astype(float)
        vo = df["volume"].astype(float)

        current = _safe_float(cl.iloc[-1])
        if current <= 0:
            valid = cl[cl > 0]
            current = _safe_float(valid.iloc[-1]) if not valid.empty else 0.0
        OUT["current"] = current

        rejected = False

        # ── 추격 방지 가드 (이미 움직인 종목 거부) ──
        if market == "CRYPTO":
            avg_value = _safe_float((vo * cl).rolling(20).mean().iloc[-1])
            if avg_value < th["min_value"]:
                OUT["signals"].append(f"❌ 유동성 부족 (일평균 거래대금 {avg_value/1e8:.1f}억원)")
                rejected = True
        else:
            avg_vol = _safe_float(vo.rolling(20).mean().iloc[-1])
            if avg_vol < th["min_vol"]:
                OUT["signals"].append(f"❌ 유동성 부족 (일평균 {int(avg_vol):,}주)")
                rejected = True

        ma20 = _safe_float(cl.rolling(20).mean().iloc[-1])
        if ma20 > 0 and current > 0 and current > ma20 * th["max_ma20_dev"]:
            OUT["signals"].append(f"❌ 이미 급등 (MA20 대비 +{(th['max_ma20_dev']-1)*100:.0f}% 초과)")
            rejected = True

        p5ago = _safe_float(cl.iloc[-6]) if len(cl) >= 6 else current
        gain5 = (current - p5ago) / p5ago if p5ago > 0 else 0
        if gain5 > th["max_gain5"]:
            OUT["signals"].append(f"❌ 5일 수익 {gain5*100:.1f}% — 이미 터진 종목")
            rejected = True


        hi60 = _safe_float(cl.rolling(60).max().iloc[-1])
        if hi60 > 0 and current > 0 and current >= hi60 * th["max_hi60"]:
            OUT["signals"].append(f"❌ 60일 고점권 ({th['max_hi60']*100:.0f}% 이상)")
            rejected = True

        # ── MA ──
        ma5  = _safe_float(cl.rolling(5).mean().iloc[-1])
        ma10 = _safe_float(cl.rolling(10).mean().iloc[-1])
        ma60 = _safe_float(cl.rolling(60).mean().iloc[-1])

        # ── RSI ──
        delta  = cl.diff()
        gain_s = delta.clip(lower=0).rolling(14).mean()
        loss_s = (-delta.clip(upper=0)).rolling(14).mean()
        rsi_s  = 100 - 100 / (1 + gain_s / loss_s.replace(0, np.nan))
        rsi    = _safe_float(rsi_s.iloc[-1], default=50.0)
        OUT["rsi"] = rsi
        if rsi > th["max_rsi"]:
            OUT["signals"].append(f"❌ RSI 과열 ({rsi:.1f})")
            rejected = True

        # ============================================================
        # 신호 체계
        #  [셋업 — 예측형]  S1 변동성수축 / S2 거래량눌림 / S3 추세눌림목
        #  [트리거 — 초기확인] S2T 거래량증가 / S4 RSI다이버전스 / S5 반등캔들
        #
        #  통과 게이트: (not rejected) AND (셋업 ≥1) AND (score ≥ threshold)
        # ============================================================
        score        = 0
        setup_hits   = 0   # 셋업 발화 수
        setup_strong = 0   # 강한 셋업 수
        trigger_hits = 0   # 트리거 발화 수

        # ── [S1] BB수축 — 가산점 전용 (백테스트: 리프트 -0.11%, 통과조건 제외) ──
        bb_std   = cl.rolling(20).std()
        bb_mean  = cl.rolling(20).mean().replace(0, np.nan)
        bb_width = (bb_std * 2) / bb_mean
        bw_now   = _safe_float(bb_width.iloc[-1])
        bw_avg   = _safe_float(bb_width.rolling(20).mean().iloc[-1])
        s1 = False
        if bw_avg > 0 and bw_now > 0:
            if bw_now < bw_avg * 0.80:
                s1 = True; setup_hits += 1; setup_strong += 1; score += W['s1_strong']
                OUT["signals"].append(f"✅ [S1] 변동성 강수축 ({bw_now:.3f} < {bw_avg*0.80:.3f})")
            elif bw_now < bw_avg * 0.92:
                s1 = True; setup_hits += 1; score += W['s1_weak']
                OUT["signals"].append(f"🔶 [S1] 변동성 수축 진행 ({bw_now:.3f} < {bw_avg*0.92:.3f})")
            else:
                OUT["signals"].append("⬜ [S1] 변동성 수축 없음")
        else:
            OUT["signals"].append("⬜ [S1] 변동성 계산 불가")
        OUT["s1"] = s1

        # ── [S2] 거래량 눌림 — 가산점 전용 (백테스트: 리프트 -0.12%, 통과조건 제외) ──
        vol_ma5  = _safe_float(vo.rolling(5).mean().iloc[-1])
        vol_ma20 = _safe_float(vo.rolling(20).mean().iloc[-1])
        vol_now  = _safe_float(vo.iloc[-1])
        s2 = False
        if vol_ma20 > 0 and vol_ma5 < vol_ma20 * 0.65:
            s2 = True; setup_hits += 1; setup_strong += 1; score += W['s2_strong']
            OUT["signals"].append(f"✅ [S2] 거래량 강한 눌림 ({vol_ma5/vol_ma20*100:.0f}% of MA20)")
        elif vol_ma20 > 0 and vol_ma5 < vol_ma20 * 0.80:
            s2 = True; setup_hits += 1; score += W['s2_weak']
            OUT["signals"].append(f"🔶 [S2] 거래량 눌림 ({vol_ma5/vol_ma20*100:.0f}% of MA20)")
        else:
            OUT["signals"].append(f"⬜ [S2] 거래량 눌림 없음 ({vol_ma5/vol_ma20*100:.0f}% of MA20)")
        OUT["s2"] = s2
        if vol_ma5 > 0 and vol_now > vol_ma5 * 2.0:
            trigger_hits += 1; score += W['s2t_strong']
            OUT["signals"].append(f"➕ [S2T] 거래량 폭발 ({vol_now/vol_ma5*100:.0f}% of MA5)")
        elif vol_ma5 > 0 and vol_now > vol_ma5 * 1.50:
            trigger_hits += 1; score += W['s2t_weak']
            OUT["signals"].append(f"➕ [S2T] 거래량 증가 ({vol_now/vol_ma5*100:.0f}% of MA5)")

        # ── [S3] 정배열+MA20 눌림목 — 핵심 진입 신호 (백테스트: 조합시 양수) ──
        aligned_full = ma5 > 0 and ma20 > 0 and ma60 > 0 and ma5 > ma20 > ma60
        midterm_up   = ma20 > 0 and ma60 > 0 and ma20 > ma60
        near_ma20_5  = ma20 > 0 and current > 0 and abs(current - ma20) / ma20 <= 0.05
        s3 = False
        if aligned_full and near_ma20_5:
            s3 = True; setup_hits += 1; setup_strong += 1; score += W['s3_strong']
            OUT["signals"].append("✅ [S3] 완전 정배열 + MA20 눌림목 ★핵심진입")
        elif midterm_up and near_ma20_5:
            s3 = True; setup_hits += 1; score += W['s3_weak']
            OUT["signals"].append("🔶 [S3] 중기 상승추세 + MA20 눌림목")
        else:
            near_str = f"{abs(current-ma20)/ma20*100:.1f}%" if ma20 > 0 and current > 0 else "-"
            OUT["signals"].append(f"⬜ [S3] 추세 눌림목 없음 (MA20 이격 {near_str})")
        OUT["s3"] = s3

        # ── [S4] RSI 강세 다이버전스 — 핵심 진입 신호 (백테스트: 유일한 양수 리프트 조합) ──
        s4 = False
        try:
            if len(cl) >= 20:
                pw = cl.iloc[-20:]
                rw = rsi_s.iloc[-20:]
                i1 = pw.iloc[:10].idxmin()
                i2 = pw.iloc[10:].idxmin()
                p_low1 = _safe_float(pw.loc[i1])
                p_low2 = _safe_float(pw.loc[i2])
                r_low1 = _safe_float(rw.loc[i1], 50.0)
                r_low2 = _safe_float(rw.loc[i2], 50.0)
                s4 = (p_low2 < p_low1) and (r_low2 > r_low1 + 3)
                if s4:
                    trigger_hits += 1; score += W['s4']
                    OUT["signals"].append(f"✅ [S4] RSI 강세 다이버전스 (저점↓ RSI↑)")
                else:
                    OUT["signals"].append("⬜ [S4] RSI 다이버전스 없음")
            else:
                OUT["signals"].append("⬜ [S4] RSI 다이버전스 데이터 부족")
        except:
            OUT["signals"].append("⬜ [S4] RSI 다이버전스 계산 실패")
        OUT["s4"] = s4

        # ── [S5] 반등 캔들 패턴 (트리거) — NaN/index 방어 유지 ──
        s5 = False
        try:
            if "open" not in df.columns or len(df) < 2:
                OUT["signals"].append("⬜ [S5] 반등 캔들 데이터 부족")
            else:
                op = df["open"].astype(float)
                o1, c1_v = _safe_float(op.iloc[-1]),  _safe_float(cl.iloc[-1])
                h1, l1   = _safe_float(hi.iloc[-1]),   _safe_float(lo.iloc[-1])
                o2, c2_v = _safe_float(op.iloc[-2]),   _safe_float(cl.iloc[-2])
                if any(v == 0.0 for v in [o1, c1_v, h1, l1, o2, c2_v]):
                    OUT["signals"].append("⬜ [S5] 캔들 값 이상 (0 포함)")
                elif h1 < l1 or h1 < max(o1, c1_v) or l1 > min(o1, c1_v):
                    OUT["signals"].append("⬜ [S5] 캔들 OHLC 무결성 오류")
                else:
                    body  = abs(c1_v - o1)
                    lower = min(o1, c1_v) - l1
                    upper = h1 - max(o1, c1_v)
                    hammer   = body > 0 and lower > body * 2 and upper < body * 0.5
                    bull_rev = c2_v < o2 and c1_v > o1
                    s5 = hammer or bull_rev
                    if s5:
                        trigger_hits += 1; score += W['s5']
                        pat = "망치형" if hammer else "양봉전환"
                        OUT["signals"].append(f"✅ [S5] {pat} 캔들 — 단기 반등 신호")
                    else:
                        OUT["signals"].append("⬜ [S5] 반등 캔들 패턴 없음")
        except Exception as e5:
            OUT["s5"] = False
            OUT["signals"].append(f"⬜ [S5] 캔들 패턴 계산 실패 ({e5})")
        OUT["s5"] = s5

        # ── RSI 구간 보너스 ──
        if 40 <= rsi <= 55:
            score += W['rsi_good']
            OUT["signals"].append(f"✅ RSI 매수 구간 ({rsi:.1f})")
        elif 30 <= rsi < 40:
            score += W['rsi_oversold']
            OUT["signals"].append(f"🔶 RSI 과매도 ({rsi:.1f})")
        elif rsi < 30:
            score += W['rsi_extreme']
            OUT["signals"].append(f"🔶 RSI 극과매도 ({rsi:.1f})")
        else:
            OUT["signals"].append(f"⬜ RSI 보너스 없음 ({rsi:.1f})")

        # ── 목표가/손절가 — ATR + BB상단 (60일고점 제거) ──
        # 60일고점은 과거 고점이라 이상값 유발 → 완전 제거
        # 목표가 = min(BB상단, 현재가+ATR*2) — 현재 변동성 기반
        # 손절가 = 현재가 - ATR*1.5 (단, 최대 -7%)
        _atr_target = current * 1.08  # 기본값
        _atr_stop   = current * 0.93
        buy_low     = current * 0.97
        buy_high    = current * 1.02
        try:
            hi_col = df["high"].astype(float)
            lo_col = df["low"].astype(float)
            tr = pd.concat([
                hi_col - lo_col,
                (hi_col - cl.shift()).abs(),
                (lo_col - cl.shift()).abs()
            ], axis=1).max(axis=1)
            atr = _safe_float(tr.rolling(14).mean().iloc[-1])
            if atr <= 0 or current <= 0:
                raise ValueError("ATR 이상")

            # BB 상단
            std20  = _safe_float(cl.rolling(20).std().iloc[-1])
            bb_top = ma20 + std20 * 2 if ma20 > 0 and std20 > 0 else 0

            # 목표가 후보 두 개
            t_atr = current + atr * 2          # ATR 2배
            t_bb  = bb_top if bb_top > current else current + atr * 2

            # 둘 중 보수적인 값 (더 낮은 것) 선택
            _atr_target = min(t_atr, t_bb)

            # 범위 제한: +5% ~ +15%
            _atr_target = max(_atr_target, current * 1.05)
            _atr_target = min(_atr_target, current * 1.15)

            # 손절가: ATR 기반, 최대 -7%
            _atr_stop = current - atr * 1.5
            _atr_stop = max(_atr_stop, current * 0.93)  # 최대 -7%
            _atr_stop = min(_atr_stop, current * 0.97)  # 최소 -3%

            # 매수구간: ATR 기반
            buy_low  = max(current * 0.97, current - atr * 0.5)
            buy_high = min(current * 1.02, current + atr * 0.3)
        except:
            pass  # 기본값 유지

        OUT["buy_min"] = round(buy_low,  4)
        OUT["buy_max"] = round(buy_high, 4)
        # ★ 'ft' in dir()는 항상 True → locals() 사용 또는 명시적 변수로 처리
        try:
            OUT["target"] = round(_atr_target, 4)
            OUT["stop"]   = round(_atr_stop,   4)
        except:
            OUT["target"] = round(current * 1.08, 4)
            OUT["stop"]   = round(current * 0.93, 4)
        OUT["score"]   = int(score)

        # ── 통과 게이트 — 3차 백테스트 기반 재설계 ──
        # S3 AND S4 둘 다 필수
        # S3 OR S4 → 리프트 -0.33%p (2차 백테스트)
        # S3 AND S4 → 유일한 양수 방향 조합 (+0.10%, 승률 51.8%)
        core_pass = s3 and s4
        OUT["pass"] = (not rejected) and core_pass and (score >= W['min_pass_score'])

        # 변화감지 — 참고 표시만 (통과조건 아님)
        try:
            bbw_s  = (cl.rolling(20).std()*2)/cl.rolling(20).mean().replace(0,np.nan)
            bw_t   = _safe_float(bbw_s.iloc[-1]); bw_y=_safe_float(bbw_s.iloc[-2])
            bw_av  = _safe_float(bbw_s.rolling(20).mean().iloc[-1])
            bb_t   = bw_av>0 and bw_t>0 and bw_t<bw_av*0.85
            bb_y   = bw_av>0 and bw_y>0 and bw_y<bw_av*0.85
            vm5_t  = _safe_float(vo.rolling(5).mean().iloc[-1])
            vm5_y  = _safe_float(vo.rolling(5).mean().iloc[-2])
            vm20_v = _safe_float(vo.rolling(20).mean().iloc[-1])
            vd_t   = vm20_v>0 and vm5_t<vm20_v*0.65
            vd_y   = vm20_v>0 and vm5_y<vm20_v*0.65
            ma20_y = _safe_float(cl.rolling(20).mean().iloc[-2])
            cur_y  = _safe_float(cl.iloc[-2])
            n5_t   = ma20>0 and current>0 and abs(current-ma20)/ma20<=0.05
            n5_y   = ma20_y>0 and cur_y>0 and abs(cur_y-ma20_y)/ma20_y<=0.05
            up_t   = ma5>0 and ma20>0 and ma60>0 and ma20>ma60
            new_sigs=[]
            if bb_t and not bb_y:          new_sigs.append("BB수축시작")
            if vd_t and not vd_y:          new_sigs.append("거래량눌림시작")
            if n5_t and not n5_y and up_t: new_sigs.append("눌림목진입")
            existing=sum([bb_t,vd_t,n5_t and up_t])
            if len(new_sigs)>=1 and existing>=2:
                OUT["signals"].append(f"🔔 참고: {','.join(new_sigs)} 오늘 처음 감지")
        except:
            pass

        # ── 등급 ── 점수 기반 (셋업강도 보조 조건)
        if score >= 65 and setup_strong >= 1 and trigger_hits >= 1:
            grade = "A+"
        elif score >= 55 and setup_hits >= 1 and trigger_hits >= 1:
            grade = "A"
        elif score >= 45 and setup_hits >= 1:
            grade = "B+"
        elif score >= 38 and setup_hits >= 1:
            grade = "B"
        else:
            grade = "C"
        OUT["grade"] = grade

    except Exception as e:
        OUT["signals"].append(f"오류: {e}")
    return OUT


# ============================================================
# 8. 스캐너
# ============================================================
# ── 나스닥100 자동 로딩 (Wikipedia 스크래핑, API 키 불필요) ──
@st.cache_data(ttl=86400, show_spinner=False)
def load_us_tickers() -> list:
    """
    나스닥100 + S&P500 자동 로딩 (Wikipedia)
    실패 시 하드코딩 fallback
    """
    FALLBACK = [
        # 나스닥 대형주
        "NVDA","META","GOOGL","AMZN","MSFT","AMD","TSLA","AAPL","NFLX","AVGO",
        # 반도체
        "QCOM","MU","AMAT","LRCX","KLAC","MRVL","SMCI","ARM","INTC","TXN",
        # AI/클라우드
        "PLTR","CRM","SNOW","DDOG","MDB","NET","ZS","CRWD","PANW","OKTA",
        # 핀테크/성장
        "PYPL","SQ","SOFI","HOOD","UPST","AFRM","COIN","MSTR",
        # 바이오/헬스
        "HIMS","AXSM","MRNA","BNTX","REGN","VRTX","ISRG","DXCM",
        # 기타 성장
        "ASTS","RIVN","LCID","NIO","RBLX","U","DKNG","PENN",
        # S&P500 대형주
        "JPM","BAC","GS","MS","V","MA","BRK-B","JNJ","UNH","PFE",
        "XOM","CVX","LIN","APD","NEE","DUK","AMT","PLD","SPG",
        "WMT","COST","TGT","HD","LOW","MCD","SBUX","NKE","DIS",
        "BA","CAT","DE","HON","MMM","GE","RTX","LMT","NOC",
    ]
    tickers = []

    # 나스닥100 로딩
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            attrs={"id": "constituents"},
        )
        if tables:
            df_t = tables[0]
            col = next((c for c in df_t.columns
                       if "ticker" in c.lower() or "symbol" in c.lower()), None)
            if col:
                ndq = [t.replace(".", "-") for t in df_t[col].dropna().tolist()
                       if isinstance(t, str) and len(t) <= 6]
                tickers.extend(ndq)
    except: pass

    # S&P500 로딩
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        )
        if tables:
            df_t = tables[0]
            col = next((c for c in df_t.columns
                       if "ticker" in c.lower() or "symbol" in c.lower()), None)
            if col:
                sp5 = [t.replace(".", "-") for t in df_t[col].dropna().tolist()
                       if isinstance(t, str) and len(t) <= 5]
                tickers.extend(sp5)
    except: pass

    # 중복 제거 + 정렬
    seen = set(); unique = []
    for t in tickers:
        if t not in seen: seen.add(t); unique.append(t)

    if len(unique) >= 100:
        return unique[:500]  # 최대 500개
    return FALLBACK

US_WATCHLIST = load_us_tickers()

def summarize_skips(skips: list) -> dict:
    cnt = Counter()
    for s in skips:
        why = s.get("why", "기타")
        key = why.split("(")[0].strip().lstrip("❌🔶⬜ ").strip()
        cnt[key] += 1
    return dict(cnt.most_common())


@st.cache_data(ttl=300, show_spinner=False)
def scan_kr() -> tuple:
    listing = load_krx_listing()
    targets = listing[listing["Marcap"] > 3e11].nlargest(KR_SCAN_TOP_N, "Marcap")
    codes   = list(zip(targets["Code"].tolist(), targets["Name"].tolist()))

    get_krx_daily_snapshot()

    def _fetch(item):
        code, name = item
        df = load_ohlcv_kr(code)
        if df is None:
            return {"_skip": True, "ticker": f"{name}({code})", "why": "데이터 부족"}
        r = quant_predict(df, "KR")
        if not r["pass"]:
            why = next((s for s in r["signals"] if "❌" in s), "조건 미충족")
            return {"_skip": True, "ticker": f"{name}({code})", "why": why}
        price, vol, src = get_kr_price_with_fallback(code)
        if price <= 0:
            price = r["current"]
        # ★ 매수구간 항상 실시간 price 기준 (OHLCV MA 기준 사용 안 함)
        bmin = int(price * 0.97) if price > 0 else 0
        bmax = int(price * 1.02) if price > 0 else 0
        return {
            "_skip":    False,
            "종목":     name,
            "코드":     code,
            "등급":     r["grade"],
            "점수":     r["score"],
            "현재가":   int(price),
            "RSI":      round(r["rsi"], 1),
            "매수구간": f"₩{bmin:,} ~ ₩{bmax:,}",
            "목표가":   int(r["target"]) if r.get("target",0) > price*1.03 and r.get("target",0) <= price*1.15 else int(price * 1.08),
            "손절가":   int(r["stop"])   if r.get("stop",0) > price*0.85 and r.get("stop",0) < price*0.98 else int(price * 0.93),
            "signals":  r["signals"],
            "source":   src,
            "s_flags":  [r["s1"], r["s2"], r["s3"], r["s4"], r["s5"]],
        }

    with ThreadPoolExecutor(max_workers=30) as ex:
        raw = list(ex.map(_fetch, codes))
    skips = [r for r in raw if r.get("_skip")]
    top3  = sorted([r for r in raw if not r.get("_skip")], key=lambda x: x["점수"], reverse=True)[:3]
    return top3, skips


@st.cache_data(ttl=300, show_spinner=False)
def scan_us() -> tuple:
    rt_map = get_us_price_batch(tuple(US_WATCHLIST))

    def _fetch(ticker):
        df = load_ohlcv_us(ticker)
        if df is None:
            return {"_skip": True, "ticker": ticker, "why": "OHLCV 없음"}
        r = quant_predict(df, "US")
        if not r["pass"]:
            why = next((s for s in r["signals"] if "❌" in s), "조건 미충족")
            return {"_skip": True, "ticker": ticker, "why": why}
        price, src = rt_map.get(ticker, (0.0, "없음"))
        if price <= 0:
            price, src = get_us_price(ticker)
        if price <= 0:
            price = r["current"]; src = "OHLCV종가"
        # ★ 매수구간 항상 실시간 price 기준
        def _uf(v):
            if v < 1: return f"${v:,.4f}"
            elif v < 10: return f"${v:,.3f}"
            else: return f"${v:,.2f}"
        return {
            "_skip":    False,
            "종목":     ticker,
            "등급":     r["grade"],
            "점수":     r["score"],
            "현재가":   round(price, 2),
            "RSI":      round(r["rsi"], 1),
            "매수구간": f"{_uf(price*0.97)} ~ {_uf(price*1.02)}",
            "목표가":   round(r["target"], 2) if r.get("target",0) > price*1.03 and r.get("target",0) <= price*1.15 else round(price * 1.08, 2),
            "손절가":   round(r["stop"],   2) if r.get("stop",0) > price*0.85 and r.get("stop",0) < price*0.98 else round(price * 0.93, 2),
            "signals":  r["signals"],
            "source":   src,
            "s_flags":  [r["s1"], r["s2"], r["s3"], r["s4"], r["s5"]],
        }

    with ThreadPoolExecutor(max_workers=50) as ex:  # 나스닥+S&P500 대응
        raw = list(ex.map(_fetch, US_WATCHLIST))
    skips = [r for r in raw if r.get("_skip")]
    top3  = sorted([r for r in raw if not r.get("_skip")], key=lambda x: x["점수"], reverse=True)[:3]
    return top3, skips


@st.cache_data(ttl=300, show_spinner=False)
def scan_crypto() -> tuple:
    try:
        coins = pyupbit.get_tickers(fiat="KRW")[:CRYPTO_SCAN_LIMIT]
    except:
        coins = ["KRW-BTC","KRW-ETH","KRW-XRP"]

    def _fetch(coin):
        try:
            df = pyupbit.get_ohlcv(coin, interval="day", count=120)
            if df is None or df.empty:
                return {"_skip": True, "ticker": coin, "why": "OHLCV 없음"}
            r = quant_predict(df, "CRYPTO")
            if not r["pass"]:
                why = next((s for s in r["signals"] if "❌" in s), "조건 미충족")
                return {"_skip": True, "ticker": coin, "why": why}
            c = r["current"]
            # ★ 매수구간 항상 실시간 c 기준
            return {
                "_skip":    False,
                "종목":     coin.replace("KRW-", ""),
                "등급":     r["grade"],
                "점수":     r["score"],
                "현재가":   c,
                "RSI":      round(r["rsi"], 1),
                "매수구간": f"₩{int(c*0.97):,} ~ ₩{int(c*1.02):,}",
                "목표가":   round(r["target"], 0) if r["target"] > 0 else round(c * 1.10, 0),
                "손절가":   round(r["stop"],   0) if r["stop"]   > 0 else round(c * 0.93, 0),
                "signals":  r["signals"],
                "s_flags":  [r["s1"], r["s2"], r["s3"], r["s4"], r["s5"]],
            }
        except Exception as e:
            return {"_skip": True, "ticker": coin, "why": f"오류:{e}"}

    with ThreadPoolExecutor(max_workers=20) as ex:
        raw = list(ex.map(_fetch, coins))
    skips = [r for r in raw if r.get("_skip")]
    top3  = sorted([r for r in raw if not r.get("_skip")], key=lambda x: x["점수"], reverse=True)[:3]
    return top3, skips


# ============================================================
# 9. 포트폴리오 조회
# ============================================================
def get_portfolio_data(name: str) -> dict:
    name = name.strip().upper()

    # ── 국내 6자리 ──
    if name.isdigit() and len(name) == 6:
        price, vol, src = get_kr_price_with_fallback(code=name)
        is_ohlcv_fallback = "OHLCV" in src

        df = load_ohlcv_kr(name)
        if df is not None:
            r = quant_predict(df, "KR")
            curr = price if price > 0 else 0.0
            listing = load_krx_listing()
            row     = listing[listing["Code"] == name]
            label   = row["Name"].values[0] if not row.empty else name

            # ★ 매수구간 항상 실시간 curr 기준으로만 계산 (OHLCV MA 기준 사용 안 함)
            buy_min = int(curr * 0.97) if curr > 0 else 0
            buy_max = int(curr * 1.02) if curr > 0 else 0
            # ATR 기반 목표가/손절가 (quant_predict에서 계산된 값 우선)
            tgt = int(r["target"]) if r.get("target",0) > curr > 0 else int(curr * 1.08) if curr > 0 else 0
            stp = int(r["stop"])   if r.get("stop",0)   > 0        else int(curr * 0.93) if curr > 0 else 0

            return {
                "label":    f"{name} ({label})",
                "curr":     curr,
                "score":    r["score"],
                "grade":    r["grade"],
                "rsi":      round(r["rsi"], 1),
                "currency": "KRW",
                "stop":     stp,
                "target":   tgt,
                "buy_min":  buy_min,
                "buy_max":  buy_max,
                "source":   src + ("⚠️지연" if is_ohlcv_fallback else ""),
                "ok":       curr > 0 and not is_ohlcv_fallback,
                "signals":  r["signals"],
            }
        if price > 0:
            # OHLCV 없으면 ATR 계산 불가 → 고정값 fallback (표시용)
            return {
                "label": f"{name} ({src}·지표없음)", "curr": price,
                "score": 0, "grade": "-", "rsi": 50.0, "currency": "KRW",
                "stop": int(price * 0.93), "target": int(price * 1.08),
                "buy_min": int(price*0.97), "buy_max": int(price*1.02),
                "source": src, "ok": not is_ohlcv_fallback, "signals": [],
            }
        return {"label": None, "curr": 0, "score": 0, "grade": "F",
                "rsi": 0, "currency": "KRW", "stop": 0, "target": 0,
                "buy_min": 0.0, "buy_max": 0.0, "source": "실패", "ok": False, "signals": []}

    # ── 해외 티커 — 포트폴리오용: 장외가 + 정규장종가 둘 다 fetch ──
    market_open = is_us_market_open()

    # 장외가 먼저 시도 (포트폴리오 전용)
    prepost_price, session, regular_price = _yf_prepost_price(name)

    # Finnhub (정규장 기준)
    q = _fh_fetch_raw(name)
    fh_c, fh_pc = q["c"], q["pc"]

    # 정규장가 결정
    if market_open and fh_c > 0:
        price, src = fh_c, "Finnhub(정규장)"
    elif not market_open and fh_pc > 0:
        price, src = fh_pc, "Finnhub(전일종가)"
    elif fh_c > 0:
        price, src = fh_c, "Finnhub"
    elif regular_price > 0:
        price, src = regular_price, "yfinance(정규장)"
    else:
        yf_price, yf_src = _yf_fresh_price(name)
        price = yf_price if yf_price and yf_price > 0 else 0.0
        src   = yf_src if yf_src else "실패"

    # 장외가 유효성 확인 (정규장가 대비 ±30% 이내만 신뢰)
    # 장외가 표시 조건:
    # 1. prepost_price 유효
    # 2. 정규장가 대비 ±30% 이내
    # 3. 실패/장외마감 아님
    # 4. 정규장 중이어도 가격 차이 0.1% 이상이면 표시 (프리/애프터 반영)
    prepost_diff_raw = (prepost_price - price) / price * 100 if prepost_price > 0 and price > 0 else 0
    has_prepost = (prepost_price > 0 and price > 0 and
                   0.70 <= prepost_price/price <= 1.30 and
                   session not in ["실패", "장외마감"] and
                   abs(prepost_diff_raw) >= 0.1)  # 가격 차이 0.1% 이상일 때만
    prepost_diff = prepost_diff_raw if has_prepost else 0

    df = load_ohlcv_us(name)
    if df is not None:
        r = quant_predict(df, "US")
        curr = price
        if curr <= 0:
            return {
                "label": f"{name} ({src}·가격없음)", "curr": 0,
                "score": r["score"], "grade": r["grade"],
                "rsi": round(r["rsi"], 1), "currency": "USD",
                "stop": 0.0, "target": 0.0,
                "buy_min": 0.0, "buy_max": 0.0,
                "source": src, "ok": False, "signals": r["signals"],
            }

        # ★ 매수구간/목표가/손절가 항상 실시간 curr 기준으로만 계산
        #   OHLCV MA 기반 buy_min/max는 현재가보다 높게 나오는 버그 있어서 사용 안 함
        def _usd_round(v):
            if v < 1:   return round(v, 4)
            elif v < 10: return round(v, 3)
            else:        return round(v, 2)

        buy_min = _usd_round(curr * 0.97)
        buy_max = _usd_round(curr * 1.02)

        tgt_us = _usd_round(r["target"]) if r.get("target",0) > curr > 0 else _usd_round(curr * 1.08)
        stp_us = _usd_round(r["stop"])   if r.get("stop",0)   > 0        else _usd_round(curr * 0.93)
        return {
            "label":    f"{name} ({src})",
            "curr":     _usd_round(curr),
            "score":    r["score"],
            "grade":    r["grade"],
            "rsi":      round(r["rsi"], 1),
            "currency": "USD",
            "stop":     stp_us,
            "target":   tgt_us,
            "buy_min":  buy_min,
            "buy_max":  buy_max,
            "source":   src,
            "ok":       curr > 0,
            "signals":  r["signals"],
            "has_prepost":      has_prepost,
            "prepost_price":    _usd_round(prepost_price) if has_prepost else 0,
            "prepost_session":  session if has_prepost else "",
            "prepost_diff":     round(prepost_diff, 2) if has_prepost else 0,
        }
    if price > 0:
        # OHLCV 없으면 ATR 계산 불가 → 고정값 fallback (표시용)
        return {
            "label": f"{name} ({src}·지표없음)", "curr": round(price, 4),
            "score": 0, "grade": "-", "rsi": 50.0, "currency": "USD",
            "stop": round(price * 0.93, 4), "target": round(price * 1.08, 4),
            "buy_min": round(price * 0.97, 4), "buy_max": round(price * 1.02, 4),
            "source": src, "ok": True, "signals": [],
        }

    # ── 코인 ──
    try:
        coin_key = f"KRW-{name}"
        df_c = pyupbit.get_ohlcv(coin_key, interval="day", count=120)
        if df_c is not None and not df_c.empty:
            r = quant_predict(df_c, "CRYPTO")
            try:
                upbit_price = pyupbit.get_current_price(coin_key)
                c = float(upbit_price) if upbit_price and float(upbit_price) > 0 else r["current"]
                coin_src = "Upbit(실시간)" if upbit_price and float(upbit_price) > 0 else "OHLCV종가"
            except:
                c = r["current"]
                coin_src = "OHLCV종가"
            if c > 0:
                return {
                    "label":    f"{name} (업비트)",
                    "curr":     c,
                    "score":    r["score"],
                    "grade":    r["grade"],
                    "rsi":      round(r["rsi"], 1),
                    "currency": "KRW",
                    "stop":     round(r["stop"], 0)   if r.get("stop",0)   > 0       else round(c * 0.93, 0),
                    "target":   round(r["target"], 0) if r.get("target",0) > c > 0 else round(c * 1.10, 0),
                    "buy_min":  r["buy_min"],
                    "buy_max":  r["buy_max"],
                    "source":   coin_src,
                    "ok":       True,
                    "signals":  r["signals"],
                }
    except:
        pass

    return {"label": None, "curr": 0, "score": 0, "grade": "F",
            "rsi": 0, "currency": "USD", "stop": 0, "target": 0,
            "buy_min": 0.0, "buy_max": 0.0,
            "source": "실패", "ok": False, "signals": []}


# ============================================================
# 10. UI
# ============================================================
fg_val, fg_txt, exchange = get_market_status()

st.sidebar.title("🛡️ Tae Scanner")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.sidebar.metric("🇺🇸 미국 정규장", "OPEN" if is_us_market_open() else "CLOSED")

with st.sidebar.expander("🔑 API 상태", expanded=True):
    st.write("KIS:", "✅ 연결됨 (실시간)" if KIS_APP_KEY else "❌ 키 없음")
    st.write("KRX:", "✅ fallback" if KRX_API_KEY else "❌ 키 없음")
    st.write("Finnhub:", "✅ 연결됨" if FINNHUB_API_KEY else "❌ 키 없음 (yfinance 대체)")

st.title("🚀 Tae's Quant 폭등 예측 스캐너")

# ★ v7 캡션 — 실제 로직과 일치
st.caption(
    "📌 매수 검토 후보 탐지기 (추격매수 아님) | "
    "핵심진입: S3 정배열눌림목 OR S4 RSI다이버전스 필수 | "
    "가산점: S1 BB수축·S2 거래량눌림·S5 캔들 | "
    "실제 KRX 199종목 백테스트 기반 재설계 (2023~)"
)

# ── 수동 재스캔 버튼 ──
_col_scan, _col_time, _ = st.columns([1, 2, 4])
if _col_scan.button("🔄 지금 재스캔", type="primary", use_container_width=True):
    scan_kr.clear()
    scan_us.clear()
    scan_crypto.clear()
    load_ohlcv_kr.clear()
    load_ohlcv_us.clear()
    st.rerun()
_col_time.markdown(
    f"<span style='font-size:12px;color:#94a3b8;line-height:2.5;'>"
    f"마지막 스캔: {datetime.now().strftime('%H:%M:%S')}</span>",
    unsafe_allow_html=True,
)

ph_us   = st.empty()
ph_coin = st.empty()
ph_kr   = st.empty()
st.divider()

# ── 포트폴리오 ──
st.header("💼 내 자산 실시간 관리")

col_btn1, _ = st.columns([1, 5])
if col_btn1.button("🚨 전체 초기화"):
    st.session_state.my_portfolio = []
    save_portfolio([])
    st.rerun()

# 탭 분리: 관심종목 / 보유종목
tab_watch, tab_hold = st.tabs(["👀 관심종목 (매수 전)", "💰 보유종목 (평단 입력)"])

with tab_watch:
    st.caption("스캐너 추천 종목을 등록하면 다음날 매수 여부를 자동 판단해드려요")
    with st.form(key="watch_form", clear_on_submit=True):
        w1, w2, w3 = st.columns([2, 2, 1])
        wn_in = w1.text_input("종목코드", placeholder="005930 / AAPL / BTC")
        wm_in = w2.text_input("메모", placeholder="스캐너 추천, 관심 이유 등")
        if w3.form_submit_button("👀 관심 추가"):
            if wn_in:
                # 중복 체크
                exists = any(p["name"] == wn_in.strip().upper() for p in st.session_state.my_portfolio)
                if not exists:
                    st.session_state.my_portfolio.append({
                        "name": wn_in.strip().upper(),
                        "buy":  0.0,
                        "date": "",
                        "type": "watch",
                        "memo": wm_in.strip(),
                    })
                    save_portfolio(st.session_state.my_portfolio)
                    st.rerun()
                else:
                    st.warning("이미 등록된 종목이에요.")

with tab_hold:
    with st.form(key="portfolio_form", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        n_in = c1.text_input("종목코드 / 티커 / 코인",
                             placeholder="국내: 005930  해외: AAPL  코인: BTC")
        b_in = c2.number_input("내 평단가", min_value=0.0, step=0.01, format="%.4f")
        d_in = c3.text_input("매수일자", placeholder="2024-01-15")
        if c4.form_submit_button("➕ 추가"):
            if n_in and b_in > 0:
                # 관심종목 → 보유종목으로 업그레이드
                for p in st.session_state.my_portfolio:
                    if p["name"] == n_in.strip().upper() and p.get("type") == "watch":
                        p["buy"]  = float(b_in)
                        p["date"] = d_in.strip() if d_in.strip() else ""
                        p["type"] = "hold"
                        save_portfolio(st.session_state.my_portfolio)
                        st.rerun()
                        break
                else:
                    st.session_state.my_portfolio.append({
                        "name": n_in.strip().upper(),
                        "buy":  float(b_in),
                        "date": d_in.strip() if d_in.strip() else "",
                        "type": "hold",
                        "memo": "",
                    })
                    save_portfolio(st.session_state.my_portfolio)
                    st.rerun()
            else:
                st.warning("종목명과 평단가를 입력하세요.")

if st.session_state.my_portfolio:
    to_remove = None
    for i, p in enumerate(st.session_state.my_portfolio):
        name, buy = p["name"], p["buy"]
        ptype = p.get("type", "hold")  # watch or hold
        d = get_portfolio_data(name)

        if not d["ok"] or d["curr"] <= 0:
            st.error(f"⚠️ {name} 조회 실패 — 출처: {d['source']}")
            if st.button(f"❌ 삭제 ({name})", key=f"del_err_{i}"):
                to_remove = i
            continue

        curr   = d["curr"]
        profit = (curr - buy) / buy * 100 if buy > 0 else 0
        is_kr  = d["currency"] == "KRW"

        # ── 관심종목 카드 (별도 렌더링) ──
        if ptype == "watch":
            grade_color = {"A+": "#f59e0b", "A": "#10b981", "B+": "#3b82f6",
                           "B": "#94a3b8", "C": "#64748b"}.get(d["grade"], "#64748b")

            def _usd_fmt_w(v):
                if v <= 0: return "$0"
                if v < 1:  return f"${v:,.4f}"
                elif v < 10: return f"${v:,.3f}"
                else: return f"${v:,.2f}"
            fmt_w = (lambda v: f"₩{int(v):,}") if is_kr else _usd_fmt_w

            # 전일 종가 (갭 계산용)
            prev_close = 0.0
            try:
                if is_kr:
                    df_tmp = load_ohlcv_kr(name)
                    if df_tmp is not None and len(df_tmp) >= 2:
                        prev_close = float(df_tmp["close"].iloc[-2])
                else:
                    df_tmp = load_ohlcv_us(name)
                    if df_tmp is not None and len(df_tmp) >= 2:
                        prev_close = float(df_tmp["close"].iloc[-2])
            except: pass

            gap_pct = (curr - prev_close) / prev_close * 100 if prev_close > 0 else 0

            # 갭 판단
            if abs(gap_pct) < 3:
                gap_verdict = "🟢 갭 양호"; gap_color = "#10b981"
                gap_detail  = f"갭 {gap_pct:+.1f}% — 매수 검토 가능"
            elif abs(gap_pct) < 5:
                gap_verdict = "🟡 소폭 갭"; gap_color = "#f59e0b"
                gap_detail  = f"갭 {gap_pct:+.1f}% — 눌림 기다려야"
            else:
                gap_verdict = "🔴 갭 과다"; gap_color = "#ef4444"
                gap_detail  = f"갭 {gap_pct:+.1f}% — 추격매수 위험"

            # 신호 유지 여부
            sigs_w = d.get("signals", [])
            s3_alive = any("S3" in s and "✅" in s for s in sigs_w)
            s4_alive = any("S4" in s and "✅" in s for s in sigs_w)
            signal_ok = s3_alive and s4_alive

            # 수급 (국내만)
            supply_html = ""
            if is_kr and KRX_API_KEY:
                sup = calc_supply_signal(name)
                if sup.get("ok"):
                    sup_sigs = " / ".join(sup.get("signals", [])[:3])
                    supply_html = f"""
  <div style="background:#1a2744;border:1px solid #3b82f6;border-radius:6px;
              padding:8px 12px;margin-bottom:8px;">
    <span style="color:#3b82f6;font-size:11px;font-weight:bold;">수급</span>
    <span style="color:{sup['color']};font-size:12px;font-weight:bold;margin-left:8px;">
      {sup['verdict']}
    </span><br>
    <span style="color:#94a3b8;font-size:10px;">{sup_sigs}</span>
  </div>"""

            # 종합 매수 판단
            buy_score = 0
            if abs(gap_pct) < 3: buy_score += 2
            elif abs(gap_pct) < 5: buy_score += 1
            if signal_ok: buy_score += 2
            if is_kr and KRX_API_KEY:
                sup = calc_supply_signal(name)
                if sup.get("ok") and sup.get("score", 0) >= 3: buy_score += 2

            if buy_score >= 5:
                buy_verdict = "🟢 매수 적극 고려"; buy_color = "#10b981"
            elif buy_score >= 3:
                buy_verdict = "🟡 조건부 매수 (눌림 확인)"; buy_color = "#f59e0b"
            else:
                buy_verdict = "🔴 매수 보류"; buy_color = "#ef4444"

            memo_str = p.get("memo", "")

            st.markdown(f"""
<div style="background:#1e293b;padding:16px;border-radius:12px;
            border-left:6px solid {grade_color};margin-bottom:12px;
            border-top:2px solid #334155;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h3 style="margin:0;">👀 {d['label']}
      <span style="font-size:11px;background:#334155;color:#94a3b8;
                   padding:2px 6px;border-radius:4px;margin-left:8px;">관심종목</span>
    </h3>
    <span style="background:{grade_color};color:#000;font-size:11px;font-weight:bold;
                 padding:2px 6px;border-radius:4px;">{d['grade']} {d['score']}점</span>
  </div>
  {f'<div style="font-size:11px;color:#64748b;margin-bottom:8px;">📝 {memo_str}</div>' if memo_str else ''}

  <div style="background:#0f172a;padding:10px;border-radius:8px;margin-bottom:8px;">
    <div style="display:flex;justify-content:space-between;">
      <span style="color:#94a3b8;font-size:11px;">현재가</span>
      <span style="font-weight:bold;">{fmt_w(curr)}</span>
    </div>
    <div style="display:flex;justify-content:space-between;margin-top:4px;">
      <span style="color:#94a3b8;font-size:11px;">전일 대비 갭</span>
      <span style="color:{'#10b981' if gap_pct>=0 else '#ef4444'};font-weight:bold;">
        {gap_pct:+.1f}%
      </span>
    </div>
  </div>

  <div style="background:#0f172a;padding:10px 14px;border-radius:8px;margin-bottom:8px;">
    <div style="display:flex;justify-content:space-between;">
      <span style="font-size:12px;font-weight:bold;color:{gap_color};">{gap_verdict}</span>
      <span style="font-size:11px;color:#94a3b8;">{gap_detail}</span>
    </div>
  </div>

  <div style="background:#0f172a;padding:10px 14px;border-radius:8px;margin-bottom:8px;">
    <div style="display:flex;justify-content:space-between;">
      <span style="font-size:12px;color:#94a3b8;">신호 유지</span>
      <span style="font-size:12px;font-weight:bold;">
        {'✅ S3+S4 유지' if signal_ok else ('⚠️ S3만' if s3_alive else '❌ 신호 소멸')}
      </span>
    </div>
  </div>

  {supply_html}

  <div style="background:#0f172a;padding:12px 14px;border-radius:8px;margin-bottom:8px;
              border-left:3px solid {buy_color};">
    <span style="font-size:14px;font-weight:bold;color:{buy_color};">{buy_verdict}</span>
  </div>

  <div style="display:flex;gap:8px;margin-top:8px;">
    <div style="font-size:10px;color:#475569;">📡 {d['source']}</div>
  </div>
</div>""", unsafe_allow_html=True)

            col_buy, col_del = st.columns([2, 1])
            if col_buy.button(f"➕ 매수 확정 (평단 입력)", key=f"buy_{i}"):
                st.session_state[f"buy_confirm_{i}"] = True
            if st.session_state.get(f"buy_confirm_{i}"):
                buy_price = st.number_input(f"평단가 입력 ({name})",
                                           min_value=0.0, step=0.01,
                                           format="%.4f", key=f"buy_price_{i}")
                buy_date  = st.text_input("매수일자", value=datetime.now().strftime("%Y-%m-%d"),
                                         key=f"buy_date_{i}")
                if st.button("✅ 확정", key=f"buy_ok_{i}"):
                    p["buy"]  = float(buy_price)
                    p["date"] = buy_date
                    p["type"] = "hold"
                    st.session_state[f"buy_confirm_{i}"] = False
                    save_portfolio(st.session_state.my_portfolio)
                    st.rerun()
            if col_del.button("🗑️ 삭제", key=f"del_w_{i}"):
                to_remove = i
            continue  # 관심종목은 여기서 끝

        def _usd_fmt(v: float) -> str:
            if v <= 0:   return "$0"
            if v < 1:    return f"${v:,.4f}"
            elif v < 10: return f"${v:,.3f}"
            else:        return f"${v:,.2f}"

        fmt = (lambda v: f"₩{int(v):,}") if is_kr else _usd_fmt
        p_color    = "#10b981" if profit >= 0 else "#ef4444"
        grade_color = {"A+": "#f59e0b", "A": "#10b981", "B+": "#3b82f6",
                       "B": "#94a3b8", "C": "#64748b"}.get(d["grade"], "#64748b")

        bmin = d.get("buy_min", 0); bmax = d.get("buy_max", 0)
        buy_range_str = (f"₩{int(bmin):,} ~ ₩{int(bmax):,}" if is_kr else
                         f"{_usd_fmt(bmin)} ~ {_usd_fmt(bmax)}") if bmin > 0 else "—"

        stale_warn = any(kw in d.get("source","") for kw in ["주의","오래됨","stale","지연"])
        warn_badge = ("<span style='background:#ef4444;color:#fff;font-size:10px;"
                      "padding:2px 6px;border-radius:4px;margin-left:8px;'>⚠️ 시세 지연</span>"
                      ) if stale_warn else ""

        # ══ 포지션 판단 (개선판) ══
        rsi_v = d["rsi"]

        # ── 보유일수 계산 ──
        hold_days = 0
        buy_date_str = p.get("date", "")
        if buy_date_str:
            try:
                from datetime import datetime as _dt
                buy_dt = _dt.strptime(buy_date_str, "%Y-%m-%d")
                hold_days = (_dt.now() - buy_dt).days
            except: hold_days = 0

        # ── 평단가 기준 고정 손절가 ──
        fixed_stop   = buy * 0.93   # 평단 -7% 고정
        fixed_target = buy * 1.08   # 평단 +8% (1차 익절 참고)

        # ── 추세 이탈 감지 (MA 꺾임) ──
        sigs = d.get("signals", [])
        s3_on   = any("S3" in s and "✅" in s for s in sigs)
        s1_on   = any("S1" in s and "✅" in s for s in sigs)
        # MA 꺾임: S3 꺼짐 = 정배열 깨짐
        trend_broken = not s3_on  # 정배열 미충족 = 추세 약화

        # ── 거래량 이상 감지 ──
        s2_on = any("S2" in s and "✅" in s for s in sigs)
        vol_dry = s2_on  # 거래량 눌림 = 매집 or 이탈 가능성

        # ── 판단 로직 (우선순위 순) ──

        # 1순위: 평단 기준 손절 (가장 중요)
        if curr <= fixed_stop:
            action = "🔴 즉시 손절 고려"; action_color = "#ef4444"
            action_reason = f"평단 대비 -7% 이탈 ({profit:.1f}%)"

        # 2순위: 추세 붕괴 + 손실
        elif trend_broken and profit < -3:
            action = "🔴 손절 고려"; action_color = "#ef4444"
            action_reason = f"정배열 붕괴 + 손실 {profit:.1f}%"

        # 3순위: RSI 과열 + 수익 (익절)
        elif rsi_v > 70 and profit > 5:
            action = "🟡 익절 고려"; action_color = "#f59e0b"
            action_reason = f"RSI 과열({rsi_v:.0f}) + 수익 {profit:.1f}%"

        # 4순위: 목표가 도달
        elif curr >= fixed_target:
            action = "🟡 익절 고려"; action_color = "#f59e0b"
            action_reason = f"평단 기준 목표가({fmt(fixed_target)}) 도달"

        # 5순위: 장기보유 + 추세 약화
        elif hold_days >= 10 and trend_broken:
            action = "🟡 익절/손절 검토"; action_color = "#f59e0b"
            action_reason = f"보유 {hold_days}일 + 추세 약화 — 재검토 필요"

        # 6순위: 추가매수 (정배열 유지 + RSI 여유 + 소폭 손실)
        elif s3_on and 40 <= rsi_v <= 60 and -3 <= profit <= 0:
            action = "🟢 추가매수 검토"; action_color = "#10b981"
            action_reason = f"정배열 유지 + RSI 여유({rsi_v:.0f}) + 눌림 {profit:.1f}%"

        # 7순위: 홀딩 (정배열 유지 + 수익 구간)
        elif s3_on and profit > 0:
            action = "⚪ 홀딩"; action_color = "#94a3b8"
            action_reason = f"정배열 유지 + 수익 {profit:.1f}% — 추세 살아있음"

        # 8순위: 단기보유 관망
        elif hold_days > 0 and hold_days <= 3:
            action = "⚪ 홀딩 (관망)"; action_color = "#94a3b8"
            action_reason = f"매수 {hold_days}일차 — 추세 확인 중"

        else:
            action = "⬜ 관망"; action_color = "#64748b"
            action_reason = "추세 불명확 — 신호 대기"

        # 보유일수 표시
        hold_str = f"{hold_days}일째 보유" if hold_days > 0 else ("매수일 미입력" if not buy_date_str else "오늘 매수")

        # 2. 변화감지 — 오늘 새 신호 켜졌나
        change_signals = [s for s in d.get("signals", []) if "🔔 변화감지" in s]
        change_html = ""
        if change_signals:
            change_html = f"""
  <div style="background:#1e3a2f;border:1px solid #10b981;border-radius:6px;
              padding:8px 12px;margin-bottom:8px;">
    <span style="color:#10b981;font-size:12px;font-weight:bold;">
      🔔 오늘 새 신호 감지 — 1~2일 내 변화 가능
    </span><br>
    <span style="color:#6ee7b7;font-size:11px;">{change_signals[0].replace("🔔 변화감지: ","")}</span>
  </div>"""

        # 장외가 HTML (해외 종목만)
        prepost_html = ""
        if not is_kr and d.get("has_prepost", False):
            pp_price  = d.get("prepost_price", 0)
            pp_sess   = d.get("prepost_session", "")
            pp_diff   = d.get("prepost_diff", 0)
            pp_color  = "#10b981" if pp_diff >= 0 else "#ef4444"
            pp_profit = (pp_price - buy) / buy * 100 if buy > 0 and pp_price > 0 else 0
            prepost_html = f"""
  <div style="background:#1a2744;border:1px solid #3b82f6;border-radius:6px;
              padding:8px 12px;margin-bottom:8px;">
    <span style="color:#3b82f6;font-size:12px;font-weight:bold;">{pp_sess}</span>
    <span style="color:{pp_color};font-size:13px;font-weight:bold;margin-left:12px;">
      {_usd_fmt(pp_price)} ({pp_diff:+.2f}%)
    </span>
    <span style="color:#94a3b8;font-size:11px;margin-left:8px;">
      수익률 기준: {pp_profit:+.2f}%
    </span>
  </div>"""

        # 3. 방향성 판단 (RSI + MA + 거래량 조합)
        sigs = d.get("signals", [])
        s1_on = any("S1" in s and "✅" in s for s in sigs)
        s2_on = any("S2" in s and "✅" in s for s in sigs)
        s3_on = any("S3" in s and "✅" in s for s in sigs)

        if s1_on and s2_on and s3_on:
            direction = "🚀 강한 상승 셋업"; dir_color = "#10b981"
            dir_detail = "BB수축+거래량눌림+정배열 동시 — 폭발 직전"
        elif (s1_on or s2_on) and s3_on:
            direction = "📈 상승 가능성 높음"; dir_color = "#3b82f6"
            dir_detail = "주요 셋업 2개 이상 충족"
        elif s3_on and 40 <= rsi_v <= 60:
            direction = "📈 완만한 상승 기대"; dir_color = "#3b82f6"
            dir_detail = f"정배열 눌림목 + RSI 중립({rsi_v:.0f})"
        elif rsi_v > 70:
            direction = "⚠️ 과매수 주의"; dir_color = "#f59e0b"
            dir_detail = f"RSI {rsi_v:.0f} — 단기 조정 가능"
        elif rsi_v < 30:
            direction = "🔄 반등 기대"; dir_color = "#a78bfa"
            dir_detail = f"RSI {rsi_v:.0f} 극과매도 — 단기 반등 가능"
        elif d["score"] >= 50:
            direction = "🔶 중립 (신호 대기)"; dir_color = "#f59e0b"
            dir_detail = f"점수 {d['score']}점 — 추가 신호 확인 필요"
        else:
            direction = "⬜ 방향 불명확"; dir_color = "#64748b"
            dir_detail = "뚜렷한 신호 없음 — 관망"

        st.markdown(f"""
<div style="background:#1e293b;padding:20px;border-radius:12px;
            border-left:6px solid {grade_color};margin-bottom:16px;">

  <h3 style="margin:0 0 12px 0;">📈 {d['label']}
    <span style="font-size:14px;background:{grade_color};color:#000;
                 padding:2px 8px;border-radius:4px;margin-left:8px;">
      {d['grade']}등급 {d['score']}점
    </span>{warn_badge}
  </h3>

  {change_html}
  {prepost_html}

  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px;">
    <div><div style="font-size:11px;color:#94a3b8;">내 평단가</div>
         <div style="font-size:20px;font-weight:bold;">{fmt(buy)}</div></div>
    <div><div style="font-size:11px;color:#94a3b8;">현재가</div>
         <div style="font-size:20px;font-weight:bold;">{fmt(curr)}</div></div>
    <div><div style="font-size:11px;color:#94a3b8;">수익률</div>
         <div style="font-size:20px;font-weight:bold;color:{p_color};">
           {'+' if profit>=0 else ''}{profit:.2f}%</div></div>
  </div>

  <div style="background:#0f172a;padding:10px 14px;border-radius:8px;margin-bottom:8px;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <span style="font-size:13px;font-weight:bold;color:{action_color};">{action}</span>
      <span style="font-size:11px;color:#94a3b8;">{action_reason}</span>
    </div>
  </div>

  <div style="background:#0f172a;padding:10px 14px;border-radius:8px;margin-bottom:8px;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <span style="font-size:13px;font-weight:bold;color:{dir_color};">{direction}</span>
      <span style="font-size:11px;color:#94a3b8;">{dir_detail}</span>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr 1fr;gap:8px;margin-bottom:8px;">
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">평단기준 손절</div>
      <div style="color:#ef4444;font-weight:bold;font-size:11px;">{fmt(fixed_stop)}</div>
      <div style="font-size:9px;color:#64748b;">고정 -7%</div></div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">평단기준 목표</div>
      <div style="color:#3b82f6;font-weight:bold;font-size:11px;">{fmt(fixed_target)}</div>
      <div style="font-size:9px;color:#64748b;">+8%</div></div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">RSI</div>
      <div style="font-weight:bold;">{rsi_v}</div></div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">보유기간</div>
      <div style="font-weight:bold;font-size:11px;">{hold_str}</div></div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">추세</div>
      <div style="font-weight:bold;font-size:11px;">{'🟢정배열' if s3_on else '🔴추세약화'}</div></div>
  </div>
  <div style="font-size:10px;color:#475569;">📡 출처: {d['source']}</div>
</div>""", unsafe_allow_html=True)

        if st.button("🗑️ 삭제", key=f"del_{i}"):
            to_remove = i

    if to_remove is not None:
        st.session_state.my_portfolio.pop(to_remove)
        save_portfolio(st.session_state.my_portfolio)
        st.rerun()

# ── 스캔 실행 ──
with st.spinner("📡 퀀트 예측 스캔 중..."):
    kr_top3, kr_skips         = scan_kr()
    us_top3, us_skips         = scan_us()
    crypto_top3, crypto_skips = scan_crypto()

with st.sidebar.expander(f"🔍 국내 스캔 제외 ({len(kr_skips)}종목)", expanded=False):
    for reason, cnt in summarize_skips(kr_skips).items():
        st.markdown(f"- **{reason}**: {cnt}개")

with st.sidebar.expander(f"🔍 코인 스캔 제외 ({len(crypto_skips)}종목)", expanded=False):
    for reason, cnt in summarize_skips(crypto_skips).items():
        st.markdown(f"- **{reason}**: {cnt}개")

with st.sidebar.expander("🔍 해외 스캔 제외 로그", expanded=False):
    for s in us_skips:
        st.markdown(f"- **{s['ticker']}**: {s['why']}")

# ── 카드 렌더 ──
S_LABELS = ["S1:BB수축", "S2:거래량눌림", "S3:추세눌림목", "S4:RSI다이버전스", "S5:반등캔들"]

def render_cards(placeholder, title: str, data: list, currency: str):
    with placeholder.container():
        st.header(title)
        if not data:
            st.info("⚠️ 조건 충족 종목 없음 (사이드바 제외 요약 확인)")
            return
        cols = st.columns(len(data))
        for i, item in enumerate(data):
            medal = "🥇🥈🥉"[i]
            is_kr = currency == "KRW"
            sym   = "₩" if is_kr else ("$" if currency == "USD" else "")
            fmt   = (lambda v: f"{sym}{int(v):,}") if is_kr else (lambda v: f"{sym}{v:,.2f}")
            grade_color = {"A+": "#f59e0b","A": "#10b981","B+": "#3b82f6",
                           "B": "#94a3b8","C": "#64748b"}.get(item.get("등급","C"), "#64748b")
            flags = item.get("s_flags", [False]*5)
            badge_html = " ".join(
                f"<span style='background:{'#10b981' if ok else '#1e293b'};color:{'#fff' if ok else '#475569'};"
                f"font-size:9px;padding:2px 5px;border-radius:3px;border:1px solid #334155;'>{lbl}</span>"
                for ok, lbl in zip(flags, S_LABELS)
            )
            sigs_html = "".join(
                f"<li style='font-size:11px;margin:2px 0;'>{s}</li>"
                for s in item.get("signals", [])
            )
            src_html = f"<span style='font-size:9px;color:#64748b;'>📡{item.get('source','')}</span>"
            with cols[i]:
                st.markdown(f"""
<div style="background:#1e293b;padding:18px;border-radius:12px;
            border-left:5px solid {grade_color};margin-bottom:8px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <h3 style="margin:0;">{medal} {item['종목']}</h3>
    <span style="background:{grade_color};color:#000;font-size:12px;font-weight:bold;
                 padding:2px 8px;border-radius:4px;">{item.get('등급','?')}</span>
  </div>
  <div style="margin-bottom:8px;">{badge_html}</div>
  <ul style="padding-left:16px;margin:8px 0;">
    <li>🎯 예측 점수: <b>{item['점수']}점</b></li>
    <li>📊 RSI: <code>{item['RSI']}</code></li>
    <li>💰 현재가: <b>{fmt(item['현재가'])}</b> {src_html}</li>
    <li>🟢 매수구간: <b style="color:#10b981;">{item['매수구간']}</b></li>
    <li>📈 목표가: <span style="color:#3b82f6;">{fmt(item['목표가'])}</span></li>
    <li>📉 손절선: <span style="color:#ef4444;">{fmt(item['손절가'])}</span></li>
  </ul>
  <details>
    <summary style="cursor:pointer;font-size:11px;color:#94a3b8;">📋 시그널 상세</summary>
    <ul style="margin-top:4px;padding-left:14px;">{sigs_html}</ul>
  </details>
</div>""", unsafe_allow_html=True)

render_cards(ph_us,   "🇺🇸 해외 폭등 예측 TOP 3",  us_top3,     "USD")
render_cards(ph_coin, "🪙 코인 폭등 예측 TOP 3",    crypto_top3, "KRW")
render_cards(ph_kr,   "🔥 국내 폭등 예측 TOP 3",    kr_top3,     "KRW")

# ============================================================
# 11. ★ 백테스트 탭 — 실제 KRX 데이터로 S1~S5 검증
# ============================================================
st.divider()
st.header("🔬 시그널 백테스트 (실제 KRX 데이터)")
st.caption("실제 과거 OHLCV 데이터로 S1~S5 각각의 예측력을 검증합니다.")

with st.expander("⚙️ 백테스트 설정", expanded=True):
    bt_col1, bt_col2, bt_col3 = st.columns(3)
    bt_universe = bt_col1.selectbox(
        "종목 유니버스",
        ["코스피200 전체(자동)", "코스피100(자동)", "직접입력"],
        index=0,
    )
    bt_codes_input = bt_col1.text_input(
        "직접입력 시 종목코드 (쉼표 구분)",
        value="005930,000660,035420,005380,051910,035720,000270,028260,012330,066570",
        disabled=(bt_universe != "직접입력"),
    )
    bt_start   = bt_col2.text_input("시작일", value="2023-01-01")
    bt_max_n   = bt_col2.number_input("최대 종목수 (속도 조절)", min_value=10, max_value=200, value=100, step=10)
    bt_ma_mode = bt_col2.selectbox("MA 방식 비교", ["현재(SMA)", "EMA5+EMA20+SMA60", "둘 다 비교"], index=2)
    bt_run     = bt_col3.button("🚀 백테스트 실행", type="primary", use_container_width=True)
    st.caption("⚠️ 코스피200 전체 + 2년치는 수분 소요될 수 있어요. 최대 종목수로 속도 조절하세요.")

@st.cache_data(ttl=86400, show_spinner=False)
def load_kospi200_codes(top_n=200) -> list:
    """KRX 상장 종목에서 시총 상위 N개 코드 자동 로딩"""
    try:
        listing = fdr.StockListing("KRX")
        kospi = listing[listing["Market"].str.contains("KOSPI", na=False)]
        kospi = kospi[kospi["Marcap"] > 0].nlargest(top_n, "Marcap")
        return kospi["Code"].tolist()
    except Exception as e:
        st.warning(f"코스피 종목 로딩 실패: {e}")
        return ["005930","000660","035420","005380","051910",
                "035720","000270","028260","012330","066570"]

if bt_run:
    if bt_universe == "코스피200 전체(자동)":
        bt_codes = load_kospi200_codes(top_n=bt_max_n)
        st.info(f"📋 코스피 시총 상위 {len(bt_codes)}개 종목으로 백테스트")
    elif bt_universe == "코스피100(자동)":
        bt_codes = load_kospi200_codes(top_n=min(100, bt_max_n))
        st.info(f"📋 코스피 시총 상위 {len(bt_codes)}개 종목으로 백테스트")
    else:
        bt_codes = [c.strip() for c in bt_codes_input.split(",") if c.strip()]
        st.info(f"📋 직접 입력 {len(bt_codes)}개 종목으로 백테스트")

    @st.cache_data(ttl=3600, show_spinner=False)
    def load_bt_ohlcv(code, start):
        try:
            df = fdr.DataReader(code, start=start)
            if df is not None and len(df) >= 80:
                df.columns = [c.lower() for c in df.columns]
                return df
        except:
            pass
        return None

    def calc_signal_flags(df, ma_mode="현재(SMA)"):
        """각 봉에서 S1~S5 + 변화감지 플래그 계산 (벡터화)
        ma_mode: "현재(SMA)" or "EMA5+EMA20+SMA60"
        """
        cl = df["close"].astype(float)
        hi = df["high"].astype(float)
        lo = df["low"].astype(float)
        vo = df["volume"].astype(float)

        # MA 방식 선택
        if ma_mode == "EMA5+EMA20+SMA60":
            ma5  = cl.ewm(span=5,  adjust=False).mean()   # EMA5
            ma20 = cl.ewm(span=20, adjust=False).mean()   # EMA20
            ma60 = cl.rolling(60).mean()                  # SMA60 유지
        else:
            ma5  = cl.rolling(5).mean()
            ma20 = cl.rolling(20).mean()
            ma60 = cl.rolling(60).mean()

        # S1: BB 수축
        bb_std  = cl.rolling(20).std()
        bb_mean = cl.rolling(20).mean().replace(0, np.nan)
        bbw     = (bb_std * 2) / bb_mean
        bw_avg  = bbw.rolling(20).mean()
        s1_strong = (bbw < bw_avg * 0.80).fillna(False).astype(bool)
        s1_weak   = ((bbw < bw_avg * 0.92).fillna(False).astype(bool)) & ~s1_strong
        s1 = s1_strong | s1_weak

        # S2: 거래량 눌림
        vm5  = vo.rolling(5).mean()
        vm20 = vo.rolling(20).mean()
        s2_strong = (vm5 < vm20 * 0.65).fillna(False).astype(bool)
        s2_weak   = ((vm5 < vm20 * 0.80).fillna(False).astype(bool)) & ~s2_strong
        s2 = s2_strong | s2_weak

        # S3: 정배열 + MA20 눌림목
        aligned = ((ma5 > ma20) & (ma20 > ma60)).fillna(False).astype(bool)
        near    = (((cl - ma20).abs() / ma20) <= 0.05).fillna(False).astype(bool)
        s3 = aligned & near

        # S4: RSI 다이버전스 — 벡터화 (20봉 rolling, 속도 개선)
        delta  = cl.diff()
        gain_s = delta.clip(lower=0).rolling(14).mean()
        loss_s = (-delta.clip(upper=0)).rolling(14).mean()
        rsi    = 100 - 100 / (1 + gain_s / loss_s.replace(0, np.nan))
        rsi_filled = rsi.fillna(50.0)

        # 전반 10봉 최저가 / 후반 10봉 최저가 rolling 비교
        # 전반: t-20~t-10 / 후반: t-10~t
        p_low_prev = cl.rolling(20).apply(lambda x: x[:10].min(), raw=True)
        p_low_now  = cl.rolling(20).apply(lambda x: x[10:].min(), raw=True)
        r_low_prev = rsi_filled.rolling(20).apply(lambda x: x[:10].min(), raw=True)
        r_low_now  = rsi_filled.rolling(20).apply(lambda x: x[10:].min(), raw=True)
        s4 = ((p_low_now < p_low_prev) & (r_low_now > r_low_prev + 3)).fillna(False).astype(bool)

        # S5: 양봉전환 캔들
        op = df["open"].astype(float)
        cl_shift = cl.shift(1).fillna(cl)
        op_shift = op.shift(1).fillna(op)
        bull_rev = ((cl_shift < op_shift) & (cl > op)).fillna(False).astype(bool)
        body     = (cl - op).abs()
        lower    = (op.clip(upper=cl) - lo)
        upper    = (hi - op.clip(lower=cl))
        hammer   = ((body > 0) & (lower > body * 2) & (upper < body * 0.5)).fillna(False).astype(bool)
        s5 = bull_rev | hammer

        # ── S6: 거래량 폭발 후 눌림 (벡터화) ──
        # 최근 3~15일 내 거래량 150% 이상 폭발 → 이후 눌림 → 현재가 유지
        vm20_s6 = vo.rolling(20).mean()
        burst   = (vo > vm20_s6 * 1.5)  # 폭발일 마스크

        s6_basic  = pd.Series(False, index=cl.index)
        s6_strong = pd.Series(False, index=cl.index)

        for idx in range(25, len(cl)):
            vm = _safe_float(vm20_s6.iloc[idx])
            cur_p = _safe_float(cl.iloc[idx])
            if vm <= 0 or cur_p <= 0:
                continue
            # 3~15일 전 폭발일 탐색
            burst_day = -1; burst_price = 0.0
            for k in range(3, 16):
                if idx - k < 0: break
                if burst.iloc[idx-k]:
                    burst_day = k
                    burst_price = _safe_float(cl.iloc[idx-k])
                    break
            if burst_day < 0: continue
            # 폭발 후 거래량 눌림
            vol_after = [_safe_float(vo.iloc[idx-j]) for j in range(1, burst_day)]
            if not vol_after: continue
            vol_dried = np.mean(vol_after) < vm * 0.85
            # 현재가 폭발일 대비 -8% 이내
            price_ok  = burst_price > 0 and cur_p >= burst_price * 0.92
            if vol_dried and price_ok:
                s6_basic.iloc[idx] = True
                # 강화: 오늘 거래량 재상승
                vol_today = _safe_float(vo.iloc[idx])
                if len(vol_after) >= 2 and vol_today > np.mean(vol_after[1:]) * 1.2:
                    s6_strong.iloc[idx] = True

        s6_basic  = s6_basic.fillna(False).astype(bool)
        s6_strong = s6_strong.fillna(False).astype(bool)

        # 변화감지: 오늘 켜짐 & 어제 꺼짐
        # shift() 후 NaN → bool 변환 필수 (pandas ~연산 오류 방지)
        bb_t = (bbw < bw_avg * 0.85).fillna(False).astype(bool)
        bb_y = bb_t.shift(1).fillna(False).astype(bool)
        vd_t = (vm5 < vm20 * 0.65).fillna(False).astype(bool)
        vd_y = vd_t.shift(1).fillna(False).astype(bool)
        n5_t = (near & (ma20 > ma60)).fillna(False).astype(bool)
        n5_y = n5_t.shift(1).fillna(False).astype(bool)
        new_bb   = bb_t & ~bb_y
        new_vd   = vd_t & ~vd_y
        new_n5   = n5_t & ~n5_y
        existing = bb_t.astype(int) + vd_t.astype(int) + n5_t.astype(int)
        new_any  = new_bb | new_vd | new_n5
        change   = new_any & (existing >= 2)

        return pd.DataFrame({
            "s1": s1, "s1_strong": s1_strong,
            "s2": s2, "s2_strong": s2_strong,
            "s3": s3, "s4": s4, "s5": s5,
            "s6": s6_basic, "s6_strong": s6_strong,
            "change": change,
            "close": cl, "rsi": rsi,
        })

    def calc_lift_sharpe(returns_on, returns_off, label):
        """리프트, 샤프 계산"""
        if len(returns_on) < 5:
            return None
        on_mean  = np.mean(returns_on)
        off_mean = np.mean(returns_off)
        lift     = on_mean - off_mean
        on_std   = np.std(returns_on) if np.std(returns_on) > 0 else 1e-6
        sharpe   = on_mean / on_std * np.sqrt(252)
        wr       = sum(1 for r in returns_on if r > 0) / len(returns_on) * 100
        t, p     = sp.ttest_ind(returns_on, returns_off) if len(returns_off) >= 5 else (0, 1)
        return {"label": label, "n": len(returns_on),
                "d1_avg": on_mean, "lift": lift, "sharpe": sharpe,
                "wr": wr, "p": p}

    with st.spinner("📊 실제 데이터 로딩 및 백테스트 계산 중..."):
        all_rows = []
        loaded_codes = []
        progress = st.progress(0)
        status_txt = st.empty()

        def _bt_fetch(code):
            """종목 하나 백테스트 행 반환"""
            df = load_bt_ohlcv(code, bt_start)
            if df is None or len(df) < 80:
                return code, None
            try:
                flags = calc_signal_flags(df)
                cl = df["close"].astype(float)
                d1 = cl.pct_change(1).shift(-1) * 100
                d2 = cl.pct_change(2).shift(-2) * 100
                rows = []
                for idx in range(60, len(df)-2):
                    row = {
                        "code": code,
                        "date": df.index[idx],
                        "d1": _safe_float(d1.iloc[idx]),
                        "d2": _safe_float(d2.iloc[idx]),
                    }
                    for sig in ["s1","s1_strong","s2","s2_strong","s3","s4","s5","s6","s6_strong","change"]:
                        row[sig] = bool(flags[sig].iloc[idx]) if sig in flags.columns else False
                    rows.append(row)
                return code, rows
            except Exception as e:
                return code, None

        # 병렬 로딩 (최대 20 workers)
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(_bt_fetch, code): code for code in bt_codes}
            done = 0
            for fut in futures:
                code = futures[fut]
                try:
                    c, rows = fut.result()
                    if rows:
                        all_rows.extend(rows)
                        loaded_codes.append(c)
                except: pass
                done += 1
                progress.progress(done / len(bt_codes))
                status_txt.caption(f"로딩 중... {done}/{len(bt_codes)} ({len(loaded_codes)}개 성공)")

        progress.empty()
        status_txt.empty()

    if not all_rows:
        st.error("데이터를 불러올 수 없습니다.")
    else:
        bt_df = pd.DataFrame(all_rows)
        total_days = len(bt_df)
        st.success(f"✅ {len(loaded_codes)}개 종목 / {total_days:,}개 봉 로딩 완료")

        # ── 둘 다 비교 모드: EMA 방식도 같이 계산 ──
        if bt_ma_mode == "둘 다 비교":
            st.subheader("📊 SMA vs EMA 비교")
            st.caption("같은 종목/기간에서 MA 방식만 바꿔서 신호 성과 비교")

            with st.spinner("EMA 방식 계산 중..."):
                all_rows_ema = []
                def _bt_fetch_ema(code):
                    df_tmp = load_bt_ohlcv(code, bt_start)
                    if df_tmp is None or len(df_tmp) < 80: return code, None
                    try:
                        flags = calc_signal_flags(df_tmp, ma_mode="EMA5+EMA20+SMA60")
                        cl_tmp = df_tmp["close"].astype(float)
                        d1 = cl_tmp.pct_change(1).shift(-1) * 100
                        d2 = cl_tmp.pct_change(2).shift(-2) * 100
                        rows = []
                        for idx in range(60, len(df_tmp)-2):
                            row = {"code": code, "date": df_tmp.index[idx],
                                   "d1": _safe_float(d1.iloc[idx]),
                                   "d2": _safe_float(d2.iloc[idx])}
                            for sig in ["s1","s1_strong","s2","s2_strong","s3","s4","s5","s6","s6_strong","change"]:
                                row[sig] = bool(flags[sig].iloc[idx]) if sig in flags.columns else False
                            rows.append(row)
                        return code, rows
                    except: return code, None

                with ThreadPoolExecutor(max_workers=20) as ex:
                    for fut in ex.map(_bt_fetch_ema, loaded_codes):
                        try:
                            c, rows = fut if isinstance(fut, tuple) else (None, None)
                            if rows: all_rows_ema.extend(rows)
                        except: pass

                # 직접 map으로 재시도
                if not all_rows_ema:
                    with ThreadPoolExecutor(max_workers=20) as ex:
                        results_ema = list(ex.map(_bt_fetch_ema, loaded_codes))
                    for c, rows in results_ema:
                        if rows: all_rows_ema.extend(rows)

            if all_rows_ema:
                bt_ema = pd.DataFrame(all_rows_ema)

                # 비교 테이블
                compare_rows = []
                sigs_compare = [
                    ("s1", "S1 BB수축"),
                    ("s2", "S2 거래량눌림"),
                    ("s3", "S3 정배열"),
                    ("s4", "S4 RSI다이버전스"),
                    ("s5", "S5 양봉전환"),
                ]
                base_d2_sma = bt_df["d2"].dropna().tolist()
                base_d2_ema = bt_ema["d2"].dropna().tolist()

                for sig, label in sigs_compare:
                    # SMA 방식
                    on_sma  = bt_df.loc[bt_df[sig]==True, "d2"].dropna().tolist()
                    off_sma = bt_df.loc[bt_df[sig]==False,"d2"].dropna().tolist()
                    # EMA 방식
                    on_ema  = bt_ema.loc[bt_ema[sig]==True, "d2"].dropna().tolist()
                    off_ema = bt_ema.loc[bt_ema[sig]==False,"d2"].dropna().tolist()

                    if not on_sma or not on_ema: continue
                    lift_sma = np.mean(on_sma) - np.mean(off_sma)
                    lift_ema = np.mean(on_ema) - np.mean(off_ema)
                    t1,p1 = sp.ttest_ind(on_sma, off_sma) if len(off_sma)>=5 else (0,1)
                    t2,p2 = sp.ttest_ind(on_ema, off_ema) if len(off_ema)>=5 else (0,1)
                    wr_sma = sum(1 for g in on_sma if g>0)/len(on_sma)*100
                    wr_ema = sum(1 for g in on_ema if g>0)/len(on_ema)*100

                    better = "EMA ✅" if lift_ema > lift_sma else ("SMA ✅" if lift_sma > lift_ema else "동일")
                    compare_rows.append({
                        "시그널": label,
                        "SMA 발화수": len(on_sma),
                        "SMA 리프트": f"{lift_sma:+.3f}%",
                        "SMA 승률": f"{wr_sma:.1f}%",
                        "SMA p값": f"{p1:.3f}",
                        "EMA 발화수": len(on_ema),
                        "EMA 리프트": f"{lift_ema:+.3f}%",
                        "EMA 승률": f"{wr_ema:.1f}%",
                        "EMA p값": f"{p2:.3f}",
                        "우위": better,
                    })

                if compare_rows:
                    st.dataframe(pd.DataFrame(compare_rows), use_container_width=True, hide_index=True)

                    # 최종 판정
                    ema_wins = sum(1 for r in compare_rows if "EMA" in r["우위"])
                    sma_wins = sum(1 for r in compare_rows if "SMA" in r["우위"])
                    if ema_wins > sma_wins:
                        st.success(f"✅ EMA 방식이 {ema_wins}/{len(compare_rows)}개 신호에서 우위 — EMA 적용 권장")
                    elif sma_wins > ema_wins:
                        st.info(f"➡️ SMA 방식이 {sma_wins}/{len(compare_rows)}개 신호에서 우위 — 현재 유지 권장")
                    else:
                        st.warning("⚠️ 두 방식 차이 없음 — 현재 유지")

                    # JSON 저장에 비교 결과 포함
                    st.session_state["ema_compare"] = compare_rows

        # ── 시그널별 결과 테이블 ──
        st.subheader("📊 S1~S5 실제 예측력")
        st.caption("리프트 = ON 종목 D+2 평균 − OFF 종목 D+2 평균 / 샤프 = 연환산 / p<0.05 = 통계적으로 유의미")

        results_table = []
        sigs = [
            ("s1",        "S1 BB수축(전체)",      "12~25"),
            ("s1_strong", "S1 BB강수축",          "25"),
            ("s2",        "S2 거래량눌림(전체)",  "10~20"),
            ("s2_strong", "S2 거래량강눌림",      "20"),
            ("s3",        "S3 정배열눌림목",      "12~25"),
            ("s4",        "S4 RSI다이버전스",     "12"),
            ("s5",        "S5 양봉전환",          "8"),
            ("s6",        "S6 거래량폭발후눌림",  "미정"),
            ("s6_strong", "S6 거래량폭발+재상승", "미정"),
            ("change",    "변화감지(오늘첫셋업)", "보정"),
        ]

        for sig_key, label, cur_score in sigs:
            # S6 등 새 컬럼이 없을 수 있어서 방어
            if sig_key not in bt_df.columns:
                continue
            on_d1  = bt_df.loc[bt_df[sig_key]==True,  "d1"].dropna().tolist()
            off_d1 = bt_df.loc[bt_df[sig_key]==False, "d1"].dropna().tolist()
            on_d2  = bt_df.loc[bt_df[sig_key]==True,  "d2"].dropna().tolist()
            off_d2 = bt_df.loc[bt_df[sig_key]==False, "d2"].dropna().tolist()
            if len(on_d2) < 5: continue

            lift_d2 = np.mean(on_d2) - np.mean(off_d2)
            sharpe  = np.mean(on_d2) / (np.std(on_d2)+1e-6) * np.sqrt(252)
            wr_d1   = sum(1 for r in on_d1 if r>0) / len(on_d1) * 100 if on_d1 else 0
            wr_d2   = sum(1 for r in on_d2 if r>0) / len(on_d2) * 100
            t,p     = sp.ttest_ind(on_d2, off_d2) if len(off_d2)>=5 else (0,1)
            sig_flag = "✅" if p<0.05 and lift_d2>0 else ("❌" if lift_d2<0 else "⚠️")

            results_table.append({
                "시그널":    label,
                "현재점수":  cur_score,
                "발화횟수":  len(on_d2),
                "D+1승률":   f"{wr_d1:.1f}%",
                "D+2승률":   f"{wr_d2:.1f}%",
                "D+2평균":   f"{np.mean(on_d2):+.2f}%",
                "리프트":    f"{lift_d2:+.2f}%",
                "샤프":      f"{sharpe:.2f}",
                "p값":       f"{p:.4f}",
                "유의성":    sig_flag,
            })

        if results_table:
            result_df = pd.DataFrame(results_table)
            st.dataframe(result_df, use_container_width=True, hide_index=True)

            # ── 과대/과소평가 분석 ──
            st.subheader("📐 점수 적정성 분석")
            cols = st.columns(3)
            over, under, ok = [], [], []
            for row in results_table:
                try:
                    lift = float(row["리프트"].replace("%",""))
                    p    = float(row["p값"])
                    # 현재점수 숫자 추출
                    score_str = row["현재점수"].replace("~","-")
                    score_num = int(score_str.split("-")[-1]) if "-" in score_str else int(score_str) if score_str.isdigit() else 15
                    if p>0.05 or lift<0:
                        over.append(f"{row['시그널']} ({row['현재점수']}점, 리프트{row['리프트']})")
                    elif lift>1.0 and score_num<15:
                        under.append(f"{row['시그널']} (리프트{row['리프트']}, 현재{row['현재점수']}점)")
                    else:
                        ok.append(row['시그널'])
                except: pass

            with cols[0]:
                st.markdown("**⬇️ 과대평가 (점수 낮춰야)**")
                for x in over: st.markdown(f"- {x}")
                if not over: st.markdown("없음")
            with cols[1]:
                st.markdown("**⬆️ 과소평가 (점수 올려야)**")
                for x in under: st.markdown(f"- {x}")
                if not under: st.markdown("없음")
            with cols[2]:
                st.markdown("**✅ 적정**")
                for x in ok: st.markdown(f"- {x}")

            # ── 조합별 성과 ──
            st.subheader("🔗 핵심 조합 성과")
            combo_results = []
            combos = [
                ("S1+S2",       (bt_df["s1"]) & (bt_df["s2"])),
                ("S1+S3",       (bt_df["s1"]) & (bt_df["s3"])),
                ("S2+S3",       (bt_df["s2"]) & (bt_df["s3"])),
                ("S1+S2+S3",    (bt_df["s1"]) & (bt_df["s2"]) & (bt_df["s3"])),
                ("S1+S2+S5",    (bt_df["s1"]) & (bt_df["s2"]) & (bt_df["s5"])),
                ("변화감지+S3", (bt_df["change"]) & (bt_df["s3"])),
                ("S3+S4",       (bt_df["s3"]) & (bt_df["s4"])),
                ("S1+S2+S3+S5", (bt_df["s1"]) & (bt_df["s2"]) & (bt_df["s3"]) & (bt_df["s5"])),
                ("S6(기본)",      bt_df["s6"] if "s6" in bt_df.columns else pd.Series(False, index=bt_df.index)),
                ("S6(강화)",      bt_df["s6_strong"] if "s6_strong" in bt_df.columns else pd.Series(False, index=bt_df.index)),
                ("S3+S6",        (bt_df["s3"]) & (bt_df["s6"] if "s6" in bt_df.columns else pd.Series(False, index=bt_df.index))),
                ("S4+S6",        (bt_df["s4"]) & (bt_df["s6"] if "s6" in bt_df.columns else pd.Series(False, index=bt_df.index))),
                ("S3+S4+S6",     (bt_df["s3"]) & (bt_df["s4"]) & (bt_df["s6"] if "s6" in bt_df.columns else pd.Series(False, index=bt_df.index))),
            ]
            base_d2 = bt_df["d2"].dropna().tolist()
            base_wr = sum(1 for r in base_d2 if r>0)/len(base_d2)*100
            combo_results.append({"조합":"전체(기준선)","발화수":len(base_d2),
                                   "D+2평균":f"{np.mean(base_d2):+.2f}%",
                                   "D+2승률":f"{base_wr:.1f}%","리프트":"기준","p값":"-"})
            for name, mask in combos:
                try:
                    subset = bt_df.loc[mask, "d2"].dropna().tolist()
                except Exception:
                    continue
                if len(subset) < 5: continue
                avg = np.mean(subset); wr = sum(1 for r in subset if r>0)/len(subset)*100
                lift = avg - np.mean(base_d2)
                t,p = sp.ttest_ind(subset, base_d2)
                combo_results.append({"조합":name,"발화수":len(subset),
                                       "D+2평균":f"{avg:+.2f}%","D+2승률":f"{wr:.1f}%",
                                       "리프트":f"{lift:+.2f}%","p값":f"{p:.4f}"})
            st.dataframe(pd.DataFrame(combo_results), use_container_width=True, hide_index=True)

            # ── 스캐너 전체 통과 조건 백테스트 ──
            st.subheader("🎯 핵심: 스캐너 전체 통과 조건 vs 전체 비교")
            st.caption("단독 시그널이 아닌 현재 스캐너가 실제로 통과시키는 종목의 D+2 성과")

            def full_scanner_pass(row):
                """현재 스캐너 통과 조건 재현 (백테스트 행 기준)"""
                # 셋업 계산
                sh = sum([row["s1"], row["s2"], row["s3"]])
                ss = sum([row["s1_strong"], row["s2_strong"],
                          (row["s3"] and row.get("s3_strong", row["s3"]))])
                th = 0  # 트리거 (S4/S5는 별도 컬럼 없어서 0)

                # 점수 재현 (현재 로직)
                score = 0
                if row["s1_strong"]: score += 25
                elif row["s1"]:      score += 12
                if row["s2_strong"]: score += 20
                elif row["s2"]:      score += 10
                if row["s3"]:        score += 25  # strong 가정
                if row["s4"]:        score += 12; th += 1
                if row["s5"]:        score += 8;  th += 1

                qual = (ss >= 1) or (th >= 1)
                return (sh >= 1) and qual and (score >= 38) and row["change"]

            # s3_strong 컬럼 추가 (s3 발화 = strong으로 간주)
            bt_df["s3_strong"] = bt_df["s3"]
            bt_df["s2_strong_col"] = bt_df["s2_strong"]

            scanner_mask = bt_df.apply(full_scanner_pass, axis=1)
            scanner_pass = bt_df.loc[scanner_mask, "d2"].dropna().tolist()
            scanner_fail = bt_df.loc[~scanner_mask, "d2"].dropna().tolist()
            base_all     = bt_df["d2"].dropna().tolist()

            sc_col1, sc_col2, sc_col3 = st.columns(3)
            def show_metric(col, label, lst, baseline=None):
                if not lst: return
                avg = np.mean(lst)
                wr  = sum(1 for g in lst if g>0)/len(lst)*100
                ag  = np.mean([g for g in lst if g>0]) if any(g>0 for g in lst) else 0
                al  = np.mean([g for g in lst if g<=0]) if any(g<=0 for g in lst) else 0
                ev  = wr/100*ag + (1-wr/100)*al
                lift_str = f"리프트 {avg-np.mean(baseline):+.2f}%p" if baseline else ""
                col.metric(label, f"D+2 {avg:+.2f}%", f"승률 {wr:.1f}% | {lift_str}")
                col.caption(f"종목수 {len(lst):,}개 | 기대값 {ev:+.2f}%")

            show_metric(sc_col1, "📊 전체 기준선", base_all)
            show_metric(sc_col2, "✅ 스캐너 통과", scanner_pass, base_all)
            show_metric(sc_col3, "❌ 스캐너 탈락", scanner_fail, base_all)

            if scanner_pass and scanner_fail:
                from scipy import stats as sp2
                t, p = sp2.ttest_ind(scanner_pass, scanner_fail)
                lift = np.mean(scanner_pass) - np.mean(base_all)
                wr_pass = sum(1 for g in scanner_pass if g>0)/len(scanner_pass)*100
                wr_base = sum(1 for g in base_all if g>0)/len(base_all)*100

                if p < 0.05 and lift > 0:
                    verdict = "✅ 스캐너가 실제로 좋은 종목 선별 중"
                    color   = "green"
                elif p < 0.05 and lift < 0:
                    verdict = "❌ 스캐너 통과 종목이 오히려 낮은 성과 — 로직 재설계 필요"
                    color   = "red"
                else:
                    verdict = "⚠️ 통계적으로 유의미한 차이 없음 — 스캐너 효과 불분명"
                    color   = "orange"

                st.markdown(f"""
<div style="background:#1e293b;padding:16px;border-radius:10px;border-left:5px solid {'#10b981' if color=='green' else '#ef4444' if color=='red' else '#f59e0b'};">
  <b style="font-size:16px;">{verdict}</b><br><br>
  통과 종목 D+2: <b>{np.mean(scanner_pass):+.2f}%</b> (승률 {wr_pass:.1f}%)<br>
  전체 기준선:   <b>{np.mean(base_all):+.2f}%</b> (승률 {wr_base:.1f}%)<br>
  리프트: <b>{lift:+.2f}%p</b> | t={t:.2f} | p={p:.4f}<br><br>
  <b>통과 종목수: {len(scanner_pass):,}개 / 전체 {len(base_all):,}개 ({len(scanner_pass)/len(base_all)*100:.1f}%)</b>
</div>""", unsafe_allow_html=True)

                # 점수 구간별 성과
                st.markdown("##### 점수 구간별 D+2 성과")
                score_rows = []
                def get_score(row):
                    s = 0
                    if row["s1_strong"]: s+=25
                    elif row["s1"]:      s+=12
                    if row["s2_strong"]: s+=20
                    elif row["s2"]:      s+=10
                    if row["s3"]:        s+=25
                    if row["s4"]:        s+=12
                    if row["s5"]:        s+=8
                    return s

                bt_df["score"] = bt_df.apply(get_score, axis=1)
                for lo_s, hi_s, label in [(0,38,"<38(미통과)"),(38,50,"38~49"),(50,65,"50~64"),(65,200,"65+")]:
                    sub = bt_df.loc[(bt_df["score"]>=lo_s)&(bt_df["score"]<hi_s), "d2"].dropna().tolist()
                    if not sub: continue
                    avg=np.mean(sub); wr=sum(1 for g in sub if g>0)/len(sub)*100
                    ag=np.mean([g for g in sub if g>0]) if any(g>0 for g in sub) else 0
                    al=np.mean([g for g in sub if g<=0]) if any(g<=0 for g in sub) else 0
                    ev=wr/100*ag+(1-wr/100)*al
                    score_rows.append({"점수구간":label,"종목수":len(sub),
                                       "D+2평균":f"{avg:+.2f}%","승률":f"{wr:.1f}%","기대값":f"{ev:+.2f}%"})
                if score_rows:
                    st.dataframe(pd.DataFrame(score_rows), use_container_width=True, hide_index=True)

            st.divider()

            # ── 시장환경 × 시총구간 × 조합 교차분석 ──
            st.subheader("🔬 심층 분석: 시장환경 × 시총구간 × 조합")
            st.caption("어떤 환경에서 어떤 종목에 어떤 신호가 먹히는지")

            # 코스피 지수 로딩 (시장환경 구분용)
            @st.cache_data(ttl=3600, show_spinner=False)
            def load_kospi_index(start):
                try:
                    df_idx = fdr.DataReader("KS11", start=start)
                    if df_idx is not None and len(df_idx) > 0:
                        df_idx.columns = [c.lower() for c in df_idx.columns]
                        df_idx["ret5"] = df_idx["close"].pct_change(5) * 100
                        return df_idx
                except: pass
                return None

            # 종목 시총 정보
            @st.cache_data(ttl=86400, show_spinner=False)
            def load_marcap_map():
                try:
                    listing = fdr.StockListing("KRX")
                    return dict(zip(listing["Code"], listing["Marcap"]))
                except: return {}

            with st.spinner("시장환경/시총 데이터 로딩..."):
                df_idx   = load_kospi_index(bt_start)
                marcap_m = load_marcap_map()

            if df_idx is not None and not bt_df.empty:
                # 날짜 인덱스 맞추기
                idx_ret = df_idx["ret5"].to_dict()

                def get_market_env(date):
                    try:
                        ret = idx_ret.get(pd.Timestamp(date), None)
                        if ret is None:
                            # 가장 가까운 날짜
                            dts = sorted(idx_ret.keys())
                            ts  = pd.Timestamp(date)
                            closest = min(dts, key=lambda x: abs(x-ts))
                            ret = idx_ret[closest]
                        if ret >  2: return "상승장(+2%↑)"
                        if ret < -2: return "하락장(-2%↓)"
                        return "횡보장"
                    except: return "횡보장"

                def get_marcap_tier(code):
                    m = marcap_m.get(str(code), 0)
                    if m >= 1e12:   return "대형(1조+)"
                    if m >= 1e11:   return "중형(1천억~1조)"
                    return "소형(~1천억)"

                bt_df["env"]   = bt_df["date"].apply(get_market_env)
                bt_df["tier"]  = bt_df["code"].apply(get_marcap_tier)

                # 시장환경별 분석
                st.markdown("##### 📈 시장환경별 신호 성과")
                env_rows = []
                for env in ["상승장(+2%↑)", "횡보장", "하락장(-2%↓)"]:
                    sub = bt_df[bt_df["env"]==env]
                    if len(sub) < 100: continue
                    base_d2 = sub["d2"].dropna().tolist()
                    for sig, label in [("s3","S3정배열"),("s4","S4RSI다이버"),
                                       ("s1","S1BB수축"),("s2","S2거래량눌림")]:
                        on  = sub.loc[sub[sig]==True,  "d2"].dropna().tolist()
                        off = sub.loc[sub[sig]==False, "d2"].dropna().tolist()
                        if len(on) < 20: continue
                        lift = np.mean(on) - np.mean(off)
                        t,p  = sp.ttest_ind(on, off) if len(off)>=5 else (0,1)
                        wr   = sum(1 for g in on if g>0)/len(on)*100
                        env_rows.append({
                            "시장환경":env, "시그널":label, "발화수":len(on),
                            "D+2평균":f"{np.mean(on):+.2f}%",
                            "승률":f"{wr:.1f}%",
                            "리프트":f"{lift:+.2f}%",
                            "p값":f"{p:.3f}",
                            "판정":"✅" if p<0.05 and lift>0 else ("❌" if p<0.05 and lift<0 else "⚠️"),
                        })
                if env_rows:
                    st.dataframe(pd.DataFrame(env_rows), use_container_width=True, hide_index=True)

                # 시총구간별 분석
                st.markdown("##### 🏢 시총구간별 신호 성과")
                tier_rows = []
                for tier in ["대형(1조+)", "중형(1천억~1조)", "소형(~1천억)"]:
                    sub = bt_df[bt_df["tier"]==tier]
                    if len(sub) < 50: continue
                    for sig, label in [("s3","S3정배열"),("s4","S4RSI다이버"),
                                       ("s1","S1BB수축"),("s2","S2거래량눌림")]:
                        on  = sub.loc[sub[sig]==True,  "d2"].dropna().tolist()
                        off = sub.loc[sub[sig]==False, "d2"].dropna().tolist()
                        if len(on) < 20: continue
                        lift = np.mean(on) - np.mean(off)
                        t,p  = sp.ttest_ind(on, off) if len(off)>=5 else (0,1)
                        wr   = sum(1 for g in on if g>0)/len(on)*100
                        tier_rows.append({
                            "시총구간":tier, "시그널":label, "발화수":len(on),
                            "D+2평균":f"{np.mean(on):+.2f}%",
                            "승률":f"{wr:.1f}%",
                            "리프트":f"{lift:+.2f}%",
                            "p값":f"{p:.3f}",
                            "판정":"✅" if p<0.05 and lift>0 else ("❌" if p<0.05 and lift<0 else "⚠️"),
                        })
                if tier_rows:
                    st.dataframe(pd.DataFrame(tier_rows), use_container_width=True, hide_index=True)

                # 교차분석: 시장환경 × 시총 × S3+S4 조합
                st.markdown("##### 🔗 교차분석: 시장환경 × 시총 × S3+S4 조합")
                cross_rows = []
                for env in ["상승장(+2%↑)", "횡보장", "하락장(-2%↓)"]:
                    for tier in ["대형(1조+)", "중형(1천억~1조)", "소형(~1천억)"]:
                        sub = bt_df[(bt_df["env"]==env) & (bt_df["tier"]==tier)]
                        if len(sub) < 30: continue
                        base = sub["d2"].dropna().tolist()
                        # S3+S4 조합
                        combo = sub.loc[(sub["s3"]==True)&(sub["s4"]==True), "d2"].dropna().tolist()
                        # S3만
                        s3only = sub.loc[(sub["s3"]==True)&(sub["s4"]==False), "d2"].dropna().tolist()
                        for label, lst in [("S3+S4조합",combo),("S3단독",s3only)]:
                            if len(lst) < 5: continue
                            lift = np.mean(lst) - np.mean(base)
                            t,p  = sp.ttest_ind(lst, base) if len(base)>=5 else (0,1)
                            cross_rows.append({
                                "시장환경":env, "시총":tier, "조합":label,
                                "발화수":len(lst),
                                "기준선":f"{np.mean(base):+.2f}%",
                                "조합D+2":f"{np.mean(lst):+.2f}%",
                                "리프트":f"{lift:+.2f}%",
                                "p값":f"{p:.3f}",
                                "판정":"✅" if p<0.05 and lift>0 else ("❌" if p<0.05 and lift<0 else "⚠️"),
                            })
                if cross_rows:
                    cross_df = pd.DataFrame(cross_rows)
                    # 유의미한 것 상단 정렬
                    cross_df["_lift_num"] = cross_df["리프트"].str.replace("%","").astype(float)
                    cross_df = cross_df.sort_values("_lift_num", ascending=False).drop("_lift_num",axis=1)
                    st.dataframe(cross_df, use_container_width=True, hide_index=True)

                    # 최적 조건 요약
                    best = cross_df.iloc[0] if len(cross_df)>0 else None
                    if best is not None:
                        st.info(f"📌 가장 좋은 조건: **{best['시장환경']}** × **{best['시총']}** × **{best['조합']}** → D+2 리프트 {best['리프트']}")
            else:
                st.warning("코스피 지수 데이터 로딩 실패 — 시장환경 분석 불가")

            st.divider()

            # ── 방향 A: 모멘텀 기반 신호 검증 ──
            st.subheader("🚀 방향 A: 모멘텀 기반 신호 검증")
            st.caption("이미 오르고 있는 종목 중 추가 상승 가능한 것 — 현재 스캐너와 반대 방향")

            def momentum_pass(row):
                """모멘텀 기반 통과 조건"""
                # S1: BB 수축 없음 (모멘텀 있어야)
                no_squeeze = not row["s1_strong"]
                # S2: 거래량 감소 없음 (거래량 있어야)
                no_vol_dry = not row["s2_strong"]
                # S3: 정배열 (상승 추세)
                uptrend = row["s3"]
                # S4: RSI 다이버전스 (반등 신호)
                rsi_ok = row["s4"]
                # 기본: 정배열 + 거래량 있음 + RSI 다이버전스
                return uptrend and no_vol_dry and rsi_ok

            def momentum_pass_v2(row):
                """모멘텀 v2: 정배열 + S5 양봉전환 (단순)"""
                return row["s3"] and row["s5"] and not row["s2_strong"]

            def momentum_pass_v3(row):
                """모멘텀 v3: S3 + S4 조합"""
                return row["s3"] and row["s4"]

            mom_results = []
            for label, fn in [
                ("정배열+RSI다이버전스+거래량있음", momentum_pass),
                ("정배열+양봉전환(거래량눌림제외)", momentum_pass_v2),
                ("정배열+RSI다이버전스(단순)", momentum_pass_v3),
            ]:
                mask = bt_df.apply(fn, axis=1)
                subset = bt_df.loc[mask, "d2"].dropna().tolist()
                if len(subset) < 10: continue
                avg=np.mean(subset); wr=sum(1 for g in subset if g>0)/len(subset)*100
                ag=np.mean([g for g in subset if g>0]) if any(g>0 for g in subset) else 0
                al=np.mean([g for g in subset if g<=0]) if any(g<=0 for g in subset) else 0
                ev=wr/100*ag+(1-wr/100)*al
                lift=avg-np.mean(base_all)
                from scipy import stats as sp3
                t,p=sp3.ttest_ind(subset, base_all)
                mom_results.append({
                    "조건":label,"종목수":len(subset),
                    "D+2평균":f"{avg:+.2f}%","승률":f"{wr:.1f}%",
                    "기대값":f"{ev:+.2f}%","리프트":f"{lift:+.2f}%",
                    "p값":f"{p:.4f}",
                    "판정":"✅유의미" if p<0.05 and lift>0 else ("❌역효과" if p<0.05 and lift<0 else "⚠️무의미"),
                })
            if mom_results:
                st.dataframe(pd.DataFrame(mom_results), use_container_width=True, hide_index=True)

            st.divider()

            # ── 방향 B: 보유기간 연장 검증 (D+5, D+10) ──
            st.subheader("📅 방향 B: 보유기간별 성과 (D+1~D+10)")
            st.caption("현재 신호 체계가 더 긴 보유기간에서 유효한지 검증")

            # D+5, D+10 수익률 계산
            bt_loaded_map = {}
            for code in loaded_codes:
                df_tmp = load_bt_ohlcv(code, bt_start)
                if df_tmp is not None:
                    bt_loaded_map[code] = df_tmp

            # 기존 bt_df에 D+5, D+10 추가
            @st.cache_data(ttl=3600, show_spinner=False)
            def calc_extended_returns(codes_tuple, start):
                rows_ext = []
                for code in codes_tuple:
                    df_tmp = load_bt_ohlcv(code, start)
                    if df_tmp is None or len(df_tmp) < 80: continue
                    try:
                        flags = calc_signal_flags(df_tmp, ma_mode=bt_ma_mode if 'bt_ma_mode' in dir() else '현재(SMA)')
                        cl_tmp = df_tmp["close"].astype(float)
                        d = {}
                        for n in [1,2,3,5,7,10]:
                            d[f"d{n}"] = cl_tmp.pct_change(n).shift(-n) * 100
                        for idx in range(60, len(df_tmp)-11):
                            row = {"code":code}
                            for n in [1,2,3,5,7,10]:
                                row[f"d{n}"] = _safe_float(d[f"d{n}"].iloc[idx])
                            for sig in ["s1","s1_strong","s2","s2_strong","s3","s4","s5","s6","s6_strong","change"]:
                                row[sig] = bool(flags[sig].iloc[idx]) if sig in flags.columns else False
                            rows_ext.append(row)
                    except: pass
                return pd.DataFrame(rows_ext)

            with st.spinner("D+1~D+10 수익률 계산 중..."):
                bt_ext = calc_extended_returns(tuple(loaded_codes), bt_start)

            if not bt_ext.empty:
                # 현재 스캐너 통과 조건 적용
                bt_ext["scanner"] = bt_ext.apply(full_scanner_pass, axis=1)

                hold_rows = []
                for n in [1,2,3,5,7,10]:
                    col = f"d{n}"
                    if col not in bt_ext.columns: continue
                    all_d  = bt_ext[col].dropna().tolist()
                    pass_d = bt_ext.loc[bt_ext["scanner"]==True,  col].dropna().tolist()
                    fail_d = bt_ext.loc[bt_ext["scanner"]==False, col].dropna().tolist()
                    if not pass_d: continue
                    from scipy import stats as sp4
                    t,p = sp4.ttest_ind(pass_d, fail_d)
                    lift = np.mean(pass_d) - np.mean(all_d)
                    wr_pass = sum(1 for g in pass_d if g>0)/len(pass_d)*100
                    wr_all  = sum(1 for g in all_d  if g>0)/len(all_d)*100
                    hold_rows.append({
                        "보유기간": f"D+{n}",
                        "전체평균":  f"{np.mean(all_d):+.2f}%",
                        "통과평균":  f"{np.mean(pass_d):+.2f}%",
                        "전체승률":  f"{wr_all:.1f}%",
                        "통과승률":  f"{wr_pass:.1f}%",
                        "리프트":    f"{lift:+.2f}%",
                        "p값":       f"{p:.4f}",
                        "판정":      "✅유의미" if p<0.05 and lift>0 else ("❌역효과" if p<0.05 and lift<0 else "⚠️무의미"),
                    })

                if hold_rows:
                    st.dataframe(pd.DataFrame(hold_rows), use_container_width=True, hide_index=True)

                    # 모멘텀 전략도 같은 기간으로
                    st.markdown("##### 모멘텀 전략 보유기간별 비교")
                    bt_ext["momentum"] = bt_ext.apply(momentum_pass_v3, axis=1)
                    mom_hold = []
                    for n in [1,2,3,5,7,10]:
                        col=f"d{n}"
                        if col not in bt_ext.columns: continue
                        all_d  = bt_ext[col].dropna().tolist()
                        mom_d  = bt_ext.loc[bt_ext["momentum"]==True, col].dropna().tolist()
                        cur_d  = bt_ext.loc[bt_ext["scanner"]==True,  col].dropna().tolist()
                        if not mom_d: continue
                        from scipy import stats as sp5
                        t,p=sp5.ttest_ind(mom_d, all_d)
                        lift=np.mean(mom_d)-np.mean(all_d)
                        wr=sum(1 for g in mom_d if g>0)/len(mom_d)*100
                        mom_hold.append({
                            "보유기간":f"D+{n}",
                            "기준선":f"{np.mean(all_d):+.2f}%",
                            "현재스캐너":f"{np.mean(cur_d):+.2f}%" if cur_d else "-",
                            "모멘텀전략":f"{np.mean(mom_d):+.2f}%",
                            "모멘텀승률":f"{wr:.1f}%",
                            "모멘텀리프트":f"{lift:+.2f}%",
                            "p값":f"{p:.4f}",
                            "판정":"✅" if p<0.05 and lift>0 else ("❌" if p<0.05 and lift<0 else "⚠️"),
                        })
                    if mom_hold:
                        st.dataframe(pd.DataFrame(mom_hold), use_container_width=True, hide_index=True)

                    # 최종 수정 방향 판정
                    st.subheader("🏁 최종 수정 방향 판정")
                    best_cur  = max([float(r["리프트"].replace("%","")) for r in hold_rows], default=-999)
                    best_mom  = max([float(r["모멘텀리프트"].replace("%","")) for r in mom_hold], default=-999) if mom_hold else -999
                    best_n_cur = next((r["보유기간"] for r in hold_rows if float(r["리프트"].replace("%",""))==best_cur), "?")
                    best_n_mom = next((r["판정"] for r in mom_hold if float(r["모멘텀리프트"].replace("%",""))==best_mom), "?") if mom_hold else "?"

                    if best_cur > 0.1 and best_cur > best_mom:
                        direction = f"✅ 방향B 채택: 현재 로직 유지 + 보유기간 {best_n_cur}로 연장 (리프트 {best_cur:+.2f}%)"
                        color = "#10b981"
                    elif best_mom > 0.1 and best_mom > best_cur:
                        direction = f"✅ 방향A 채택: 모멘텀 기반 신호로 전환 (리프트 {best_mom:+.2f}%)"
                        color = "#3b82f6"
                    elif best_cur > 0.1 and best_mom > 0.1:
                        direction = f"✅ A+B 병행: 두 전략 모두 유효 — 시장 국면에 따라 전환"
                        color = "#f59e0b"
                    else:
                        direction = "⚠️ 두 방향 모두 유의미한 개선 없음 — 신호 체계 전면 재검토 필요"
                        color = "#ef4444"

                    st.markdown(f"""
<div style="background:#1e293b;padding:16px;border-radius:10px;border-left:5px solid {color};">
  <b style="font-size:16px;">{direction}</b>
</div>""", unsafe_allow_html=True)

            st.divider()

            # ── 핵심: 코드 수정 필요 여부 자동 판단 ──
            st.subheader("🤖 코드 수정 필요 여부 자동 판단")
            st.caption("백테스트 결과 기반 — 지금 코드 그대로 써도 되는지 알려줘요")

            # 현재 가중치
            cur_w = load_weights()

            # 판단 기준
            # 1. 스캐너 통과 종목 리프트가 기준선 대비 유의미하게 낮으면 → 수정 필요
            # 2. 개별 신호 방향이 현재 가중치와 크게 다르면 → 수정 필요
            # 3. 둘 다 괜찮으면 → 유지

            needs_update = []
            ok_items = []

            # 스캐너 전체 통과 성과 재계산
            def _full_pass(row):
                sh = sum([row["s1"], row["s2"], row["s3"]])
                th_ = sum([row["s4"], row["s5"]])
                s = 0
                if row.get("s1_strong",False): s+=cur_w["s1_strong"]
                elif row["s1"]: s+=cur_w["s1_weak"]
                if row.get("s2_strong",False): s+=cur_w["s2_strong"]
                elif row["s2"]: s+=cur_w["s2_weak"]
                if row["s3"]: s+=cur_w["s3_strong"]
                if row["s4"]: s+=cur_w["s4"]; th_+=1
                if row["s5"]: s+=cur_w["s5"]; th_+=1
                # S6 가산점 (검증 중)
                if row.get("s6_strong", False): s += 10
                elif row.get("s6", False):      s += 5
                core = row["s3"] and row["s4"]
                return core and (sh>=1) and (s>=cur_w["min_pass_score"])

            bt_df["_pass"] = bt_df.apply(_full_pass, axis=1)
            pass_d2 = bt_df.loc[bt_df["_pass"]==True,  "d2"].dropna().tolist()
            all_d2  = bt_df["d2"].dropna().tolist()

            from scipy import stats as sp_j
            scanner_lift = np.mean(pass_d2) - np.mean(all_d2) if pass_d2 else -999
            scanner_p    = sp_j.ttest_ind(pass_d2, all_d2)[1] if len(pass_d2)>10 else 1.0

            # 판단 1: 스캐너 전체 성과
            if scanner_p < 0.05 and scanner_lift < -0.1:
                needs_update.append({
                    "항목": "스캐너 전체 통과 성과",
                    "현재": f"리프트 {scanner_lift:+.2f}%p (p={scanner_p:.3f})",
                    "문제": "통과 종목이 기준선보다 유의미하게 낮음",
                    "권고": "pass 조건 또는 핵심 신호 재검토"
                })
            elif scanner_p < 0.05 and scanner_lift > 0.1:
                ok_items.append(f"스캐너 전체 성과 ✅ 기준선 대비 +{scanner_lift:.2f}%p (p={scanner_p:.3f})")
            else:
                ok_items.append(f"스캐너 전체 성과 ⚠️ 유의미한 차이 없음 (리프트 {scanner_lift:+.2f}%p)")

            # 판단 2: 개별 신호 방향 vs 현재 가중치
            sig_map = {
                "s1": ("S1 BB수축",  cur_w["s1_strong"], 15),
                "s2": ("S2 거래량눌림", cur_w["s2_strong"], 12),
                "s3": ("S3 정배열",  cur_w["s3_strong"], 20),
                "s4": ("S4 RSI다이버전스", cur_w["s4"], 8),
                "s5": ("S5 양봉전환", cur_w["s5"], 4),
            }
            for sig, (label, cur_score, threshold) in sig_map.items():
                on  = bt_df.loc[bt_df[sig]==True,  "d2"].dropna().tolist()
                off = bt_df.loc[bt_df[sig]==False, "d2"].dropna().tolist()
                if len(on) < 20: continue
                lift = np.mean(on) - np.mean(off)
                t,p  = sp_j.ttest_ind(on, off) if len(off)>=5 else (0,1)

                # 현재 점수가 높은데 리프트가 음수 → 과대평가
                if p < 0.05 and lift < -0.05 and cur_score >= threshold:
                    needs_update.append({
                        "항목": label,
                        "현재": f"점수 {cur_score}점 / 리프트 {lift:+.2f}% (p={p:.3f})",
                        "문제": f"점수 높은데 실제 음수 리프트",
                        "권고": f"점수 {int(cur_score*0.7)}점으로 하향 검토"
                    })
                # 현재 점수가 낮은데 리프트가 양수 → 과소평가
                elif p < 0.05 and lift > 0.05 and cur_score < threshold:
                    needs_update.append({
                        "항목": label,
                        "현재": f"점수 {cur_score}점 / 리프트 {lift:+.2f}% (p={p:.3f})",
                        "문제": "점수 낮은데 실제 양수 리프트",
                        "권고": f"점수 {int(cur_score*1.3)}점으로 상향 검토"
                    })
                else:
                    direction = "양수✅" if lift>0 else "음수⚠️"
                    ok_items.append(f"{label}: 리프트 {lift:+.2f}% ({direction}), 현재 점수 {cur_score}점 적정")

            # 결과 출력
            if needs_update:
                st.markdown(f"""
<div style="background:#1e293b;padding:16px;border-radius:10px;
            border-left:5px solid #ef4444;margin-bottom:12px;">
  <b style="font-size:16px;color:#ef4444;">⚠️ 코드 수정 권고 — {len(needs_update)}개 항목</b>
</div>""", unsafe_allow_html=True)
                for item in needs_update:
                    with st.expander(f"🔧 {item['항목']} — {item['문제']}"):
                        st.markdown(f"**현재 상태:** {item['현재']}")
                        st.markdown(f"**문제:** {item['문제']}")
                        st.markdown(f"**권고:** {item['권고']}")
            else:
                st.markdown(f"""
<div style="background:#1e293b;padding:16px;border-radius:10px;
            border-left:5px solid #10b981;margin-bottom:12px;">
  <b style="font-size:16px;color:#10b981;">✅ 현재 코드 유지 — 수정 불필요</b><br>
  <span style="color:#94a3b8;font-size:13px;">모든 신호 방향이 현재 가중치와 일치합니다</span>
</div>""", unsafe_allow_html=True)

            if ok_items:
                with st.expander("✅ 정상 항목 보기"):
                    for x in ok_items:
                        st.markdown(f"- {x}")

            # 백테스트 결과 저장 (복붙용 + 기록용)
            st.divider()
            st.subheader("💾 백테스트 결과 저장")
            st.caption("저장하면 다음에 Claude에게 붙여넣기만 하면 돼요")

            import json as json_bt
            bt_summary = {
                "실행일시": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "종목수": len(loaded_codes),
                "봉수": len(bt_df),
                "시작일": bt_start,
                "스캐너_리프트": round(scanner_lift, 4),
                "스캐너_p값": round(scanner_p, 4),
                "수정필요": len(needs_update) > 0,
                "수정항목수": len(needs_update),
                "신호별_결과": {},
                "현재_가중치": {k:v for k,v in cur_w.items() if not k.startswith("_")},
                "수정_권고": needs_update,
            }
            for sig, (label, cur_score, _) in sig_map.items():
                on  = bt_df.loc[bt_df[sig]==True, "d2"].dropna().tolist()
                off = bt_df.loc[bt_df[sig]==False,"d2"].dropna().tolist()
                if not on: continue
                lift = np.mean(on)-np.mean(off)
                t,p  = sp_j.ttest_ind(on,off) if len(off)>=5 else (0,1)
                bt_summary["신호별_결과"][label] = {
                    "발화수": len(on), "리프트": round(lift,4), "p값": round(p,4)
                }

            bt_json_str = json_bt.dumps(bt_summary, ensure_ascii=False, indent=2)

            # 저장 버튼
            save_col1, save_col2 = st.columns(2)
            if save_col1.button("💾 로컬 저장 (backtest_history.json)"):
                hist_file = "backtest_history.json"
                history = []
                if os.path.exists(hist_file):
                    try:
                        with open(hist_file,"r",encoding="utf-8") as f:
                            history = json.load(f)
                    except: pass
                history.append(bt_summary)
                history = history[-10:]  # 최근 10회만 보관
                with open(hist_file,"w",encoding="utf-8") as f:
                    json.dump(history, f, ensure_ascii=False, indent=2)
                st.success(f"✅ backtest_history.json 저장 완료 (총 {len(history)}회 기록)")

            # Claude에게 붙여넣기용
            st.markdown("**📋 Claude에게 붙여넣기용 (복사해서 대화창에 붙여넣으세요)**")
            st.code(bt_json_str, language="json")

            st.divider()

            # ── 점수 재설계 권고 (기존 유지) ──
            st.subheader("💡 실제 데이터 기반 점수 재설계 권고")
            st.caption("리프트 크고 p<0.05인 시그널은 점수 상향, 그 반대는 하향 권고")
            reco_cols = st.columns(2)
            with reco_cols[0]:
                st.markdown("**현재 점수 체계**")
                for row in results_table:
                    st.markdown(f"- {row['시그널']}: **{row['현재점수']}점** (리프트 {row['리프트']}, p={row['p값']})")
            with reco_cols[1]:
                st.markdown("**재설계 권고**")
                for row in results_table:
                    try:
                        lift = float(row["리프트"].replace("%",""))
                        p    = float(row["p값"])
                        score_str = row["현재점수"]
                        if p<0.05 and lift>1.5:   change_txt="⬆️ 상향 권장"
                        elif p<0.05 and lift>0:   change_txt="➡️ 유지"
                        elif p>0.1 or lift<=0:    change_txt="⬇️ 하향 권장"
                        else:                     change_txt="➡️ 유지"
                        st.markdown(f"- {row['시그널']}: {score_str}점 → **{change_txt}**")
                    except: pass
