import streamlit as st
import pyupbit
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import json
import os
import re
import FinanceDataReader as fdr
from ta.momentum import RSIIndicator
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# ★ KRX 인증키 설정 (여기에 발급받은 인증키를 입력하세요)
# ==========================================
KRX_API_KEY = "08810EEE8F724ED7BB7D35A2B79190956C2FFCB7"   # ← 인증키 삽입 위치

# pandas_datareader 없으면 설치 안내
try:
    import pandas_datareader
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas-datareader", "-q"])

# ==========================================
# 0. 데이터 영구 저장 로직
# ==========================================
DATA_FILE = "portfolio.json"

def load_portfolio():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f: return json.load(f)
        except: return []
    return []

def save_portfolio(data):
    with open(DATA_FILE, "w") as f: json.dump(data, f)

# ==========================================
# 1. 페이지 설정 및 상태 초기화
# ==========================================
st.set_page_config(page_title="Tae Scanner", layout="wide")

if 'my_portfolio' not in st.session_state:
    st.session_state.my_portfolio = load_portfolio()

# ==========================================
# 2. 핵심 분석 엔진 (스윙 점수 + 동적 밴드)
# ==========================================
def calculate_swing_score_and_bands(df):
    if df is None or len(df) < 60:
        return 0, 0, 0, "계산불가", 0, 0
    try:
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        current = float(df["close"].iloc[-1])
        rsi = float(RSIIndicator(df["close"]).rsi().iloc[-1])

        ma10 = float(df["close"].rolling(10).mean().iloc[-1])
        ma20 = float(df["close"].rolling(20).mean().iloc[-1])
        ma60 = float(df["close"].rolling(60).mean().iloc[-1])

        volume_now = float(df["volume"].iloc[-1])
        volume_avg = float(df["volume"].rolling(20).mean().iloc[-1])
        vol_ratio = volume_now / volume_avg if volume_avg > 0 else 1

        score = 0
        if 40 <= rsi <= 60: score += 40
        elif rsi < 40: score += 20
        if current > ma10: score += 20
        if current > ma20: score += 20
        if 0.8 <= vol_ratio <= 1.3: score += 40
        elif vol_ratio > 2.0: score -= 20

        if rsi < 40:
            buy_min, buy_max = ma60, ma20
        else:
            buy_min, buy_max = (ma20, ma10) if ma10 >= ma20 else (ma10, ma20)

        return int(score), current, rsi, buy_min, buy_max, ma20
    except:
        return 40, 0, 50.0, 0, 0, 0


