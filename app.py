"""
Tae Scanner — 퀀트 폭등 예측 엔진 (v4)
==================================
v3 기반, 최소 수정:
1. [버그 수정] 해외 가격 yfinance fallback이 오래된 일봉 종가를 가져오던 문제 해결
   - 기존: history(period="5d", interval="1d") 마지막 행을 그대로 사용 → 저유동성 종목에서
     yfinance가 최신 거래일 데이터를 늦게 채우는 경우, 며칠 전 가격이 "최신가"처럼 표시됨
   - 수정: 1) fast_info.last_price 우선 시도 (장중/장외 모두)
           2) 1분봉(1d/1m) 마지막 값 시도 — 가장 최신 실제 체결가에 가까움
           3) 그래도 실패하면 일봉 종가로 fallback (최후 수단)
         + 일봉 fallback 시, 그 날짜가 너무 오래됐으면(3일 초과) source에 경고 표시
2. 그 외 원본(v3) 코드 구조 100% 유지
"""

import streamlit as st
import pyupbit
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import json, os
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
KRX_API_KEY     = ""   # ← data.krx.co.kr AUTH_KEY
FINNHUB_API_KEY = ""   # ← Finnhub 키 (없으면 yfinance)

# ============================================================
# ★ 스캔/필터 튜닝값
# ============================================================
KR_SCAN_TOP_N     = 300
CRYPTO_SCAN_LIMIT = 80

