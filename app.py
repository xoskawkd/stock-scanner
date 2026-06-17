"""
Tae Scanner — 퀀트 폭등 예측 엔진 (v2 수정판)
==================================
국내 기준가 : KRX 공식 OpenAPI(data-dbg.krx.co.kr) 일별 확정종가 → yfinance → OHLCV 최종안전망
해외 실시간 : Finnhub(장중) / 전일 정규장 종가(장마감 시) → yfinance fallback
핵심 전략  : 폭등 1~2일 전 신호 5개 복합 감지

[v2 수정 내역]
1. 통과 조건 완화: 핵심신호(S1·S2·S3) 2개 동시 충족 → 1개 이상으로 완화, 통과 점수 50→40
   - 워치리스트의 강세 종목(NVDA 등)이 "이미 신고가권"이라는 이유로 전부 걸러지던 문제 해결
2. "이미 급등" 컷오프 완화 (RSI 70→78, 5일수익 8%→15%, MA20이탈 8%→15%, 60일고점 85%→95%)
3. 코인 유동성 필터를 '거래량(개수)' → '거래대금(원화)' 기준으로 교체 (저가코인 거래량 왜곡 버그)
4. scan_crypto가 quant_predict(df, "KR")로 잘못 호출되던 것을 "CRYPTO"로 수정
5. KRX 가격 조회를 공식 OpenAPI 스펙(AUTH_KEY 헤더, stk_bydd_trd 엔드포인트)으로 교체
   - 기존 코드는 Bearer 인증 + 비공식 스크래핑 엔드포인트가 섞여 있어 키가 있어도 항상 실패했음
   - KRX 공식 데이터는 '장마감 후 확정되는 일별 종가'이며 틱 단위 실시간이 아님 (사용자 확인 후 이대로 진행)
6. 해외 Finnhub 가격: 미국 정규장(09:30~16:00 ET) 여부를 판별해
   - 장중 → 실시간가(c) 사용 / 장마감 → 전일 정규장 종가(pc) 사용
   - 기존 코드는 장 마감 후에도 단일 호가성 시간외가(c)를 그대로 끌어와 수익률이 튀는 문제가 있었음
7. 국내/코인 스캔에 "제외 이유 요약" 사이드바 로그 추가 (몇 종목이 어떤 이유로 빠졌는지 확인 가능)
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
KRX_API_KEY     = "08810EEE8F724ED7BB7D35A2B79190956C2FFCB7"   # ← data.krx.co.kr 에서 발급받은 AUTH_KEY 입력
FINNHUB_API_KEY = "d8p0ftpr01qp954tu3ogd8p0ftpr01qp954tu3p0"   # ← Finnhub 키 입력 (없으면 yfinance 사용)

# ============================================================
# ★ 스캔/필터 튜닝값 (여기만 건드리면 민감도 조절 가능)
# ============================================================
KR_SCAN_TOP_N     = 300   # 국내: 시총 상위 N개 중 스캔
CRYPTO_SCAN_LIMIT = 80     # 코인: 업비트 KRW 마켓 상위 N개 스캔 (기존 40 → 80)

THRESHOLDS = {
    # min_vol         : 일평균 거래량(주식 수) 하한 — 코인은 별도(거래대금) 기준 사용
    # max_rsi         : RSI 과열 컷오프 (이 값 초과면 바로 탈락)
    # max_gain5        : 5일 수익률 컷오프
    # max_ma20_dev     : 현재가가 MA20의 몇 배를 넘으면 탈락
    # max_hi60         : 60일 고점 대비 몇 %부터 탈락
    # min_pass_score   : 통과 최소 점수
    "KR":     {"min_vol": 50_000,  "max_rsi": 78, "max_gain5": 0.15, "max_ma20_dev": 1.15, "max_hi60": 0.95, "min_pass_score": 40},
    "US":     {"min_vol": 500_000, "max_rsi": 78, "max_gain5": 0.15, "max_ma20_dev": 1.15, "max_hi60": 0.95, "min_pass_score": 40},
    "CRYPTO": {"min_value": 1_000_000_000, "max_rsi": 80, "max_gain5": 0.25, "max_ma20_dev": 1.20, "max_hi60": 0.97, "min_pass_score": 40},
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
# 2. 국내 기준가 — KRX 공식 OpenAPI (일별 확정종가)
# ============================================================
@st.cache_data(ttl=600, show_spinner=False)
def get_krx_daily_snapshot() -> tuple:
    """
    KRX 공식 OpenAPI(data-dbg.krx.co.kr) — 전종목 일별매매정보를 '한 번에' 조회.
    AUTH_KEY 헤더 방식 (Bearer 아님). 오늘 데이터가 아직 확정 전이면(휴장/장중)
    최대 5영업일 전까지 거슬러 올라가며 가장 최근 확정 데이터를 찾는다.
    반환: (DataFrame, 기준일자 문자열) — 키 없거나 실패 시 (빈 DataFrame, "")
    """
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
        except Exception:
            pass
        d -= timedelta(days=1)
    return pd.DataFrame(), ""


def get_krx_price(code: str) -> tuple:
    """캐시된 전종목 스냅샷에서 종목 1개를 찾아 반환. (price, volume, source)"""
    if not KRX_API_KEY:
        return 0.0, 0.0, "KRX키없음"
    snap, basdd = get_krx_daily_snapshot()
    if snap.empty:
        return 0.0, 0.0, "KRX응답없음"
    # 필드명은 KRX OpenAPI 표준 응답 기준(ISU_SRT_CD=단축코드, TDD_CLSPRC=종가, ACC_TRDVOL=거래량)
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
    except Exception:
        return 0.0, 0.0, "KRX파싱오류"


@st.cache_data(ttl=30, show_spinner=False)
def get_kr_price_with_fallback(code: str) -> tuple:
    """
    KRX(확정종가) → yfinance(준실시간) → OHLCV 마지막 종가(최종 안전망) 순서.
    이전 코드는 KRX가 거의 항상 실패해도 그대로 '실패'로 끝났는데,
    이제 fdr 일봉 종가를 최종 안전망으로 둬서 '조회 실패' 자체가 거의 안 뜨게 함.
    """
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
    except Exception:
        pass

    # 최종 안전망: 이미 로드돼 있는 일봉 OHLCV의 마지막 종가
    df_ohlcv = load_ohlcv_kr(code)
    if df_ohlcv is not None and not df_ohlcv.empty:
        return float(df_ohlcv["close"].iloc[-1]), float(df_ohlcv["volume"].iloc[-1]), "OHLCV종가(안전망)"
    return 0.0, 0.0, "실패"


# ============================================================
# 3. 해외 가격 — Finnhub(장중 실시간) / 전일 정규장 종가(장마감) → yfinance
# ============================================================
def is_us_market_open() -> bool:
    """미국 정규장(09:30~16:00 ET, 평일) 여부. 공휴일 휴장은 별도 체크 안 함."""
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


def _fh_fetch_raw(ticker: str) -> dict:
    """Finnhub /quote. c=현재가, pc=전일 정규장 종가."""
    if not FINNHUB_API_KEY:
        return {"c": 0.0, "pc": 0.0}
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=4,
        ).json()
        return {"c": float(r.get("c", 0) or 0), "pc": float(r.get("pc", 0) or 0)}
    except Exception:
        return {"c": 0.0, "pc": 0.0}


@st.cache_data(ttl=60, show_spinner=False)
def get_us_price(ticker: str) -> tuple:
    """
    반환: (price, source)
    장중에는 Finnhub 실시간가(c), 장마감 후에는 전일 정규장 종가(pc)를 우선 사용해
    시간외/프리마켓 단일호가로 수익률이 왜곡되는 문제를 방지.
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

    try:
        t = yf.Ticker(ticker)
        if market_open:
            p = getattr(t.fast_info, "last_price", 0)
            if p and float(p) > 0:
                return float(p), "yfinance"
        df = t.history(period="5d", interval="1d")
        if not df.empty:
            return float(df["Close"].iloc[-1]), "yfinance-일봉종가"
    except Exception:
        pass
    return 0.0, "실패"


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
# 6. ★ 퀀트 폭등 예측 엔진
#
# 폭등 1~2일 전에 흔히 나타나는 5가지 신호:
#
# [S1] 변동성 수축 (Volatility Squeeze)
# [S2] 거래량 패턴 — 눌림 후 폭발 직전
# [S3] 이동평균 배열 + 눌림목
# [S4] RSI 다이버전스 (가격 하락 vs RSI 상승)
# [S5] 캔들 패턴 — 반등 캔들
#
# 통과 조건(v2): 핵심신호(S1·S2·S3) 중 1개 이상 + 종합점수 40점 이상
# (v1은 2개 동시 충족을 요구해 강세장에서 거의 통과 종목이 없었음)
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

        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        cl = df["close"]
        hi = df["high"]
        lo = df["low"]
        vo = df["volume"]

        current = float(cl.iloc[-1])
        OUT["current"] = current

        # rejected=True가 되는 항목이 있어도 등급/점수/RSI는 끝까지 계산한다.
        # (포트폴리오 조회는 "스캔 통과 여부"와 무관하게 항상 현재 RSI/등급을 보여줘야 하므로
        #  v1처럼 여기서 바로 return해버리면 보유종목이 전부 기본값(F등급, RSI 50)으로 보였음)
        rejected = False

        # ── 잡주/저유동성 필터 ──
        if market == "CRYPTO":
            avg_value = float((vo * cl).rolling(20).mean().iloc[-1])  # 평균 거래대금(원화)
            if avg_value < th["min_value"]:
                OUT["signals"].append(f"❌ 유동성 부족 (일평균 거래대금 {avg_value/1e8:.1f}억원)")
                rejected = True
        else:
            avg_vol = float(vo.rolling(20).mean().iloc[-1])
            if avg_vol < th["min_vol"]:
                OUT["signals"].append(f"❌ 유동성 부족 (일평균 {int(avg_vol):,}주)")
                rejected = True

        # ── 이미 오른 종목 표시(통과 제외용, 등급 계산은 계속 진행) ──
        ma20 = float(cl.rolling(20).mean().iloc[-1])
        if ma20 > 0 and current > ma20 * th["max_ma20_dev"]:
            OUT["signals"].append(f"❌ 이미 급등 (MA20 대비 +{(th['max_ma20_dev']-1)*100:.0f}% 초과)")
            rejected = True

        p5ago = float(cl.iloc[-6]) if len(cl) >= 6 else current
        gain5 = (current - p5ago) / p5ago if p5ago > 0 else 0
        if gain5 > th["max_gain5"]:
            OUT["signals"].append(f"❌ 5일 수익 {gain5*100:.1f}% — 이미 터진 종목")
            rejected = True

        hi60 = float(cl.rolling(60).max().iloc[-1])
        if hi60 > 0 and current >= hi60 * th["max_hi60"]:
            OUT["signals"].append(f"❌ 60일 고점권 ({th['max_hi60']*100:.0f}% 이상) — 고점 매수 위험")
            rejected = True

        # ── MA 계산 ──
        ma5  = float(cl.rolling(5).mean().iloc[-1])
        ma10 = float(cl.rolling(10).mean().iloc[-1])
        ma60 = float(cl.rolling(60).mean().iloc[-1])

        # ── RSI (항상 계산) ──
        delta = cl.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi_s = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        rsi   = float(rsi_s.iloc[-1])
        OUT["rsi"] = rsi
        if rsi > th["max_rsi"]:
            OUT["signals"].append(f"❌ RSI 과열 ({rsi:.1f}) — 단기 고점 위험")
            rejected = True

        score = 0

        # ── [S1] 볼린저밴드 변동성 수축 ──
        bb_std    = cl.rolling(20).std()
        bb_width  = (bb_std * 2) / cl.rolling(20).mean()
        bw_now    = float(bb_width.iloc[-1])
        bw_avg    = float(bb_width.rolling(20).mean().iloc[-1])
        s1 = bw_avg > 0 and bw_now < bw_avg * 0.75
        OUT["s1"] = s1
        if s1:
            score += 30
            OUT["signals"].append(f"✅ [S1] BB 변동성 수축 — 폭발 직전 에너지 응축 (밴드폭 {bw_now:.3f} < 기준 {bw_avg*0.75:.3f})")
        elif bw_avg > 0 and bw_now < bw_avg * 0.90:
            score += 10
            OUT["signals"].append("🔶 [S1] BB 밴드 소폭 수축 중")
        else:
            OUT["signals"].append("⬜ [S1] 변동성 수축 없음")

        # ── [S2] 거래량 눌림 후 폭발 신호 ──
        vol_ma5  = float(vo.rolling(5).mean().iloc[-1])
        vol_ma20 = float(vo.rolling(20).mean().iloc[-1])
        vol_now  = float(vo.iloc[-1])
        vol_dry    = vol_ma20 > 0 and vol_ma5 < vol_ma20 * 0.70
        vol_burst  = vol_ma5 > 0  and vol_now > vol_ma5  * 1.50
        s2 = vol_dry and vol_burst
        OUT["s2"] = s2
        if s2:
            score += 30
            OUT["signals"].append(f"✅ [S2] 거래량 눌림 후 폭발 — 첫 매수세 유입 감지 ({vol_now/vol_ma20*100:.0f}%)")
        elif vol_burst:
            score += 12
            OUT["signals"].append("🔶 [S2] 거래량 급증 (눌림 미확인)")
        elif vol_dry:
            score += 8
            OUT["signals"].append("🔶 [S2] 거래량 눌림 확인 (폭발 대기)")
        else:
            OUT["signals"].append("⬜ [S2] 거래량 신호 없음")

        # ── [S3] 이동평균 정배열 + 눌림목 ──
        aligned   = ma5 > ma20 > ma60
        near_ma20 = ma20 > 0 and abs(current - ma20) / ma20 <= 0.05
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

        # ── [S4] RSI 다이버전스 (숨겨진 강세) ──
        s4 = False
        try:
            price_window = cl.iloc[-10:]
            rsi_window   = rsi_s.iloc[-10:]
            p_low_prev = float(price_window.iloc[:5].min())
            p_low_now  = float(price_window.iloc[5:].min())
            r_low_prev = float(rsi_window.iloc[:5].min())
            r_low_now  = float(rsi_window.iloc[5:].min())
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
            o1, c1_v, h1, l1 = float(df["open"].iloc[-1]), float(cl.iloc[-1]), float(hi.iloc[-1]), float(lo.iloc[-1])
            o2, c2_v         = float(df["open"].iloc[-2]), float(cl.iloc[-2])
            body  = abs(c1_v - o1)
            lower = o1 - l1 if c1_v >= o1 else c1_v - l1
            upper = h1 - c1_v if c1_v >= o1 else h1 - o1
            hammer    = lower > body * 2 and upper < body * 0.5
            bull_rev  = c2_v < o2 and c1_v > o1
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
            OUT["signals"].append(f"🔶 RSI 과매도 탈출 대기 ({rsi:.1f})")
        else:
            OUT["signals"].append(f"⬜ RSI 구간 외 ({rsi:.1f})")

        # ── 매수 구간 ──
        # MA20/MA5가 현재가와 너무 멀어진 경우(급락 직후 MA가 아직 안 따라 내려온 경우 등)
        # MA 기준 매수구간이 현재가보다 훨씬 위에 잡히는 문제가 있어, 현재가 기준으로 보정한다.
        # ── 매수 구간 (수정본) ──
        raw_low  = min(ma20, ma10) * 0.985
        raw_high = max(ma20, ma5)  * 1.010

        cap_high = current * 1.03
        cap_low  = current * 0.97

        buy_low  = min(max(raw_low, cap_low), cap_high)
        buy_high = min(max(raw_high, cap_low), cap_high)

        if buy_low > buy_high:
            buy_low, buy_high = cap_low, cap_high
        

        # ── 등급 & pass 기준 (v2: 핵심신호 1개 이상 + 점수 40 이상) ──
        # 등급/점수는 rejected 여부와 무관하게 항상 산출(포트폴리오 조회용).
        # "통과(pass)"만 이미 급등/RSI과열/저유동성이면 제외.
        combo = s1 or s2 or s3
        OUT["pass"]  = (not rejected) and combo and score >= th["min_pass_score"]
        OUT["grade"] = ("A+" if score >= 90 else "A" if score >= 75
                        else "B+" if score >= 60 else "B" if score >= 40
                        else "C")

    except Exception as e:
        OUT["signals"].append(f"오류: {e}")
    return OUT