# ==========================================
# ★ 고도화된 스윙 매수 로직 (핵심 신규 추가)
# ==========================================
def advanced_swing_score(df: pd.DataFrame) -> dict:
    """
    1~2일 내 급등 가능성이 높은 스윙 종목 핵심 판별 엔진.

    판별 기준:
    - 변동성 축소(Volatility Squeeze): 최근 20일 고저 범위가 줄어들고 있는가
    - 눌림목: 현재가가 ma20 ± 5% 이내에 머물고 있는가
    - 거래량 골든크로스: 최근 3일간 거래량 감소 후 오늘 5일 거래량 이평 상향 돌파
    - 이미 급등한 종목 제외 (ma20 대비 +12% 초과)
    - 장기 고점권 제외 (60일 최고점 90% 이상)
    - 현재가가 매수 권장가 범위 초과 시 제외

    Returns:
        dict: score(0~100), signals(list), is_valid(bool), buy_min, buy_max
    """
    result = {
        "score": 0,
        "signals": [],
        "is_valid": False,
        "buy_min": 0,
        "buy_max": 0,
        "rsi": 50.0,
        "current": 0,
    }

    try:
        if df is None or len(df) < 20:
            return result

        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]

        current = float(close.iloc[-1])
        result["current"] = current

        # ---------- 이동평균선 (벡터 연산) ----------
        ma5  = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        ma20_last = float(ma20.iloc[-1])
        ma60_last = float(ma60.iloc[-1])
        ma5_last  = float(ma5.iloc[-1])
        ma10_last = float(ma10.iloc[-1])

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # [제외 필터 1] 이미 급등 종목 제거
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if ma20_last > 0 and current > ma20_last * 1.12:
            result["signals"].append("❌ 이미 급등 (ma20 +12% 초과)")
            return result

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # [제외 필터 2] 장기 고점권 제거
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        high60 = float(close.rolling(60).max().iloc[-1])
        if high60 > 0 and current >= high60 * 0.90:
            result["signals"].append("❌ 60일 고점권 (90% 이상)")
            return result

        # ---------- RSI ----------
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi_series = 100 - (100 / (1 + rs))
        rsi_last = float(rsi_series.iloc[-1])
        result["rsi"] = rsi_last

        # ---------- 거래량 ----------
        vol_ma5  = volume.rolling(5).mean()
        vol_ma20 = volume.rolling(20).mean()

        vol_now      = float(volume.iloc[-1])
        vol_ma5_now  = float(vol_ma5.iloc[-1])
        vol_ma5_prev = float(vol_ma5.iloc[-2]) if len(vol_ma5) >= 2 else vol_ma5_now
        vol_ma20_now = float(vol_ma20.iloc[-1])

        # 최근 3일 거래량 감소 여부 (벡터 슬라이싱)
        vol_3days = volume.iloc[-4:-1].values   # 오늘 제외 직전 3일
        vol_3days_decreasing = bool(
            len(vol_3days) == 3 and vol_3days[0] >= vol_3days[1] >= vol_3days[2]
        )

        # 오늘 거래량이 5일 평균을 상향 돌파 (골든크로스)
        vol_golden_cross = (vol_ma5_now > vol_ma5_prev) and (vol_now > vol_ma5_now)

        # ---------- 변동성 축소 (Volatility Squeeze) ----------
        # 최근 20일 고저 범위가 좁아졌는지 확인
        range20_now  = float((high.rolling(20).max() - low.rolling(20).min()).iloc[-1])
        range20_prev = float((high.rolling(20).max() - low.rolling(20).min()).iloc[-6])  # 5일 전 대비
        volatility_squeeze = (range20_prev > 0) and (range20_now < range20_prev * 0.90)

        # ---------- 눌림목 조건 ----------
        near_ma20 = (ma20_last > 0) and (abs(current - ma20_last) / ma20_last <= 0.05)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 매수 권장 구간 산출
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if rsi_last < 40:
            buy_min = float(ma60.iloc[-1])
            buy_max = ma20_last
        elif ma10_last >= ma20_last:
            buy_min = ma20_last
            buy_max = ma10_last
        else:
            buy_min = ma10_last
            buy_max = ma20_last

        result["buy_min"] = buy_min
        result["buy_max"] = buy_max

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # [제외 필터 3] 현재가가 매수 권장가 초과
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if buy_max > 0 and current > buy_max * 1.03:   # 3% 여유 허용
            result["signals"].append("❌ 현재가 매수권장가 초과")
            return result

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 점수 산정 (최대 100점)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        score = 0

        # 1. 변동성 축소 (30점)
        if volatility_squeeze:
            score += 30
            result["signals"].append("✅ 변동성 축소 감지")
        else:
            result["signals"].append("⬜ 변동성 축소 미충족")

        # 2. 눌림목 (25점)
        if near_ma20:
            score += 25
            result["signals"].append("✅ MA20 눌림목")
        else:
            result["signals"].append("⬜ MA20 눌림목 미충족")

        # 3. 거래량 감소 후 골든크로스 (30점)
        if vol_3days_decreasing and vol_golden_cross:
            score += 30
            result["signals"].append("✅ 거래량 골든크로스 (3일 감소 후 상향)")
        elif vol_golden_cross:
            score += 15
            result["signals"].append("🔶 거래량 상향 (3일 감소 미충족)")
        else:
            result["signals"].append("⬜ 거래량 골든크로스 미충족")

        # 4. RSI 중립 구간 (15점)
        if 40 <= rsi_last <= 60:
            score += 15
            result["signals"].append(f"✅ RSI 중립 ({rsi_last:.1f})")
        elif rsi_last < 40:
            score += 7
            result["signals"].append(f"🔶 RSI 과매도 ({rsi_last:.1f})")
        else:
            result["signals"].append(f"⬜ RSI 과열 ({rsi_last:.1f})")

        result["score"]    = int(score)
        result["is_valid"] = score >= 25   # 25점 이상이면 추천 (폴백 포함)

    except Exception as e:
        result["signals"].append(f"오류: {e}")

    return result


