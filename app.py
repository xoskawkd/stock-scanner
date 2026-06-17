"""
Tae Scanner — 퀀트 폭등 예측 엔진 (v3)
==================================
[v3 주요 수정]
1. ★ 매수구간 0원 버그 수정
   - current=0일 때 cap_high/cap_low도 0이 되어 buy_min/max가 전부 0원이 되던 버그
   - current<=0이면 raw MA값 직접 사용, 0도 아닌지 추가 검증

2. ★ 국내 장중 실시간 가격 — 다중 소스 우선순위
   우선순위: KIS WebSocket(장중) → KRX 확정종가(장마감) → yfinance → OHLCV
   - is_kr_market_open() 으로 KST 09:00~15:30 판별
   - 장중에는 yfinance 1분봉 last close를 최우선 실시간 소스로 활용
   - KIS 오픈 API 키가 있으면 진짜 실시간 호가 사용 (선택)

3. ★ 폭등 예측 로직 개선
   - quant_predict: current<=0이면 OHLCV 마지막 close로 복구 후 계속 진행
   - S1(BB수축) 판정 로직 안정화: NaN/0 방어
   - S2(거래량 폭발): vol_dry 없어도 burst만으로 부분 점수 유지
   - 매수구간: MA 기반 raw값과 current 둘 다 유효한 경우만 cap 적용
   - 매수구간이 현재가보다 높으면 current 기반으로 재계산

4. ★ scan_kr에서 OHLCV 마지막 close를 quant_predict current로 보정 후
   get_kr_price_with_fallback 실시간 가격으로 덮어쓰는 순서 명확화

5. 사이드바: 장중/장마감 실시간 상태 표시
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
KIS_APP_KEY     = ""   # ← 한국투자증권 오픈API App Key (선택, 장중 실시간)
KIS_APP_SECRET  = ""   # ← 한국투자증권 오픈API App Secret
KIS_ACCOUNT_NO  = ""   # ← 계좌번호 앞 8자리

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
# 2. 시장 시간 판별
# ============================================================
def is_kr_market_open() -> bool:
    """KST 09:00~15:30 평일 여부 (한국 주식 정규장)"""
    if ZoneInfo is None:
        return False
    try:
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    except Exception:
        return False
    if now_kst.weekday() >= 5:
        return False
    open_t  = now_kst.replace(hour=9,  minute=0,  second=0, microsecond=0)
    close_t = now_kst.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now_kst <= close_t

def is_us_market_open() -> bool:
    """ET 09:30~16:00 평일 여부"""
    if ZoneInfo is None:
        return True
    try:
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return True
    if now_et.weekday() >= 5:
        return False
    open_t  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now_et <= close_t

# ============================================================
# 3. 국내 실시간 가격 — 장중/장마감 분기 처리
# ============================================================

# ── 3-A. KIS 오픈API 실시간 (선택, 키 있을 때만) ──
@st.cache_data(ttl=1800, show_spinner=False)
def _get_kis_access_token() -> str:
    """KIS 오픈API OAuth 토큰 발급 (30분 캐시)"""
    if not (KIS_APP_KEY and KIS_APP_SECRET):
        return ""
    try:
        r = requests.post(
            "https://openapi.koreainvestment.com:9443/oauth2/tokenP",
            json={"grant_type": "client_credentials",
                  "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET},
            timeout=5,
        ).json()
        return r.get("access_token", "")
    except:
        return ""

@st.cache_data(ttl=10, show_spinner=False)
def get_kis_realtime_price(code: str) -> tuple:
    """KIS 주식현재가 시세 조회 (장중 실시간, 10초 캐시)"""
    token = _get_kis_access_token()
    if not token:
        return 0.0, 0.0, "KIS키없음"
    try:
        r = requests.get(
            "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-price",
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "FHKST01010100",
            },
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
            timeout=4,
        ).json()
        out = r.get("output", {})
        price = float(out.get("stck_prpr", 0) or 0)
        vol   = float(out.get("acml_vol", 0) or 0)
        if price > 0:
            return price, vol, "KIS실시간"
    except:
        pass
    return 0.0, 0.0, "KIS실패"

# ── 3-B. KRX 공식 OpenAPI (장마감 확정종가) ──
@st.cache_data(ttl=600, show_spinner=False)
def get_krx_daily_snapshot() -> tuple:
    """KRX 전종목 일별 확정종가 스냅샷. 장중에는 전일 데이터 반환."""
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

# ── 3-C. yfinance 장중 실시간 (주요 Fallback) ──
@st.cache_data(ttl=30, show_spinner=False)
def get_yf_realtime_kr(code: str) -> tuple:
    """yfinance 1분봉 마지막 종가 (장중 준실시간, 30초 캐시)"""
    try:
        suffix = ".KS" if code[:2] in ["00","01","02","03","04","05","06","07","08"] else ".KQ"
        t  = yf.Ticker(f"{code}{suffix}")
        df = t.history(period="1d", interval="1m")
        if not df.empty:
            return float(df["Close"].iloc[-1]), float(df["Volume"].sum()), "yfinance(1분봉)"
        p = getattr(t.fast_info, "last_price", 0)
        if p and float(p) > 0:
            return float(p), 0.0, "yfinance(fast_info)"
    except:
        pass
    return 0.0, 0.0, "yfinance실패"

# ── 3-D. 통합 국내 가격 조회 (장중/장마감 자동 분기) ──
@st.cache_data(ttl=15, show_spinner=False)
def get_kr_price_with_fallback(code: str) -> tuple:
    """
    장중(KST 09:00~15:30):
      KIS 실시간 → yfinance 1분봉 → KRX 전일종가 → OHLCV
    장마감:
      KRX 확정종가 → yfinance 일봉 → OHLCV
    """
    kr_open = is_kr_market_open()

    if kr_open:
        # [1] KIS 실시간 (키 있을 때)
        if KIS_APP_KEY:
            p, v, s = get_kis_realtime_price(code)
            if p > 0:
                return p, v, s

        # [2] yfinance 1분봉
        p, v, s = get_yf_realtime_kr(code)
        if p > 0:
            return p, v, s

    # [3] KRX 확정종가
    p, v, s = get_krx_price(code)
    if p > 0:
        return p, v, s

    # [4] yfinance 일봉 최종
    try:
        suffix = ".KS" if code[:2] in ["00","01","02","03","04","05","06","07","08"] else ".KQ"
        t  = yf.Ticker(f"{code}{suffix}")
        df = t.history(period="5d", interval="1d")
        if not df.empty:
            return float(df["Close"].iloc[-1]), float(df["Volume"].iloc[-1]), "yfinance(일봉)"
    except:
        pass

    # [5] 이미 로드된 OHLCV 마지막 종가 (최종 안전망)
    df_ohlcv = load_ohlcv_kr(code)
    if df_ohlcv is not None and not df_ohlcv.empty:
        return float(df_ohlcv["close"].iloc[-1]), float(df_ohlcv["volume"].iloc[-1]), "OHLCV(안전망)"
    return 0.0, 0.0, "실패"

# ============================================================
# 4. 해외 가격 — Finnhub / yfinance
# ============================================================
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

@st.cache_data(ttl=30, show_spinner=False)
def get_us_price(ticker: str) -> tuple:
    """
    해외 주식 가격 조회 우선순위:
    [장중] Finnhub c → yfinance fast_info → yfinance 1분봉
    [장마감] Finnhub pc → yfinance 일봉 종가(iloc[-1], auto_adjust=False)
    Finnhub 키 없어도 yfinance 실시간/종가를 올바르게 가져옴.
    """
    market_open = is_us_market_open()

    # ── [1] Finnhub (키 있을 때) ──
    if FINNHUB_API_KEY:
        q = _fh_fetch_raw(ticker)
        c, pc = q["c"], q["pc"]
        if market_open and c > 0:
            return c, "Finnhub(정규장실시간)"
        if not market_open and pc > 0:
            return pc, "Finnhub(전일정규장종가)"
        if c > 0:
            return c, "Finnhub(시간외·참고)"

    # ── [2] yfinance — 장중 실시간 ──
    try:
        t = yf.Ticker(ticker)
        if market_open:
            # fast_info.last_price = 정규장 실시간 최신가
            p = getattr(t.fast_info, "last_price", None)
            if p is not None and float(p) > 0:
                return float(p), "yfinance(실시간)"
            # 1분봉 마지막 close
            df1m = t.history(period="1d", interval="1m", auto_adjust=True)
            if not df1m.empty:
                return float(df1m["Close"].iloc[-1]), "yfinance(1분봉)"

        # ── [3] yfinance — 장마감 정규장 종가 ──
        # auto_adjust=False 로 수정주가 왜곡 방지
        # period="5d" 대신 "2d" + iloc[-1] 로 가장 최근 확정 종가만 가져옴
        df_d = t.history(period="2d", interval="1d", auto_adjust=False)
        if not df_d.empty:
            # 정규장 종가 = Close (수정 전)
            return float(df_d["Close"].iloc[-1]), "yfinance(정규장종가)"
    except Exception:
        pass

    return 0.0, "실패"

@st.cache_data(ttl=30, show_spinner=False)
def get_us_price_batch(tickers: tuple) -> dict:
    with ThreadPoolExecutor(max_workers=min(len(tickers), 10)) as ex:
        futs = {t: ex.submit(get_us_price, t) for t in tickers}
        return {t: fut.result() for t, fut in futs.items()}

# ============================================================
# 5. OHLCV 로더
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
# 6. 마켓 현황
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
# 7. ★ 퀀트 폭등 예측 엔진 (v3 — 매수구간 버그 수정)
# ============================================================

def _safe_float(val, default=0.0) -> float:
    """NaN/None/Inf 방어 float 변환"""
    try:
        v = float(val)
        return v if np.isfinite(v) else default
    except:
        return default

def quant_predict(df: pd.DataFrame, market: str = "KR", realtime_price: float = 0.0) -> dict:
    """
    realtime_price > 0 이면 OHLCV 마지막 행의 close를 실시간 가격으로 교체 후 계산.
    buy_min/buy_max가 0이 되는 버그 수정:
      - current가 MA 계산 전에 0이면 OHLCV close로 복구
      - cap 계산 시 current>0 여부 재확인, 0이면 cap 적용 안 함
    """
    OUT = {
        "score": 0, "grade": "F", "signals": [],
        "pass": False, "buy_min": 0.0, "buy_max": 0.0,
        "rsi": 50.0, "current": 0.0,
        "s1": False, "s2": False, "s3": False, "s4": False, "s5": False,
    }
    th = THRESHOLDS.get(market, THRESHOLDS["KR"])
    try:
        if df is None or len(df) < 60:
            OUT["signals"].append("❌ 데이터 부족 (60일 미만)")
            return OUT

        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        cl = df["close"].astype(float)
        hi = df["high"].astype(float)
        lo = df["low"].astype(float)
        vo = df["volume"].astype(float)

        # ── 실시간 가격 주입 ──
        # scan_kr/us에서 get_kr_price_with_fallback 결과를 넣어줌
        if realtime_price > 0:
            cl = cl.copy()
            cl.iloc[-1] = realtime_price

        # ── current 결정 ──
        # cl.iloc[-1]이 0 또는 NaN이면 직전 유효값으로 복구
        current = _safe_float(cl.iloc[-1])
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
            OUT["signals"].append(f"❌ 60일 고점권 ({th['max_hi60']*100:.0f}% 이상) — 고점 매수 위험")
            rejected = True

        # ── MA 계산 ──
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
            OUT["signals"].append(f"❌ RSI 과열 ({rsi:.1f}) — 단기 고점 위험")
            rejected = True

        score = 0

        # ── [S1] BB 변동성 수축 ──
        bb_std   = cl.rolling(20).std()
        bb_mean  = cl.rolling(20).mean()
        bb_width = (bb_std * 2) / bb_mean.replace(0, np.nan)
        bw_now   = _safe_float(bb_width.iloc[-1])
        bw_avg   = _safe_float(bb_width.rolling(20).mean().iloc[-1])
        s1 = bw_avg > 0 and bw_now > 0 and bw_now < bw_avg * 0.75
        OUT["s1"] = s1
        if s1:
            score += 30
            OUT["signals"].append(f"✅ [S1] BB 변동성 수축 — 폭발 직전 에너지 응축 (밴드폭 {bw_now:.3f} < 기준 {bw_avg*0.75:.3f})")
        elif bw_avg > 0 and bw_now > 0 and bw_now < bw_avg * 0.90:
            score += 10
            OUT["signals"].append(f"🔶 [S1] BB 밴드 소폭 수축 중 ({bw_now:.3f} / avg {bw_avg:.3f})")
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
            OUT["signals"].append(f"✅ [S2] 거래량 눌림 후 폭발 — 첫 매수세 유입 ({vol_now/vol_ma20*100:.0f}% of MA20)")
        elif vol_burst:
            score += 12
            OUT["signals"].append(f"🔶 [S2] 거래량 급증 (눌림 미확인, {vol_now/vol_ma20*100:.0f}%)")
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
                OUT["signals"].append("✅ [S4] RSI 강세 다이버전스 — 매도세 소진, 반등 임박")
            else:
                OUT["signals"].append("⬜ [S4] RSI 다이버전스 없음")
        except:
            OUT["signals"].append("⬜ [S4] RSI 다이버전스 계산 실패")

        # ── [S5] 캔들 반등 패턴 ──
        s5 = False
        try:
            o1 = _safe_float(df["open"].iloc[-1])
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

        # ──────────────────────────────────────────────────────
        # ★ 매수구간 계산 (v3 버그 수정)
        #
        # 핵심 원칙:
        # 1) raw_low/raw_high는 MA 기반 — 이 값 자체가 의미 있는 기준
        # 2) current가 유효(>0)할 때만 캡 적용
        # 3) 캡 적용 후 buy_low > buy_high가 되면 current 기반으로 재설정
        # 4) 최종 결과가 0이면 current 기반으로 강제 재계산
        # ──────────────────────────────────────────────────────
        ref_low  = min(x for x in [ma5, ma10, ma20] if x > 0) if any(x > 0 for x in [ma5, ma10, ma20]) else 0
        ref_high = max(x for x in [ma5, ma10, ma20] if x > 0) if any(x > 0 for x in [ma5, ma10, ma20]) else 0

        raw_low  = ref_low  * 0.985 if ref_low  > 0 else 0
        raw_high = ref_high * 1.010 if ref_high > 0 else 0

        if current > 0 and raw_low > 0 and raw_high > 0:
            cap_high = current * 1.05
            cap_low  = current * 0.90
            buy_low  = max(min(raw_low,  cap_high), cap_low)
            buy_high = max(min(raw_high, cap_high), buy_low)
            # 역전 방어
            if buy_high <= buy_low:
                buy_low  = current * 0.97
                buy_high = current * 1.02
        elif current > 0:
            # MA 계산 실패 시 현재가 기반
            buy_low  = current * 0.97
            buy_high = current * 1.02
        elif raw_low > 0 and raw_high > 0:
            # current 없을 때 raw 그대로
            buy_low, buy_high = raw_low, raw_high
        else:
            buy_low = buy_high = 0.0

        # 최종 안전망: 여전히 0이면 current 기반 강제 설정
        if (buy_low <= 0 or buy_high <= 0) and current > 0:
            buy_low  = current * 0.97
            buy_high = current * 1.02

        OUT["buy_min"] = round(buy_low,  4)
        OUT["buy_max"] = round(buy_high, 4)
        OUT["score"]   = int(score)

        # ── 등급 & 통과 ──
        combo = s1 or s2 or s3
        OUT["pass"]  = (not rejected) and combo and score >= th["min_pass_score"]
        OUT["grade"] = ("A+" if score >= 90 else "A" if score >= 75
                        else "B+" if score >= 60 else "B" if score >= 40
                        else "C")

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


@st.cache_data(ttl=900, show_spinner=False)
def scan_kr() -> tuple:
    """
    v3: get_kr_price_with_fallback으로 실시간 가격 먼저 가져온 뒤
    realtime_price 파라미터로 quant_predict에 주입 → buy_min/max 정확히 계산
    """
    listing = load_krx_listing()
    targets = listing[listing["Marcap"] > 3e11].nlargest(KR_SCAN_TOP_N, "Marcap")
    codes   = list(zip(targets["Code"], targets["Name"]))

    def _fetch(item):
        code, name = item
        df = load_ohlcv_kr(code)
        if df is None:
            return {"_skip": True, "ticker": f"{name}({code})", "why": "데이터 부족"}

        # 실시간 가격 먼저 조회
        rt_price, rt_vol, rt_src = get_kr_price_with_fallback(code)

        # quant_predict에 실시간 가격 주입
        r = quant_predict(df, "KR", realtime_price=rt_price if rt_price > 0 else 0.0)

        if not r["pass"]:
            why = next((s for s in r["signals"] if "❌" in s), "조건 미충족(점수/콤보 부족)")
            return {"_skip": True, "ticker": f"{name}({code})", "why": why}

        # 화면 표시용 가격: 실시간 > quant current
        display_price = rt_price if rt_price > 0 else r["current"]

        return {
            "_skip":    False,
            "종목":     name,
            "코드":     code,
            "등급":     r["grade"],
            "점수":     r["score"],
            "현재가":   int(display_price) if display_price > 0 else 0,
            "RSI":      round(r["rsi"], 1),
            "매수구간": f"₩{int(r['buy_min']):,} ~ ₩{int(r['buy_max']):,}",
            "목표가":   int(display_price * 1.08) if display_price > 0 else 0,
            "손절가":   int(display_price * 0.93) if display_price > 0 else 0,
            "signals":  r["signals"],
            "source":   rt_src,
            "s_flags":  [r["s1"], r["s2"], r["s3"], r["s4"], r["s5"]],
        }

    with ThreadPoolExecutor(max_workers=30) as ex:
        raw = list(ex.map(_fetch, codes))
    skips = [r for r in raw if r.get("_skip")]
    top3  = sorted([r for r in raw if not r.get("_skip")],
                   key=lambda x: x["점수"], reverse=True)[:3]
    return top3, skips


@st.cache_data(ttl=900, show_spinner=False)
def scan_us() -> tuple:
    rt_map = get_us_price_batch(tuple(US_WATCHLIST))

    def _fetch(ticker):
        df = load_ohlcv_us(ticker)
        if df is None:
            return {"_skip": True, "ticker": ticker, "why": "OHLCV 없음"}

        rt_price, rt_src = rt_map.get(ticker, (0.0, "없음"))
        if rt_price <= 0:
            rt_price, rt_src = get_us_price(ticker)

        r = quant_predict(df, "US", realtime_price=rt_price if rt_price > 0 else 0.0)

        if not r["pass"]:
            why = next((s for s in r["signals"] if "❌" in s), "조건 미충족(점수/콤보 부족)")
            return {"_skip": True, "ticker": ticker, "why": why}

        display_price = rt_price if rt_price > 0 else r["current"]

        return {
            "_skip":    False,
            "종목":     ticker,
            "등급":     r["grade"],
            "점수":     r["score"],
            "현재가":   round(display_price, 2),
            "RSI":      round(r["rsi"], 1),
            "매수구간": f"${r['buy_min']:,.2f} ~ ${r['buy_max']:,.2f}",
            "목표가":   round(display_price * 1.08, 2),
            "손절가":   round(display_price * 0.93, 2),
            "signals":  r["signals"],
            "source":   rt_src,
            "s_flags":  [r["s1"], r["s2"], r["s3"], r["s4"], r["s5"]],
        }

    with ThreadPoolExecutor(max_workers=20) as ex:
        raw = list(ex.map(_fetch, US_WATCHLIST))
    skips = [r for r in raw if r.get("_skip")]
    top3  = sorted([r for r in raw if not r.get("_skip")],
                   key=lambda x: x["점수"], reverse=True)[:3]
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
                why = next((s for s in r["signals"] if "❌" in s), "조건 미충족(점수/콤보 부족)")
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
    top3  = sorted([r for r in raw if not r.get("_skip")],
                   key=lambda x: x["점수"], reverse=True)[:3]
    return top3, skips


# ============================================================
# 9. 포트폴리오 조회
# ============================================================
def get_portfolio_data(name: str) -> dict:
    name = name.strip().upper()

    # 국내 6자리
    if name.isdigit() and len(name) == 6:
        price, vol, src = get_kr_price_with_fallback(name)
        df = load_ohlcv_kr(name)
        if df is not None:
            r = quant_predict(df, "KR", realtime_price=price if price > 0 else 0.0)
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

    # 해외
    price, src = get_us_price(name)
    df = load_ohlcv_us(name)
    if df is not None:
        if price <= 0:
            price = float(df["close"].dropna().iloc[-1]); src = "OHLCV종가"
        r = quant_predict(df, "US", realtime_price=price if price > 0 else 0.0)
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
# 10. UI
# ============================================================
fg_val, fg_txt, exchange = get_market_status()
kr_open = is_kr_market_open()
us_open = is_us_market_open()

st.sidebar.title("🛡️ Tae Scanner v3")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")

col_kr, col_us = st.sidebar.columns(2)
col_kr.metric("🇰🇷 한국장", "🟢 OPEN" if kr_open else "🔴 CLOSED")
col_us.metric("🇺🇸 미국장", "🟢 OPEN" if us_open else "🔴 CLOSED")

if kr_open:
    st.sidebar.info("📡 국내 장중 — 실시간 가격 사용 중\n(KIS→yfinance 1분봉→KRX 순서)")
else:
    st.sidebar.info("📋 국내 장마감 — KRX 확정종가 사용 중")

with st.sidebar.expander("🔑 API 상태", expanded=True):
    st.write("KRX:", "✅ 연결됨" if KRX_API_KEY else "❌ 키 없음")
    st.write("Finnhub:", "✅ 연결됨" if FINNHUB_API_KEY else "❌ 키 없음")
    st.write("KIS 실시간:", "✅ 연결됨" if KIS_APP_KEY else "⬜ 미설정 (yfinance 대체)")

st.title("🚀 Tae's Quant 폭등 예측 스캐너 v3")
st.caption("📌 BB수축 + 거래량폭발 + 정배열 눌림목 + RSI 다이버전스 + 캔들 패턴 | 핵심신호 1개+점수40 통과 | v3: 매수구간 버그 수정 + 장중 실시간 개선")

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
        buy_range_str = (fmt(bmin) + " ~ " + fmt(bmax)) if bmin > 0 and bmax > 0 else "계산 중"

        st.markdown(f"""