THRESHOLDS = {
    "KR":     {"min_vol": 50_000,  "max_rsi": 78, "max_gain5": 0.15,
               "max_ma20_dev": 1.15, "max_hi60": 0.95, "min_pass_score": 40},
    "US":     {"min_vol": 500_000, "max_rsi": 78, "max_gain5": 0.15,
               "max_ma20_dev": 1.15, "max_hi60": 0.95, "min_pass_score": 40},
    "CRYPTO": {"min_value": 1_000_000_000, "max_rsi": 80, "max_gain5": 0.25,
               "max_ma20_dev": 1.20, "max_hi60": 0.97, "min_pass_score": 40},
}

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
# 2. 국내 기준가 — KRX 공식 OpenAPI (원본 동일)
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
    """원본과 동일: KRX → yfinance → OHLCV 안전망"""
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
#    ★ v4: yfinance fallback 순서 수정 (버그 패치)
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
    ★ 버그 수정 핵심 함수
    기존 코드는 history(period="5d", interval="1d")의 마지막 행을 바로 썼는데,
    저유동성/소형주는 yfinance가 당일(혹은 최근) 일봉을 늦게 채우는 경우가 많아
    실제로는 며칠 전 종가가 "최신가"처럼 리턴되는 문제가 있었음.

    수정된 우선순위:
    1) fast_info.last_price        — 가장 신뢰도 높은 실시간/직전 체결가
    2) 1분봉(1d, interval=1m) 마지막 값 — 장중이면 거의 실시간, 장 마감 직후면 마지막 체결가
    3) 일봉(5d, interval=1d) 마지막 값 — 최후 수단. 이때 해당 날짜가 오늘/전날이 아니면
       "(주의:해당일자 오래됨)" 표시를 붙여 사용자가 데이터가 stale함을 알 수 있게 함
    """
    try:
        t = yf.Ticker(ticker)

        # 1) fast_info 우선
        try:
            p = getattr(t.fast_info, "last_price", 0)
            if p and float(p) > 0:
                return float(p), "yfinance(실시간)"
        except:
            pass

        # 2) 1분봉 — 가장 최신 체결가에 가까움
        try:
            df_min = t.history(period="1d", interval="1m")
            if not df_min.empty:
                last_close = df_min["Close"].dropna()
                if not last_close.empty and float(last_close.iloc[-1]) > 0:
                    return float(last_close.iloc[-1]), "yfinance(1분봉)"
        except:
            pass

        # 3) 일봉 — 최후 수단 + stale 여부 체크
        try:
            df_day = t.history(period="5d", interval="1d")
            if not df_day.empty:
                last_close = df_day["Close"].dropna()
                if not last_close.empty and float(last_close.iloc[-1]) > 0:
                    last_idx = df_day.index[-1]
                    try:
                        last_date = last_idx.tz_localize(None) if last_idx.tzinfo else last_idx
                        days_old = (datetime.now() - last_date.to_pydatetime()).days
                    except:
                        days_old = 0
                    tag = "yfinance(일봉종가)" if days_old <= 3 else f"yfinance(일봉종가·{days_old}일전·주의)"
                    return float(last_close.iloc[-1]), tag
        except:
            pass

    except:
        pass
    return 0.0, "실패"


@st.cache_data(ttl=60, show_spinner=False)
def get_us_price(ticker: str) -> tuple:
    """
    v4: Finnhub(장중c/장마감pc) → yfinance(실시간→1분봉→일봉 순)
    Finnhub가 0을 주거나(키없음/요청제한/미지원 종목) 신뢰 불가 시 yfinance로 즉시 전환.
    """
    market_open = is_us_market_open()
    q = _fh_fetch_raw(ticker)
    c, pc = q["c"], q["pc"]

    if market_open and c > 0:
        return c, "Finnhub(정규장)"
    if not market_open and pc > 0:
        return pc, "Finnhub(전일정규장종가)"
    if c > 0:
        return c, "Finnhub(시간외·참고용)"

    # Finnhub 완전 실패 → yfinance 신선도 우선 fallback
    price, src = _yf_fresh_price(ticker)
    if price > 0:
        return price, src

    return 0.0, "실패"


@st.cache_data(ttl=60, show_spinner=False)
def get_us_price_batch(tickers: tuple) -> dict:
    with ThreadPoolExecutor(max_workers=min(len(tickers), 10)) as ex:
        futs = {t: ex.submit(get_us_price, t) for t in tickers}
        return {t: fut.result() for t, fut in futs.items()}


# ============================================================
# 4. OHLCV 로더 (원본 동일)
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def load_krx_listing():
    return fdr.StockListing("KRX")

@st.cache_data(ttl=1800, show_spinner=False)
def load_ohlcv_kr(code: str) -> pd.DataFrame | None:
    try:
        df = fdr.DataReader(code, start="2024-01-01")
        if df is not None and len(df) >= 60:
            df.columns = [c.lower() for c in df.columns]
            return df
    except:
        pass
    return None

@st.cache_data(ttl=1800, show_spinner=False)
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
# 5. 마켓 현황 (원본 동일)
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
# 6. ★ 퀀트 폭등 예측 엔진 (원본 v3 로직 100% 유지)
# ============================================================
def _safe_float(val, default=0.0) -> float:
    try:
        v = float(val)
        return v if np.isfinite(v) else default
    except:
        return default


def quant_predict(df: pd.DataFrame, market: str = "KR") -> dict:
    OUT = {
        "score": 0, "grade": "F", "signals": [],
        "pass": False, "buy_min": 0.0, "buy_max": 0.0,
        "rsi": 50.0, "current": 0.0,
        "s1": False, "s2": False, "s3": False, "s4": False, "s5": False,
    }
    th = THRESHOLDS.get(market, THRESHOLDS["KR"])
    try:
        if df is None or len(df) < 60:
            OUT["signals"].append("❌ 데이터 부족")
            return OUT

        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        cl = df["close"].astype(float)
        hi = df["high"].astype(float)
        lo = df["low"].astype(float)
        vo = df["volume"].astype(float)

        current = _safe_float(cl.iloc[-1])
        # current가 0이면 직전 유효값으로 복구
        if current <= 0:
            valid = cl[cl > 0]
            current = _safe_float(valid.iloc[-1]) if not valid.empty else 0.0
        OUT["current"] = current

        rejected = False

        # ── 유동성 필터 ──
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

        # ── 이미 급등 필터 ──
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
        delta = cl.diff()
        gain_s = delta.clip(lower=0).rolling(14).mean()
        loss_s = (-delta.clip(upper=0)).rolling(14).mean()
        rsi_s  = 100 - 100 / (1 + gain_s / loss_s.replace(0, np.nan))
        rsi    = _safe_float(rsi_s.iloc[-1], default=50.0)
        OUT["rsi"] = rsi
        if rsi > th["max_rsi"]:
            OUT["signals"].append(f"❌ RSI 과열 ({rsi:.1f})")
            rejected = True

        score = 0

        # ── [S1] BB 변동성 수축 ──
        bb_std   = cl.rolling(20).std()
        bb_mean  = cl.rolling(20).mean().replace(0, np.nan)
        bb_width = (bb_std * 2) / bb_mean
        bw_now   = _safe_float(bb_width.iloc[-1])
        bw_avg   = _safe_float(bb_width.rolling(20).mean().iloc[-1])
        s1 = bw_avg > 0 and bw_now > 0 and bw_now < bw_avg * 0.75
        OUT["s1"] = s1
        if s1:
            score += 30
            OUT["signals"].append(f"✅ [S1] BB 변동성 수축 — 폭발 직전 에너지 응축 (밴드폭 {bw_now:.3f} < {bw_avg*0.75:.3f})")
        elif bw_avg > 0 and bw_now > 0 and bw_now < bw_avg * 0.90:
            score += 10
            OUT["signals"].append("🔶 [S1] BB 밴드 소폭 수축 중")
        else:
            OUT["signals"].append("⬜ [S1] 변동성 수축 없음")

        # ── [S2] 거래량 눌림 후 폭발 ──
        vol_ma5  = _safe_float(vo.rolling(5).mean().iloc[-1])
        vol_ma20 = _safe_float(vo.rolling(20).mean().iloc[-1])
        vol_now  = _safe_float(vo.iloc[-1])
        vol_dry   = vol_ma20 > 0 and vol_ma5 < vol_ma20 * 0.70
        vol_burst = vol_ma5  > 0 and vol_now  > vol_ma5  * 1.50
        s2 = vol_dry and vol_burst
        OUT["s2"] = s2
        if s2:
            score += 30
            OUT["signals"].append(f"✅ [S2] 거래량 눌림 후 폭발 ({vol_now/vol_ma20*100:.0f}% of MA20)")
        elif vol_burst:
            score += 12
            OUT["signals"].append("🔶 [S2] 거래량 급증 (눌림 미확인)")
        elif vol_dry:
            score += 8
            OUT["signals"].append("🔶 [S2] 거래량 눌림 확인 (폭발 대기)")
        else:
            OUT["signals"].append("⬜ [S2] 거래량 신호 없음")

        # ── [S3] 정배열 + 눌림목 ──
        aligned   = ma5 > 0 and ma20 > 0 and ma60 > 0 and ma5 > ma20 > ma60
        near_ma20 = ma20 > 0 and current > 0 and abs(current - ma20) / ma20 <= 0.05
        s3 = aligned and near_ma20
        OUT["s3"] = s3
        if s3:
            score += 20
            OUT["signals"].append("✅ [S3] 정배열 + MA20 눌림목 — 최적 매수 타이밍")
        elif near_ma20:
            score += 10
            OUT["signals"].append("🔶 [S3] MA20 근처 (정배열 미완)")
        elif aligned:
            score += 5
            OUT["signals"].append("🔶 [S3] 정배열 (눌림목 이탈)")
        else:
            OUT["signals"].append("⬜ [S3] 정배열 없음")

        # ── [S4] RSI 강세 다이버전스 ──
        s4 = False
        try:
            price_window = cl.iloc[-10:]
            rsi_window   = rsi_s.iloc[-10:]
            p_low_prev = _safe_float(price_window.iloc[:5].min())
            p_low_now  = _safe_float(price_window.iloc[5:].min())
            r_low_prev = _safe_float(rsi_window.iloc[:5].min(), 50.0)
            r_low_now  = _safe_float(rsi_window.iloc[5:].min(), 50.0)
            s4 = (p_low_now < p_low_prev) and (r_low_now > r_low_prev + 2)
            OUT["s4"] = s4
            if s4:
                score += 15
                OUT["signals"].append("✅ [S4] RSI 강세 다이버전스 — 반등 임박")
            else:
                OUT["signals"].append("⬜ [S4] RSI 다이버전스 없음")
        except:
            OUT["signals"].append("⬜ [S4] RSI 다이버전스 계산 실패")

        # ── [S5] 캔들 반등 패턴 ──
        s5 = False
        try:
            o1   = _safe_float(df["open"].iloc[-1])
            c1_v = _safe_float(cl.iloc[-1])
            h1   = _safe_float(hi.iloc[-1])
            l1   = _safe_float(lo.iloc[-1])
            o2   = _safe_float(df["open"].iloc[-2])
            c2_v = _safe_float(cl.iloc[-2])
            body  = abs(c1_v - o1)
            lower = (o1 - l1) if c1_v >= o1 else (c1_v - l1)
            upper = (h1 - c1_v) if c1_v >= o1 else (h1 - o1)
            hammer   = body > 0 and lower > body * 2 and upper < body * 0.5
            bull_rev = c2_v < o2 and c1_v > o1
            s5 = hammer or bull_rev
            OUT["s5"] = s5
            if s5:
                score += 10
                pat = "망치형 캔들" if hammer else "양봉 전환"
                OUT["signals"].append(f"✅ [S5] {pat} — 단기 반등 신호")
            else:
                OUT["signals"].append("⬜ [S5] 반등 캔들 패턴 없음")
        except:
            OUT["signals"].append("⬜ [S5] 캔들 패턴 계산 실패")

        # ── RSI 구간 보너스 ──
        if 35 <= rsi <= 55:
            score += 10
            OUT["signals"].append(f"✅ RSI 매수 구간 ({rsi:.1f})")
        elif rsi < 35:
            score += 5
            OUT["signals"].append(f"🔶 RSI 과매도 ({rsi:.1f})")
        else:
            OUT["signals"].append(f"⬜ RSI 구간 외 ({rsi:.1f})")

        # ──────────────────────────────────────────────
        # 매수구간 (v3에서 0원 버그 수정된 로직 유지)
        # ──────────────────────────────────────────────
        raw_low  = min(ma20, ma10) * 0.985 if min(ma20, ma10) > 0 else 0
        raw_high = max(ma20, ma5)  * 1.010 if max(ma20, ma5)  > 0 else 0

        if current > 0 and raw_low > 0 and raw_high > 0:
            cap_high = current * 1.05
            cap_low  = current * 0.90
            buy_low  = max(min(raw_low,  cap_high), cap_low)
            buy_high = max(min(raw_high, cap_high), buy_low)
        elif current > 0:
            buy_low  = current * 0.97
            buy_high = current * 1.02
        elif raw_low > 0 and raw_high > 0:
            buy_low, buy_high = raw_low, raw_high
        else:
            buy_low = buy_high = 0.0

        # 최종 안전망
        if (buy_low <= 0 or buy_high <= 0) and current > 0:
            buy_low  = current * 0.97
            buy_high = current * 1.02

        OUT["buy_min"] = round(buy_low,  4)
        OUT["buy_max"] = round(buy_high, 4)
        OUT["score"]   = int(score)

        combo = s1 or s2 or s3
        OUT["pass"]  = (not rejected) and combo and score >= th["min_pass_score"]
        OUT["grade"] = ("A+" if score >= 90 else "A" if score >= 75
                        else "B+" if score >= 60 else "B" if score >= 40
                        else "C")

    except Exception as e:
        OUT["signals"].append(f"오류: {e}")
    return OUT


# ============================================================
# 7. 스캐너 (원본 동일)
# ============================================================
US_WATCHLIST = [
    "NVDA","META","GOOGL","AMZN","MSFT","AMD","TSLA",
    "PYPL","SQ","SOFI","HOOD","UPST","AFRM",
    "PLTR","ASTS","HIMS","AXSM","RIVN","SMCI","ARM",
]

def summarize_skips(skips: list) -> dict:
    cnt = Counter()
    for s in skips:
        why = s.get("why", "기타")
        key = why.split("(")[0].strip().lstrip("❌🔶⬜ ").strip()
        cnt[key] += 1
    return dict(cnt.most_common())


@st.cache_data(ttl=1800, show_spinner=False)
def scan_kr() -> tuple:
    listing = load_krx_listing()
    targets = listing[listing["Marcap"] > 3e11].nlargest(KR_SCAN_TOP_N, "Marcap")
    codes   = list(zip(targets["Code"], targets["Name"]))

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
        return {
            "_skip":    False,
            "종목":     name,
            "코드":     code,
            "등급":     r["grade"],
            "점수":     r["score"],
            "현재가":   int(price),
            "RSI":      round(r["rsi"], 1),
            "매수구간": f"₩{int(r['buy_min']):,} ~ ₩{int(r['buy_max']):,}",
            "목표가":   int(price * 1.08),
            "손절가":   int(price * 0.93),
            "signals":  r["signals"],
            "source":   src,
            "s_flags":  [r["s1"], r["s2"], r["s3"], r["s4"], r["s5"]],
        }

    with ThreadPoolExecutor(max_workers=30) as ex:
        raw = list(ex.map(_fetch, codes))
    skips = [r for r in raw if r.get("_skip")]
    top3  = sorted([r for r in raw if not r.get("_skip")], key=lambda x: x["점수"], reverse=True)[:3]
    return top3, skips


@st.cache_data(ttl=1800, show_spinner=False)
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
        return {
            "_skip":    False,
            "종목":     ticker,
            "등급":     r["grade"],
            "점수":     r["score"],
            "현재가":   round(price, 2),
            "RSI":      round(r["rsi"], 1),
            "매수구간": f"${r['buy_min']:,.2f} ~ ${r['buy_max']:,.2f}",
            "목표가":   round(price * 1.08, 2),
            "손절가":   round(price * 0.93, 2),
            "signals":  r["signals"],
            "source":   src,
            "s_flags":  [r["s1"], r["s2"], r["s3"], r["s4"], r["s5"]],
        }

    with ThreadPoolExecutor(max_workers=20) as ex:
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
            return {
                "_skip":    False,
                "종목":     coin.replace("KRW-", ""),
                "등급":     r["grade"],
                "점수":     r["score"],
                "현재가":   c,
                "RSI":      round(r["rsi"], 1),
                "매수구간": f"₩{int(r['buy_min']):,} ~ ₩{int(r['buy_max']):,}",
                "목표가":   round(c * 1.10, 0),
                "손절가":   round(c * 0.93, 0),
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
# 8. 포트폴리오 조회 — 원본 로직 복원
# ============================================================
def get_portfolio_data(name: str) -> dict:
    name = name.strip().upper()

    # 국내 6자리
    if name.isdigit() and len(name) == 6:
        price, vol, src = get_kr_price_with_fallback(name)
        df = load_ohlcv_kr(name)
        if df is not None:
            df2 = df.copy()
            if price > 0:
                df2.iloc[-1, df2.columns.get_loc("close")] = price
            r = quant_predict(df2, "KR")
            if price <= 0:
                price = r["current"]
            listing = load_krx_listing()
            row     = listing[listing["Code"] == name]
            label   = row["Name"].values[0] if not row.empty else name
            return {"label": f"{name} ({label})", "curr": price,
                    "score": r["score"], "grade": r["grade"],
                    "rsi": round(r["rsi"],1), "currency": "KRW",
                    "stop": int(price*0.93), "target": int(price*1.08),
                    "buy_min": r["buy_min"], "buy_max": r["buy_max"],
                    "source": src, "ok": price > 0, "signals": r["signals"]}

    # 해외 — get_us_price (v4: 신선도 우선 fallback 적용됨)
    price, src = get_us_price(name)
    df = load_ohlcv_us(name)
    if df is not None:
        df2 = df.copy()
        if price > 0:
            df2.iloc[-1, df2.columns.get_loc("close")] = price
        else:
            price = float(df2["close"].dropna().iloc[-1]); src = "OHLCV종가"
        r = quant_predict(df2, "US")
        return {"label": f"{name} ({src})", "curr": price,
                "score": r["score"], "grade": r["grade"],
                "rsi": round(r["rsi"],1), "currency": "USD",
                "stop": round(price*0.93,2), "target": round(price*1.08,2),
                "buy_min": r["buy_min"], "buy_max": r["buy_max"],
                "source": src, "ok": price > 0, "signals": r["signals"]}
    if price > 0:
        return {"label": f"{name} ({src}·지표없음)", "curr": price,
                "score": 0, "grade": "-", "rsi": 50.0, "currency": "USD",
                "stop": round(price*0.93,2), "target": round(price*1.08,2),
                "buy_min": 0.0, "buy_max": 0.0,
                "source": src, "ok": True, "signals": []}

    # 코인
    try:
        df_c = pyupbit.get_ohlcv(f"KRW-{name}", interval="day", count=120)
        if df_c is not None and not df_c.empty:
            r = quant_predict(df_c, "CRYPTO")
            c = r["current"]
            if c > 0:
                return {"label": f"{name} (업비트)", "curr": c,
                        "score": r["score"], "grade": r["grade"],
                        "rsi": round(r["rsi"],1), "currency": "KRW",
                        "stop": round(c*0.93,0), "target": round(c*1.10,0),
                        "buy_min": r["buy_min"], "buy_max": r["buy_max"],
                        "source": "Upbit", "ok": True, "signals": r["signals"]}
    except:
        pass

    return {"label": None, "curr": 0, "score": 0, "grade": "F",
            "rsi": 0, "currency": "USD", "stop": 0, "target": 0,
            "buy_min": 0.0, "buy_max": 0.0,
            "source": "실패", "ok": False, "signals": []}


# ============================================================
# 9. UI (원본 동일 + buy_min/max 포트폴리오 표시 추가)
# ============================================================
fg_val, fg_txt, exchange = get_market_status()

st.sidebar.title("🛡️ Tae Scanner")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.sidebar.metric("🇺🇸 미국 정규장", "OPEN" if is_us_market_open() else "CLOSED")

with st.sidebar.expander("🔑 API 상태", expanded=True):
    st.write("KRX:", "✅ 연결됨 (장마감 후 확정종가)" if KRX_API_KEY else "❌ 키 없음 (yfinance 대체)")
    st.write("Finnhub:", "✅ 연결됨" if FINNHUB_API_KEY else "❌ 키 없음 (yfinance 대체)")

st.title("🚀 Tae's Quant 폭등 예측 스캐너")
st.caption("📌 BB수축+거래량폭발+정배열눌림목+RSI다이버전스+캔들패턴 | 핵심신호 1개+점수40 통과 | v4: 해외 가격 yfinance fallback 신선도 버그 수정")

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

with st.form(key="portfolio_form", clear_on_submit=True):
    c1, c2, c3 = st.columns([2, 1, 1])
    n_in = c1.text_input("종목코드 / 티커 / 코인",
                         placeholder="국내: 005930  해외: AAPL  코인: BTC")
    b_in = c2.number_input("내 평단가", min_value=0.0, step=0.01, format="%.4f")
    if c3.form_submit_button("➕ 추가"):
        if n_in and b_in > 0:
            st.session_state.my_portfolio.append(
                {"name": n_in.strip().upper(), "buy": float(b_in)})
            save_portfolio(st.session_state.my_portfolio)
            st.rerun()
        else:
            st.warning("종목명과 평단가를 입력하세요.")

if st.session_state.my_portfolio:
    to_remove = None
    for i, p in enumerate(st.session_state.my_portfolio):
        name, buy = p["name"], p["buy"]
        d = get_portfolio_data(name)

        if not d["ok"] or d["curr"] <= 0:
            st.error(f"⚠️ {name} 조회 실패 — 출처: {d['source']}")
            if st.button(f"❌ 삭제 ({name})", key=f"del_err_{i}"):
                to_remove = i
            continue

        curr   = d["curr"]
        profit = (curr - buy) / buy * 100 if buy > 0 else 0
        is_kr  = d["currency"] == "KRW"
        sym    = "₩" if is_kr else "$"
        fmt    = (lambda v: f"{sym}{int(v):,}") if is_kr else (lambda v: f"{sym}{v:,.2f}")
        p_color = "#10b981" if profit >= 0 else "#ef4444"
        grade_color = {"A+": "#f59e0b", "A": "#10b981", "B+": "#3b82f6",
                       "B": "#94a3b8", "C": "#64748b"}.get(d["grade"], "#64748b")

        bmin = d.get("buy_min", 0)
        bmax = d.get("buy_max", 0)
        if is_kr:
            buy_range_str = f"₩{int(bmin):,} ~ ₩{int(bmax):,}" if bmin > 0 else "—"
        else:
            buy_range_str = f"${bmin:,.2f} ~ ${bmax:,.2f}" if bmin > 0 else "—"

        # ★ v4: 가격이 오래된 데이터일 경우 경고 배지 표시
        stale_warn = "주의" in d.get("source", "") or "오래됨" in d.get("source", "")
        warn_badge = ("<span style='background:#ef4444;color:#fff;font-size:10px;"
                      "padding:2px 6px;border-radius:4px;margin-left:8px;'>⚠️ 시세 지연 가능</span>") if stale_warn else ""

        st.markdown(f"""