# ==========================================
# 3. KRX 실시간 시세 (인증키 방식)
# ==========================================
def get_krx_realtime_price(code: str) -> float:
    """
    KRX 정규 API로 실시간 현재가를 조회합니다.
    인증키(KRX_API_KEY)를 상단에 입력해 주세요.
    """
    try:
        url = (
            f"http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
            f"?bld=dbms/MDC/STAT/standard/MDCSTAT01901"
            f"&locale=ko_KR&isuCd={code}&isuCd2={code}"
            f"&strtDd=&endDd=&adjStkPrc=2&share=1&money=1&csvxls_isNo=false"
        )
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Authorization": f"Bearer {KRX_API_KEY}",
        }
        res = requests.get(url, headers=headers, timeout=5)
        data = res.json()
        # KRX 응답 구조에 따라 파싱 (실제 응답 구조에 맞게 조정 필요)
        price = float(data["output"][0]["TDD_CLSPRC"].replace(",", ""))
        return price
    except Exception:
        return 0.0


def get_kr_realtime_price_naver(code: str) -> float:
    """
    네이버 폴링 API 폴백 (KRX 실패 시 사용)
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": f"https://finance.naver.com/item/main.naver?code={code}"
        }
        api_url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
        res = requests.get(api_url, headers=headers, timeout=5).json()
        return float(res['result']['areas'][0]['datas'][0]['nv'])
    except Exception:
        return 0.0


def calculate_kr_realtime_score(code):
    """
    국내 종목 점수 산출. KRX API 우선, 실패 시 네이버 폴백.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://finance.naver.com/item/main.naver?code={code}"
    }
    try:
        # 실시간 현재가: KRX 우선 → 네이버 폴백
        current = get_krx_realtime_price(code)
        volume_now = 0

        if current <= 0:
            # 네이버 폴백
            api_url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
            api_res = requests.get(api_url, headers=headers, timeout=5).json()
            item_data = api_res['result']['areas'][0]['datas'][0]
            current = float(item_data['nv'])
            volume_now = float(item_data['aq'])

        suffix = ".KS" if code.startswith(('00','01','02','03','04','05','06')) else ".KQ"
        df = yf.Ticker(f"{code}{suffix}").history(period="3mo")
        if df.empty:
            df = yf.Ticker(f"{code}.KQ").history(period="3mo")

        if df.empty:
            return 40, current, 50.0, f"{int(current*0.96):,} ~ {int(current):,}", int(current*1.07), int(current*0.94)

        if current > 0:
            df.iloc[-1, df.columns.get_loc('Close')] = current
        if volume_now > 0:
            df.iloc[-1, df.columns.get_loc('Volume')] = volume_now

        score, _, rsi, buy_min, buy_max, ma20 = calculate_swing_score_and_bands(df)

        buy_range   = f"{int(buy_min):,} ~ {int(buy_max):,}"
        target_price = int(current * 1.07)
        stop_price   = int(min(buy_min * 0.98, current * 0.94))

        return score, current, rsi, buy_range, target_price, stop_price
    except:
        return 40, 0, 50.0, "데이터 동기화 실패", 0, 0


