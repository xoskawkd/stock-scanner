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
KRX_API_KEY     = "08810EEE8F724ED7BB7D35A2B79190956C2FFCB7"   # ← data.krx.co.kr AUTH_KEY
FINNHUB_API_KEY = "e196a49253d0408cadf883e01f6b78d9"   # ← Finnhub 키 (없으면 yfinance)

# ============================================================
# ★ 스캔/필터 튜닝값
# ============================================================
KR_SCAN_TOP_N     = 300
CRYPTO_SCAN_LIMIT = 80

# ============================================================
# [v7] THRESHOLDS — 추격 가드는 소폭만 완화, 감도 개선 중심
# ============================================================
THRESHOLDS = {
    "KR":     {"min_vol": 50_000,  "max_rsi": 80, "max_gain5": 0.18,
               "max_ma20_dev": 1.18, "max_hi60": 0.97, "min_pass_score": 38},
    "US":     {"min_vol": 500_000, "max_rsi": 80, "max_gain5": 0.18,
               "max_ma20_dev": 1.18, "max_hi60": 0.97, "min_pass_score": 38},
    "CRYPTO": {"min_value": 1_000_000_000, "max_rsi": 82, "max_gain5": 0.30,
               "max_ma20_dev": 1.25, "max_hi60": 0.98, "min_pass_score": 38},
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
    """KRX → yfinance → OHLCV 안전망 (OHLCV는 최최후 수단)"""
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
    우선순위:
    1) fast_info.last_price
    2) 1분봉(1d, interval=1m)
    3) 일봉(5d) — 3일 이내만 허용, 초과 시 None 반환(stale 차단)
    """
    try:
        t = yf.Ticker(ticker)
        try:
            p = getattr(t.fast_info, "last_price", 0)
            if p and float(p) > 0:
                return float(p), "yfinance(실시간)"
        except:
            pass

        try:
            df_min = t.history(period="1d", interval="1m")
            if not df_min.empty:
                last_close = df_min["Close"].dropna()
                if not last_close.empty and float(last_close.iloc[-1]) > 0:
                    return float(last_close.iloc[-1]), "yfinance(1분봉)"
        except:
            pass

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
                    if days_old > 3:
                        return None, f"yfinance(일봉{days_old}일전·stale차단)"
                    return float(last_close.iloc[-1]), "yfinance(일봉종가)"
        except:
            pass

    except:
        pass
    return None, "실패"


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

        # ── [S1] 변동성 수축 (BB) — 예측 셋업 ──
        bb_std   = cl.rolling(20).std()
        bb_mean  = cl.rolling(20).mean().replace(0, np.nan)
        bb_width = (bb_std * 2) / bb_mean
        bw_now   = _safe_float(bb_width.iloc[-1])
        bw_avg   = _safe_float(bb_width.rolling(20).mean().iloc[-1])
        s1 = False
        if bw_avg > 0 and bw_now > 0:
            if bw_now < bw_avg * 0.80:
                s1 = True; setup_hits += 1; setup_strong += 1; score += 25
                OUT["signals"].append(
                    f"✅ [S1] 변동성 강수축 — 폭발 직전 응축 ({bw_now:.3f} < {bw_avg*0.80:.3f})")
            elif bw_now < bw_avg * 0.92:
                s1 = True; setup_hits += 1; score += 12
                OUT["signals"].append(
                    f"🔶 [S1] 변동성 수축 진행 ({bw_now:.3f} < {bw_avg*0.92:.3f})")
            else:
                OUT["signals"].append("⬜ [S1] 변동성 수축 없음")
        else:
            OUT["signals"].append("⬜ [S1] 변동성 계산 불가")
        OUT["s1"] = s1

        # ── [S2] 거래량 눌림 (셋업) + 증가 (트리거) 분리 ──
        vol_ma5  = _safe_float(vo.rolling(5).mean().iloc[-1])
        vol_ma20 = _safe_float(vo.rolling(20).mean().iloc[-1])
        vol_now  = _safe_float(vo.iloc[-1])

        s2 = False
        if vol_ma20 > 0 and vol_ma5 < vol_ma20 * 0.75:
            s2 = True; setup_hits += 1; setup_strong += 1; score += 20
            OUT["signals"].append(
                f"✅ [S2] 거래량 눌림(매집) — 매도세 소진 ({vol_ma5/vol_ma20*100:.0f}% of MA20)")
        elif vol_ma20 > 0 and vol_ma5 < vol_ma20 * 0.90:
            s2 = True; setup_hits += 1; score += 10
            OUT["signals"].append(
                f"🔶 [S2] 거래량 완만한 눌림 ({vol_ma5/vol_ma20*100:.0f}% of MA20)")
        else:
            OUT["signals"].append("⬜ [S2] 거래량 눌림 없음")
        OUT["s2"] = s2

        # S2T — 거래량 증가 트리거 (추격 아님: 가격 가드가 상단에서 이미 차단)
        if vol_ma5 > 0 and vol_now > vol_ma5 * 1.50:
            trigger_hits += 1; score += 10
            OUT["signals"].append(
                f"➕ [S2T] 거래량 증가 트리거 ({vol_now/vol_ma5*100:.0f}% of MA5)")
        elif vol_ma5 > 0 and vol_now > vol_ma5 * 1.20:
            trigger_hits += 1; score += 5
            OUT["signals"].append(
                f"➕ [S2T] 거래량 소폭 증가 ({vol_now/vol_ma5*100:.0f}% of MA5)")

        # ── [S3] 추세 눌림목 (예측 셋업) ──
        aligned_full = ma5 > 0 and ma20 > 0 and ma60 > 0 and ma5 > ma20 > ma60
        midterm_up   = ma20 > 0 and ma60 > 0 and ma20 > ma60
        near_ma20_5  = ma20 > 0 and current > 0 and abs(current - ma20) / ma20 <= 0.05
        near_ma20_7  = ma20 > 0 and current > 0 and abs(current - ma20) / ma20 <= 0.07
        s3 = False
        if aligned_full and near_ma20_5:
            s3 = True; setup_hits += 1; setup_strong += 1; score += 25
            OUT["signals"].append("✅ [S3] 정배열 + MA20 눌림목 — 최적 매수 타이밍")
        elif midterm_up and near_ma20_5:
            s3 = True; setup_hits += 1; score += 15
            OUT["signals"].append("🔶 [S3] 중기 상승 + MA20 눌림목")
        elif near_ma20_7:
            s3 = True; setup_hits += 1; score += 8
            OUT["signals"].append("🔶 [S3] MA20 인근 되돌림 (추세 미완)")
        else:
            OUT["signals"].append("⬜ [S3] 추세 눌림목 없음")
        OUT["s3"] = s3

        # ── [S4] RSI 강세 다이버전스 (트리거) ──
        s4 = False
        try:
            price_window = cl.iloc[-10:]
            rsi_window   = rsi_s.iloc[-10:]
            p_low_prev = _safe_float(price_window.iloc[:5].min())
            p_low_now  = _safe_float(price_window.iloc[5:].min())
            r_low_prev = _safe_float(rsi_window.iloc[:5].min(), 50.0)
            r_low_now  = _safe_float(rsi_window.iloc[5:].min(), 50.0)
            s4 = (p_low_now < p_low_prev) and (r_low_now > r_low_prev + 2)
            if s4:
                trigger_hits += 1; score += 12
                OUT["signals"].append("✅ [S4] RSI 강세 다이버전스 — 반등 임박")
            else:
                OUT["signals"].append("⬜ [S4] RSI 다이버전스 없음")
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
                        trigger_hits += 1; score += 8
                        pat = "망치형" if hammer else "양봉전환"
                        OUT["signals"].append(f"✅ [S5] {pat} 캔들 — 단기 반등 신호")
                    else:
                        OUT["signals"].append("⬜ [S5] 반등 캔들 패턴 없음")
        except Exception as e5:
            OUT["s5"] = False
            OUT["signals"].append(f"⬜ [S5] 캔들 패턴 계산 실패 ({e5})")
        OUT["s5"] = s5

        # ── RSI 구간 보너스 ──
        if 35 <= rsi <= 55:
            score += 10
            OUT["signals"].append(f"✅ RSI 매수 구간 ({rsi:.1f})")
        elif rsi < 35:
            score += 6
            OUT["signals"].append(f"🔶 RSI 과매도 ({rsi:.1f})")
        else:
            OUT["signals"].append(f"⬜ RSI 구간 외 ({rsi:.1f})")

        # ── 매수구간 ──
        raw_low  = min(ma20, ma10) * 0.985 if min(ma20, ma10) > 0 else 0
        raw_high = max(ma20, ma5)  * 1.010 if max(ma20, ma5)  > 0 else 0
        if current > 0 and raw_low > 0 and raw_high > 0:
            cap_high = current * 1.05
            cap_low  = current * 0.90
            buy_low  = max(min(raw_low,  cap_high), cap_low)
            buy_high = max(min(raw_high, cap_high), buy_low)
        elif current > 0:
            buy_low, buy_high = current * 0.97, current * 1.02
        elif raw_low > 0 and raw_high > 0:
            buy_low, buy_high = raw_low, raw_high
        else:
            buy_low = buy_high = 0.0
        if (buy_low <= 0 or buy_high <= 0) and current > 0:
            buy_low, buy_high = current * 0.97, current * 1.02

        OUT["buy_min"] = round(buy_low,  4)
        OUT["buy_max"] = round(buy_high, 4)
        OUT["score"]   = int(score)

        # ── 통과 게이트 (예측형) ──
        # 추격 방지 = 상단 rejected 필터 담당
        # 셋업 ≥1 요구 → 신호 없이 점수만 채운 통과 방지
        OUT["pass"] = (not rejected) and (setup_hits >= 1) and (score >= th["min_pass_score"])

        # ── 등급 ── 셋업 강도 + 트리거 유무로 계층화
        if setup_strong >= 2 and trigger_hits >= 1:
            grade = "A+"
        elif setup_hits >= 2 and trigger_hits >= 1:
            grade = "A"
        elif setup_hits >= 2:
            grade = "B+"
        elif setup_hits >= 1 and score >= 50:
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
            return {
                "label":    f"{name} ({label})",
                "curr":     curr,
                "score":    r["score"],
                "grade":    r["grade"],
                "rsi":      round(r["rsi"], 1),
                "currency": "KRW",
                "stop":     int(curr * 0.93) if curr > 0 else 0,
                "target":   int(curr * 1.08) if curr > 0 else 0,
                "buy_min":  r["buy_min"],
                "buy_max":  r["buy_max"],
                "source":   src + ("⚠️지연" if is_ohlcv_fallback else ""),
                "ok":       curr > 0 and not is_ohlcv_fallback,
                "signals":  r["signals"],
            }
        if price > 0:
            return {
                "label": f"{name} ({src}·지표없음)", "curr": price,
                "score": 0, "grade": "-", "rsi": 50.0, "currency": "KRW",
                "stop": int(price * 0.93), "target": int(price * 1.08),
                "buy_min": 0.0, "buy_max": 0.0,
                "source": src, "ok": not is_ohlcv_fallback, "signals": [],
            }
        return {"label": None, "curr": 0, "score": 0, "grade": "F",
                "rsi": 0, "currency": "KRW", "stop": 0, "target": 0,
                "buy_min": 0.0, "buy_max": 0.0, "source": "실패", "ok": False, "signals": []}

    # ── 해외 티커 ──
    market_open = is_us_market_open()
    q = _fh_fetch_raw(name)
    fh_c, fh_pc = q["c"], q["pc"]

    if market_open and fh_c > 0:
        price, src = fh_c, "Finnhub(정규장)"
    elif not market_open and fh_pc > 0:
        price, src = fh_pc, "Finnhub(전일정규장종가)"
    elif fh_c > 0:
        price, src = fh_c, "Finnhub(시간외·참고용)"
    else:
        yf_price, yf_src = _yf_fresh_price(name)
        if yf_price is not None and yf_price > 0:
            price, src = yf_price, yf_src
        else:
            price, src = 0.0, yf_src

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
                "buy_min": r["buy_min"], "buy_max": r["buy_max"],
                "source": src, "ok": False, "signals": r["signals"],
            }
        return {
            "label":    f"{name} ({src})",
            "curr":     curr,
            "score":    r["score"],
            "grade":    r["grade"],
            "rsi":      round(r["rsi"], 1),
            "currency": "USD",
            "stop":     round(curr * 0.93, 2),
            "target":   round(curr * 1.08, 2),
            "buy_min":  r["buy_min"],
            "buy_max":  r["buy_max"],
            "source":   src,
            "ok":       curr > 0,
            "signals":  r["signals"],
        }
    if price > 0:
        return {
            "label": f"{name} ({src}·지표없음)", "curr": price,
            "score": 0, "grade": "-", "rsi": 50.0, "currency": "USD",
            "stop": round(price * 0.93, 2), "target": round(price * 1.08, 2),
            "buy_min": 0.0, "buy_max": 0.0,
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
                    "stop":     round(c * 0.93, 0),
                    "target":   round(c * 1.10, 0),
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
    st.write("KRX:", "✅ 연결됨 (장마감 후 확정종가)" if KRX_API_KEY else "❌ 키 없음 (yfinance 대체)")
    st.write("Finnhub:", "✅ 연결됨" if FINNHUB_API_KEY else "❌ 키 없음 (yfinance 대체)")

st.title("🚀 Tae's Quant 폭등 예측 스캐너")

# ★ v7 캡션 — 실제 로직과 일치
st.caption(
    "📌 예측 모델 (추격매수 아님) | "
    "셋업(예측): BB수축·거래량눌림·추세눌림목 — 셋업 ≥1 + 점수 38↑ 통과 | "
    "트리거(가점): 거래량증가·RSI다이버전스·반등캔들 | "
    "v7: 셋업/트리거 분리·단계별 가점·추격가드 유지"
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

        stale_warn = any(kw in d.get("source", "") for kw in ["주의","오래됨","stale","지연"])
        warn_badge = (
            "<span style='background:#ef4444;color:#fff;font-size:10px;"
            "padding:2px 6px;border-radius:4px;margin-left:8px;'>⚠️ 시세 지연 가능</span>"
        ) if stale_warn else ""

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