<div style="background:#1e293b;padding:20px;border-radius:12px;
            border-left:6px solid {grade_color};margin-bottom:16px;">
  <h3 style="margin:0 0 12px 0;">📈 {d['label']}
    <span style="font-size:14px;background:{grade_color};color:#000;
                 padding:2px 8px;border-radius:4px;margin-left:8px;">
      {d['grade']}등급 {d['score']}점
    </span>{warn_badge}
  </h3>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px;">
    <div><div style="font-size:11px;color:#94a3b8;">내 평단가</div>
         <div style="font-size:20px;font-weight:bold;">{fmt(buy)}</div></div>
    <div><div style="font-size:11px;color:#94a3b8;">현재가</div>
         <div style="font-size:20px;font-weight:bold;">{fmt(curr)}</div></div>
    <div><div style="font-size:11px;color:#94a3b8;">수익률</div>
         <div style="font-size:20px;font-weight:bold;color:{p_color};">
           {'+' if profit>=0 else ''}{profit:.2f}%</div></div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px;margin-bottom:8px;">
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">매수구간</div>
      <div style="color:#10b981;font-weight:bold;font-size:11px;">{buy_range_str}</div></div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">목표가 (+8%)</div>
      <div style="color:#3b82f6;font-weight:bold;">{fmt(d['target'])}</div></div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">손절가 (-7%)</div>
      <div style="color:#ef4444;font-weight:bold;">{fmt(d['stop'])}</div></div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">RSI</div>
      <div style="font-weight:bold;">{d['rsi']}</div></div>
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
S_LABELS = ["S1:BB수축", "S2:거래량폭발", "S3:정배열눌림", "S4:RSI다이버전스", "S5:반등캔들"]

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