# ============================================================
# 7. 스캐너
# ============================================================
US_WATCHLIST = [
    "NVDA","META","GOOGL","AMZN","MSFT","AMD","TSLA",
    "PYPL","SQ","SOFI","HOOD","UPST","AFRM",
    "PLTR","ASTS","HIMS","AXSM","RIVN","SMCI","ARM",
]


def summarize_skips(skips: list) -> dict:
    """제외된 종목들의 탈락 이유를 키워드별로 집계 (사이드바 진단용)"""
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
            why = next((s for s in r["signals"] if "❌" in s), "조건 미충족(점수/콤보 부족)")
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
            why = next((s for s in r["signals"] if "❌" in s), "조건 미충족(점수/콤보 부족)")
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
    top3  = sorted([r for r in raw if not r.get("_skip")], key=lambda x: x["점수"], reverse=True)[:3]
    return top3, skips


# ============================================================
# 8. 포트폴리오 조회
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
                    "source": src, "ok": price > 0, "signals": r["signals"]}

    # 해외
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
                "source": src, "ok": price > 0, "signals": r["signals"]}
    if price > 0:
        return {"label": f"{name} ({src}·지표없음)", "curr": price,
                "score": 0, "grade": "-", "rsi": 50.0, "currency": "USD",
                "stop": round(price*0.93,2), "target": round(price*1.08,2),
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
                        "source": "Upbit", "ok": True, "signals": r["signals"]}
    except:
        pass

    return {"label": None, "curr": 0, "score": 0, "grade": "F",
            "rsi": 0, "currency": "USD", "stop": 0, "target": 0,
            "source": "실패", "ok": False, "signals": []}