# ==========================================
# 4. 실시간 마켓 현황
# ==========================================
@st.cache_data(ttl=30)
def get_market_status():
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=3).json()
        fg_val = fg["data"][0]["value"]
        fg_txt = ("극단적 탐욕" if int(fg_val) >= 75 else "탐욕" if int(fg_val) >= 60
                  else "중립" if int(fg_val) >= 40 else "공포" if int(fg_val) >= 25 else "극단적 공포")
        usd = yf.Ticker("KRW=X").history(period="1d")['Close'].iloc[-1]
        return fg_val, fg_txt, f"{usd:,.2f}"
    except:
        return "50", "중립", "1,350.00"


# ==========================================
# 5. KRX 종목 리스트 및 가격 캐시
# ==========================================
@st.cache_data(ttl=600, show_spinner=False)
def load_krx():
    return fdr.StockListing('KRX')

@st.cache_data(ttl=600, show_spinner=False)
def load_price(code):
    try:
        df = fdr.DataReader(code, start="2024-06-01")
        if df is not None and len(df) >= 20:
            return df
    except:
        pass
    return None


# ==========================================
# ★ 고도화된 국내 스캐너 (핵심 교체)
# ==========================================
def get_realtime_kr_hot_stocks():
    """
    변동성 축소 + 눌림목 + 거래량 골든크로스 기준으로 상위 3종목 추출.
    시가총액 1000억 이상 상위 80종목 대상 / 개잡주 완전 제외.
    조건 충족 종목이 없으면 필터 완화 후 점수 순 폴백.
    """
    df_list = load_krx()
    targets = df_list[df_list['Marcap'] > 1e11].nlargest(80, 'Marcap')

    valid_results   = []   # is_valid 통과
    fallback_results = []  # 제외 필터(급등/고점) 안 걸린 것 전부

    for code, name in zip(targets['Code'], targets['Name']):
        dfp = load_price(code)
        if dfp is None or len(dfp) < 20:
            continue
        try:
            adv = advanced_swing_score(dfp)
            # 하드 제외 필터(급등/고점/매수가초과)에 걸린 건 폴백에도 안 넣음
            if any("❌" in s for s in adv["signals"]):
                continue
            entry = {
                "Code": code, "Name": name,
                "Score": adv["score"], "Signals": adv["signals"],
                "BuyMin": adv["buy_min"], "BuyMax": adv["buy_max"],
                "RSI": adv["rsi"], "Current": adv["current"],
            }
            if adv["is_valid"]:
                valid_results.append(entry)
            else:
                fallback_results.append(entry)
        except:
            continue

    # 우선: 조건 충족 종목, 없으면 폴백 점수순
    pool = valid_results if valid_results else fallback_results
    if not pool:
        return {}
    pool = sorted(pool, key=lambda x: x["Score"], reverse=True)[:3]
    return {r["Code"]: r["Name"] for r in pool}


# ==========================================
# 6. 해외·코인 스캐너
# ==========================================
def get_safe_us_movers():
    return ["PLTR", "MSTR", "HOOD", "ASTS", "MARA", "RIOT", "UPST", "AFRM", "SOFI", "RIVN"]

def load_us_price(ticker: str):
    """
    해외주식 OHLCV 수집 (한국 환경 차단 우회)
    우선순위: stooq → FinanceDataReader → yfinance
    stooq 는 폴란드 서버라 한국 IP 차단 없음.
    반환: (df, source_name)
    """
    import datetime
    start = datetime.datetime(2024, 10, 1)

    # 1순위: stooq
    try:
        import pandas_datareader.data as web
        df = web.DataReader(f"{ticker}.US", "stooq", start=start)
        df = df.sort_index()
        if df is not None and len(df) >= 60:
            return df, "stooq"
    except Exception:
        pass

    # 2순위: FinanceDataReader
    try:
        df = fdr.DataReader(ticker, start="2024-10-01")
        if df is not None and len(df) >= 60:
            return df, "FDR"
    except Exception:
        pass

    # 3순위: yfinance (VPN 또는 해외 환경)
    try:
        df = yf.Ticker(ticker).history(period="9mo")
        if not df.empty and len(df) >= 60:
            return df, "yfinance"
    except Exception:
        pass

    return None, "실패"