<div style="background:#1e293b;padding:20px;border-radius:12px;
            border-left:6px solid {grade_color};margin-bottom:16px;">
  <h3 style="margin:0 0 12px 0;">📈 {d['label']}
    <span style="font-size:14px;background:{grade_color};color:#000;
                 padding:2px 8px;border-radius:4px;margin-left:8px;">
      {d['grade']}등급 {d['score']}점
    </span>
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
      <div style="color:#10b981;font-weight:bold;font-size:12px;">{buy_range_str}</div></div>
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

        with st.expander(f"📋 {name} 시그널 상세"):
            for sig in d.get("signals", []):
                st.markdown(f"- {sig}")

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
            st.info("⚠️ 조건 충족 종목 없음 (사이드바 '스캔 제외' 에서 이유 확인)")
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
            src_html = (f"<span style='font-size:9px;color:#64748b;'>📡{item.get('source','')}</span>"
                        if item.get("source") else "")

            bmin = item.get("매수구간", "")
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
    <li>🟢 매수구간: <b style="color:#10b981;">{bmin}</b></li>
    <li>📈 목표가: <span style="color:#3b82f6;">{fmt(item['목표가'])}</span></li>
    <li>📉 손절선: <span style="color:#ef4444;">{fmt(item['손절가'])}</span></li>
  </ul>
  <details>
    <summary style="cursor:pointer;font-size:11px;color:#94a3b8;">📋 시그널 상세 보기</summary>
    <ul style="margin-top:4px;padding-left:14px;">{sigs_html}</ul>
  </details>
</div>""", unsafe_allow_html=True)

render_cards(ph_us,   "🇺🇸 해외 폭등 예측 TOP 3",  us_top3,     "USD")
render_cards(ph_coin, "🪙 코인 폭등 예측 TOP 3",    crypto_top3, "KRW")
render_cards(ph_kr,   "🔥 국내 폭등 예측 TOP 3",    kr_top3,     "KRW")