# ============================================================
# 9. UI
# ============================================================
fg_val, fg_txt, exchange = get_market_status()

st.sidebar.title("🛡️ Tae Scanner")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.sidebar.metric("🇺🇸 미국 정규장", "OPEN" if is_us_market_open() else "CLOSED")

with st.sidebar.expander("🔑 API 상태", expanded=True):
    st.write("KRX:", "✅ 연결됨 (일별 확정종가, 장마감 후 갱신·틱 단위 실시간 아님)" if KRX_API_KEY else "❌ 키 없음 (yfinance/OHLCV 대체)")
    st.write("Finnhub:", "✅ 연결됨 (장중 실시간 / 장마감 시 전일종가)" if FINNHUB_API_KEY else "❌ 키 없음 (yfinance 대체)")

st.title("🚀 Tae's Quant 폭등 예측 스캐너")
st.caption("📌 BB수축 + 거래량 폭발 + 정배열 눌림목 + RSI 다이버전스 + 캔들 패턴 복합 감지 | 핵심신호 1개+점수40 이상 통과")

ph_us   = st.empty()
ph_coin = st.empty()
ph_kr   = st.empty()
st.divider()

# ── 포트폴리오 ──
st.header("💼 내 자산 실시간 관리")

col_btn1, col_btn2 = st.columns([1, 5])
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
    <div><div style="font-size:11px;color:#94a3b8;">기준 현재가</div>
         <div style="font-size:20px;font-weight:bold;">{fmt(curr)}</div></div>
    <div><div style="font-size:11px;color:#94a3b8;">수익률</div>
         <div style="font-size:20px;font-weight:bold;color:{p_color};">
           {'+' if profit>=0 else ''}{profit:.2f}%</div></div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px;">
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
  <div style="font-size:10px;color:#475569;">출처: {d['source']}</div>
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