def fetch_us(stock):
    """
    해외주식 스윙 분석.
    데이터 소스 실패/필터 탈락 이유를 _debug 키에 기록해 사이드바 디버그에 표시.
    """
    try:
        df, source = load_us_price(stock)

        if df is None:
            return {"_debug": True, "ticker": stock, "reason": "데이터 수집 실패 (모든 소스 차단)"}

        adv = advanced_swing_score(df)

        # 하드 제외(급등/고점/매수가초과)만 걸러냄
        if any("❌" in s for s in adv["signals"]):
            reason = next(s for s in adv["signals"] if "❌" in s)
            return {"_debug": True, "ticker": stock, "reason": f"[{source}] {reason}"}

        current = adv["current"]
        if current == 0:
            return {"_debug": True, "ticker": stock, "reason": f"[{source}] 현재가 0"}

        buy_min = adv["buy_min"]
        buy_max = adv["buy_max"]

        return {
            "_debug":   False,
            "ticker":   stock,
            "종목":     stock,
            "점수":     adv["score"],
            "현재가":   round(current, 2),
            "RSI":      round(adv["rsi"], 1),
            "매수구간":  f"${buy_min:,.2f} ~ ${buy_max:,.2f}",
            "목표가":   round(current * 1.07, 2),
            "손절가":   round(min(buy_min * 0.98, current * 0.94), 2),
            "signals":  adv["signals"],
            "source":   source,
        }
    except Exception as e:
        return {"_debug": True, "ticker": stock, "reason": str(e)}

def fetch_crypto(coin):
    try:
        df = pyupbit.get_ohlcv(coin, interval="day", count=100)
        if df is None or df.empty: return None

        adv = advanced_swing_score(df)
        # 하드 제외(급등/고점/매수가초과)만 필터
        if any("❌" in s for s in adv["signals"]): return None

        current = adv["current"]
        rsi     = adv["rsi"]
        buy_min = adv["buy_min"]
        buy_max = adv["buy_max"]
        score   = adv["score"]

        if current == 0: return None
        return {
            "ticker":  None,
            "코인":    coin.replace("KRW-", ""),
            "점수":    score,
            "현재가":  current,
            "RSI":     round(rsi, 1),
            "매수구간": f"{int(buy_min):,} ~ {int(buy_max):,}",
            "목표가":  round(current * 1.08, 0),
            "손절가":  round(min(buy_min * 0.98, current * 0.94), 0),
            "signals": adv["signals"],
        }
    except:
        return None

def fetch_kr(item):
    code, name = item
    try:
        dfp = load_price(code)
        if dfp is None or len(dfp) < 20:
            return None

        adv = advanced_swing_score(dfp)

        # 하드 제외 필터(급등/고점/매수가초과)에 걸리면 무조건 제외
        if any("❌" in s for s in adv["signals"]):
            return None

        score, real_price, rsi, buy_range, target_price, stop_price = calculate_kr_realtime_score(code)
        if real_price == 0:
            return None

        # 실시간 현재가도 매수권장가 초과 시 제외
        buy_max = adv["buy_max"]
        if buy_max > 0 and real_price > buy_max * 1.03:
            return None

        return {
            "ticker":   code,
            "종목":     name,
            "점수":     adv["score"],
            "현재가":   int(real_price),
            "RSI":      round(adv["rsi"], 1),
            "매수구간":  buy_range,
            "목표가":   target_price,
            "손절가":   stop_price,
            "signals":  adv["signals"],
        }
    except:
        return None


# ==========================================
# 7. 포트폴리오 자산 실시간 매싱
# ==========================================
def get_portfolio_market_data(name):
    name = name.strip().upper()

    # 국내주식
    if name.isdigit() and len(name) == 6:
        try:
            score, real_price, rsi, buy_range, target_price, stop_price = calculate_kr_realtime_score(name)
            krx = load_krx()
            target_row = krx[krx["Code"] == name]
            display_name = target_row["Name"].values[0] if not target_row.empty else "국내주식"
            if real_price > 0:
                return (f"{name} ({display_name})", real_price, score, rsi, "KRW", "Stock", stop_price, target_price)
        except Exception as e:
            st.error(f"{name} 국내주식 오류: {e}")

    # 해외주식 — stooq → FDR → yfinance 순 시도
    try:
        df, src = load_us_price(name)
        if df is not None and not df.empty:
            df_norm = df.copy()
            df_norm.columns = [c.lower() for c in df_norm.columns]
            curr = float(df_norm["close"].dropna().iloc[-1])
            s, _, r, b_min, b_max, ma20 = calculate_swing_score_and_bands(df)
            if curr > 0:
                return (f"{name} (해외주식·{src})", curr, s, r, "USD", "Stock", min(b_min * 0.98, curr * 0.94), curr * 1.07)
    except:
        pass

    # 코인
    try:
        df = pyupbit.get_ohlcv(f"KRW-{name}", interval="day", count=100)
        if df is not None and not df.empty:
            s, c, r, b_min, b_max, ma20 = calculate_swing_score_and_bands(df)
            if c > 0:
                return (f"{name} (업비트 코인)", c, s, r, "KRW", "Crypto", min(b_min * 0.98, c * 0.92), c * 1.10)
    except:
        pass

    return (None, 0, 0, 0, "USD", "Stock", 0, 0)


# ==========================================
# 8. UI 메인 대시보드
# ==========================================
fg_val, fg_txt, exchange = get_market_status()
st.sidebar.title("🛡️ Safety Theme Pulse")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.title("🚀 Tae's Balanced Smart TOP 3 Scanner")

kr_live_dict  = get_realtime_kr_hot_stocks()
us_live_list  = get_safe_us_movers()
try:
    coins_list = pyupbit.get_tickers(fiat="KRW")[:30]
except:
    coins_list = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]

with ThreadPoolExecutor(max_workers=20) as executor:
    us_raw     = list(executor.map(fetch_us, us_live_list))
    crypto_top = sorted([r for r in executor.map(fetch_crypto, coins_list) if r],   key=lambda x: x["점수"], reverse=True)[:3]
    kr_top     = sorted([r for r in executor.map(fetch_kr, kr_live_dict.items()) if r], key=lambda x: x["점수"], reverse=True)[:3]

us_debug = [r for r in us_raw if r and r.get("_debug")]
us_top   = sorted([r for r in us_raw if r and not r.get("_debug")], key=lambda x: x["점수"], reverse=True)[:3]

# 사이드바 디버그 패널
with st.sidebar.expander("🔍 해외주식 스캔 상세 로그", expanded=False):
    if us_debug:
        for d in us_debug:
            st.markdown(f"- **{d['ticker']}**: {d['reason']}")
    else:
        st.write("모든 종목 정상 통과")