with st.sidebar.expander(f"🔍 국내 스캔 제외 요약 ({len(kr_skips)}종목 제외)", expanded=False):
    for reason, cnt in summarize_skips(kr_skips).items():
        st.markdown(f"- **{reason}**: {cnt}개 종목")

with st.sidebar.expander(f"🔍 코인 스캔 제외 요약 ({len(crypto_skips)}종목 제외)", expanded=False):
    for reason, cnt in summarize_skips(crypto_skips).items():
        st.markdown(f"- **{reason}**: {cnt}개 종목")

with st.sidebar.expander("🔍 해외 스캔 제외 로그", expanded=False):
    for s in us_skips:
        st.markdown(f"- **{s['ticker']}**: {s['why']}")

# ── 카드 렌더 ──
S_LABELS = ["S1:BB수축", "S2:거래량폭발", "S3:정배열눌림", "S4:RSI다이버전스", "S5:반등캔들"]

def render_cards(placeholder, title: str, data: list, currency: str):
    with placeholder.container():
        st.header(title)
        if not data:
            st.info("⚠️ 조건 충족 종목 없음 — 시장 과열 또는 눌림목 대기 중 (사이드바 '스캔 제외 요약'에서 이유 확인 가능)")
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
    <summary style="cursor:pointer;font-size:11px;color:#94a3b8;margin-top:4px;">📋 시그널 상세 보기</summary>
    <ul style="margin-top:4px;padding-left:14px;">{sigs_html}</ul>
  </details>
</div>""", unsafe_allow_html=True)

render_cards(ph_us,   "🇺🇸 해외 폭등 예측 TOP 3",  us_top3,     "USD")
render_cards(ph_coin, "🪙 코인 폭등 예측 TOP 3",    crypto_top3, "KRW")
render_cards(ph_kr,   "🔥 국내 폭등 예측 TOP 3",    kr_top3,     "KRW")