for title, data, sym in [
    ("🇺🇸 해외 알짜 성장주 TOP 3", us_top, "$"),
    ("🪙 코인 TOP 3", crypto_top, ""),
    ("🔥 국내 테마/거래대금 대장주 TOP 3", kr_top, "₩"),
]:
    st.header(title)
    if not data:
        st.info(f"⚠️ 현재 {title} 중 매수 조건(급등 제외 + 매수가 이내)을 충족한 종목이 없습니다.\n\n사이드바 로그에서 탈락 이유를 확인하세요.")
        continue
    cols = st.columns(3)
    for i, item in enumerate(data):
        with cols[i]:
            key    = "종목" if "종목" in item else "코인"
            medal  = "🥇" if i == 0 else "🥈" if i == 1 else "🥉"
            signals_html = "".join(
                f"<li style='font-size:12px;'>{s}</li>"
                for s in item.get("signals", [])
            )
            st.markdown(f"""
<div style="background-color:#1e293b; padding:20px; border-radius:10px;
            border-left: 5px solid #10b981; margin-bottom:10px;">
  <h3 style="margin-top:0;">{medal} {item[key]}</h3>
  <ul>
    <li>🔥 스윙 점수: <b>{item['점수']}점</b></li>
    <li>📊 실시간 RSI: <code>{item['RSI']}</code></li>
    <li>💰 현재가: <b>{sym}{item['현재가']:,}</b></li>
    <li>🎯 정석 지지타점: <span style="color:#10b981;"><b>{item['매수구간']}</b></span></li>
    <li>📈 목표가: <span style="color:#3b82f6;">{sym}{item['목표가']:,}</span></li>
    <li>📉 손절선: <span style="color:#ef4444;">{sym}{item['손절가']:,}</span></li>
  </ul>
  <details>
    <summary style="cursor:pointer; font-size:12px; color:#94a3b8;">📋 매수 시그널 상세</summary>
    <ul style="margin-top:6px;">{signals_html}</ul>
  </details>
</div>""", unsafe_allow_html=True)

st.divider()

# ==========================================
# 9. 내 포트폴리오 관리
# ==========================================
st.header("💼 실시간 내 자산 관리 피드")

if st.button("🚨 데이터 강제 초기화 (평단가 오류 해결)"):
    st.session_state.my_portfolio = []
    save_portfolio([])
    st.rerun()

with st.form(key='portfolio_form', clear_on_submit=True):
    c1, c2, c3 = st.columns([2, 1, 1])
    n_in = c1.text_input("종목코드 / 티커", placeholder="국내: 숫자, 해외: 영문")
    b_in = c2.number_input("내 평단가", min_value=0.0, step=0.01, format="%.2f")
    if c3.form_submit_button("➕ 포트폴리오 추가"):
        if n_in and b_in > 0:
            st.session_state.my_portfolio.append({"name": n_in.strip().upper(), "buy": float(b_in)})
            save_portfolio(st.session_state.my_portfolio)
            st.rerun()
        else:
            st.warning("종목명과 0보다 큰 평단가를 입력하세요.")

if st.session_state.my_portfolio:
    to_remove = None
    for i, p in enumerate(st.session_state.my_portfolio):
        name, buy = p['name'], p['buy']
        stock_label, curr, score, rsi, currency, cat, calc_stop, calc_target = get_portfolio_market_data(name)

        if stock_label is None or curr <= 0:
            st.error(f"⚠️ {name} 데이터를 가져오지 못했습니다.")
            if st.button(f"❌ {name} 삭제", key=f"err_del_{i}"):
                to_remove = i
            continue

        profit = ((curr - buy) / buy * 100) if buy > 0 else 0
        sym = "₩" if currency == "KRW" else "$"

        st.markdown(f"### 📈 자산 대응 리포트: **{stock_label}**")
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("내 평단가",     f"{sym}{buy:,.2f}")
        col_m2.metric("실시간 현재가", f"{sym}{curr:,.2f}")
        col_m3.metric("실시간 수익률", f"{'+' if profit >= 0 else ''}{profit:.2f}%")

        st.caption(f"📊 스윙 스코어: **{score}점** | 현재 RSI 상태: **{rsi}**")

        df_guide = pd.DataFrame({
            "포지션 전략":         ["현재가 스탠스", "목표 익절가 (정밀)", "리스크 손절가 (지지선 이탈)"],
            "대응 가격 단가": [f"{sym}{curr:,.2f}", f"{sym}{calc_target:,.2f}", f"{sym}{calc_stop:,.2f}"]
        })
        st.table(df_guide)

        if st.button(f"🗑️ 삭제", key=f"del_final_{i}"):
            to_remove = i
        st.markdown("<br>", unsafe_allow_html=True)

    if to_remove is not None:
        st.session_state.my_portfolio.pop(to_remove)
        save_portfolio(st.session_state.my_portfolio)
        st.rerun()
