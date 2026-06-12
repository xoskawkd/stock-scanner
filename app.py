from datetime import datetime, timedelta
import pandas as pd
import requests
import streamlit as st
from ta.momentum import RSIIndicator
import yfinance as yf
import pyupbit

# ==========================================
# 1. 페이지 기본 설정 및 스타일 정의
# ==========================================
st.set_page_config(
    page_title="Tae Swing/Short-term TOP 3 Scanner", page_icon="⚡", layout="wide"
)

st.markdown(
    """
    <style>
    [data-testid="stMetricValue"] { font-size: 24px !important; }
    .stMarkdown h3 { margin-top: 10px !important; margin-bottom: 5px !important; }
    .news-box { background-color: #1e222b; padding: 10px; border-radius: 5px; margin-top: 10px; }
    .news-title { font-size: 13px !important; font-weight: bold; color: #00ffcc; text-decoration: none; }
    </style>
""",
    unsafe_allow_html=True,
)

# ==========================================
# 2. 데이터 분석 및 공통 스코어링 함수
# ==========================================

@st.cache_data(ttl=600)
def get_market_status():
    """글로벌 시장 상황 지표 수集"""
    fear_greed, fear_text, usdkrw = "50", "중립", "1,350.00"
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=3).json()
        fear_greed = fg["data"][0]["value"]
        fg_num = int(fear_greed)
        if fg_num >= 75: fear_text = "극단적 탐욕"
        elif fg_num >= 60: fear_text = "탐욕"
        elif fg_num >= 40: fear_text = "중립"
        elif fg_num >= 25: fear_text = "공포"
        else: fear_text = "극단적 공포"
    except:
        pass

    try:
        usd = yf.Ticker("KRW=X")
        usdkrw = f"{usd.history(period='1d')['Close'].iloc[-1]:,.2f}"
    except:
        pass

    return fear_greed, fear_text, usdkrw


def calculate_swing_score(df, is_crypto=False):
    """거래량 폭증 및 이평선 돌파 기반 스윙 핵심 점수 연산"""
    if len(df) < 25:
        return 0, 0, 0, 0, 0

    current = df["Close"].iloc[-1]
    rsi = RSIIndicator(df["Close"]).rsi().iloc[-1]
    ma10 = df["Close"].rolling(10).mean().iloc[-1]
    ma20 = df["Close"].rolling(20).mean().iloc[-1]
    volume_now = df["Volume"].iloc[-1]
    volume_avg = df["Volume"].rolling(20).mean().iloc[-1]

    score = 0

    # 1. RSI 조건 (과매도 구간 및 상승 추세 초입 가중치)
    if 35 <= rsi <= 45: score += 30
    elif 55 <= rsi <= 65: score += 25
    elif rsi < 35: score += 20

    # 2. 이평선 조건 (10일선, 20일선 위)
    if current > ma10: score += 20
    if current > ma20: score += 20

    # 3. 거래량 폭증 (돈이 들어온 흔적 - 가장 중요)
    vol_multiplier = 1.8 if is_crypto else 1.5
    if volume_now > volume_avg * vol_multiplier:
        score += 35

    # 4. 최근 5일 모멘텀 상승세
    change5 = ((current - df["Close"].iloc[-6]) / df["Close"].iloc[-6]) * 100
    if change5 > 4: score += 15

    return score, current, rsi, change5


def fetch_ticker_news(ticker_symbol):
    """야후 파이낸스 API에서 해당 종목의 최신 동향 뉴스 2개 추출"""
    news_list = []
    try:
        t = yf.Ticker(ticker_symbol)
        raw_news = t.news[:2]  # 최신 뉴스 딱 2개만 추출
        for item in raw_news:
            news_list.append({
                "title": item.get("title", "최신 뉴스 목록"),
                "link": item.get("link", "#")
            })
    except:
        pass
    return news_list

# ==========================================
# 3. 시장별 TOP 3 스캔 함수 (뉴스 포함)
# ==========================================

@st.cache_data(ttl=900)
def analyze_us_swing(stocks):
    """🇺🇸 해외 주식 스윙 타점 분석"""
    results = []
    for stock in stocks:
        try:
            df = yf.Ticker(stock).history(period="3mo")
            score, current, rsi, _ = calculate_swing_score(df)
            if current == 0: continue

            results.append({
                "ticker": stock, "종목": stock, "점수": score, "현재가": round(current, 2), "RSI": round(rsi, 1),
                "매수구간": f"${round(current * 0.97, 2)} ~ ${round(current, 2)}",
                "목표가": round(current * 1.09, 2), "손절가": round(current * 0.94, 2)
            })
        except:
            pass
    return sorted(results, key=lambda x: x["점수"], reverse=True)[:3]


@st.cache_data(ttl=600)
def analyze_crypto_swing():
    """🪙 가상화폐(업비트) 알트코인 분석"""
    try:
        coins = pyupbit.get_tickers(fiat="KRW")
    except:
        return []

    results = []
    for coin in coins:
        try:
            df = pyupbit.get_ohlcv(coin, interval="day", count=40)
            if df is None: continue
            df = df.rename(columns={"close": "Close", "volume": "Volume"})
            
            score, current, rsi, _ = calculate_swing_score(df, is_crypto=True)
            if current == 0: continue

            results.append({
                "ticker": None, "코인": coin.replace("KRW-", ""), "점수": score, "현재가": current, "RSI": round(rsi, 1),
                "매수구간": f"{current * 0.96:,.0f} ~ {current:,.0f}",
                "목표가": round(current * 1.13, 0), "손절가": round(current * 0.93, 0)
            })
        except:
            pass
    return sorted(results, key=lambda x: x["점수"], reverse=True)[:3]


@st.cache_data(ttl=900)
def analyze_kr_swing_yf(stocks_dict):
    """🇰🇷 국내 주식 최근 주도/테마 성장주 분석"""
    results = []
    for ticker, name in stocks_dict.items():
        try:
            df = yf.Ticker(ticker).history(period="3mo")
            score, current, rsi, _ = calculate_swing_score(df)
            if current == 0: continue

            results.append({
                "ticker": ticker, "종목": name, "점수": score, "현재가": int(current), "RSI": round(rsi, 1),
                "매수구간": f"{int(current * 0.97):,} ~ {int(current):,}",
                "목표가": int(current * 1.08), "손절가": int(current * 0.94)
            })
        except:
            pass
    return sorted(results, key=lambda x: x["점수"], reverse=True)[:3]


# ==========================================
# 4. [핵심] 최근 동향 테마 및 변동성 중심 종목 풀 재편
# ==========================================

# 지루한 빅테크 제외, 최근 거래량 자금이 활발히 도는 성장/변동성 미해외주식 풀
us_list = [
    "PLTR", "SOUN", "MSTR", "COIN", "ASTS", "LUNR", "UPST", "AFRM", "MARA", "RIOT",
    "BABA", "NIO", "HOOD", "RIVN", "LCID", "SOFI", "U", "AI", "DKNG", "CELH"
]

# 개우량주(삼성/하이닉스) 대거 제외 -> 최근 동향 뉴스 단골 및 변동성 스윙 유망주 풀
kr_dict = {
    "196170.KS": "알테오জেন", "454910.KS": "두산로보틱스", "042700.KS": "한화오션", 
    "273130.KS": "레인보우로보틱스", "003230.KS": "삼양식품", "010060.KS": "OCI홀딩스",
    "247540.KQ": "에코프로비엠", "373220.KS": "LG에너지솔루션", "451220.KS": "에코프로머티",
    "112610.KQ": "씨에스윈드", "214150.KQ": "클래시스", "393890.KQ": "에이프릴바이오",
    "028300.KQ": "HLB", "036570.KQ": "엔씨소프트", "039200.KQ": "오스템임플란트",
    "000880.KS": "한화", "012450.KS": "한화에어로스페이스", "064350.KS": "현대로템"
}

# 스캔 시스템 실행
us_top = analyze_us_swing(us_list)
crypto_top = analyze_crypto_swing()
kr_top = analyze_kr_swing_yf(kr_dict)

# ==========================================
# 5. Streamlit UI 대시보드 렌더링
# ==========================================

fg_val, fg_txt, exchange = get_market_status()
st.sidebar.title("📊 Market Pulse")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.sidebar.caption("💡 실시간 모멘텀 + 관련 최신 뉴스 트래킹 모드")

st.title("⚡ Tae's Dynamic Swing TOP 3 Scanner")
st.markdown("시장의 돈이 쏠리는 **변동성 테마주 풀**에서 거래량 폭증, 이평선 돌파를 감지하고 **실시간 동향 뉴스**를 함께 매핑합니다.")
st.divider()

for market_title, data, symbol in [
    ("🇺🇸 해외 주식 테마/성장주 TOP 3", us_top, "$"),
    ("🪙 가상화폐 알트코인 실시간 TOP 3", crypto_top, ""),
    ("🇰🇷 국내 주식 동향 주도주 TOP 3", kr_top, ""),
]:
    st.header(market_title)
    if data:
        cols = st.columns(3)
        for i in range(min(3, len(data))):
            item = data[i]
            with cols[i]:
                rank_emoji = ["🥇 1등 추천", "🥈 2등 추천", "🥉 3등 추천"][i]
                name_key = "종목" if "종목" in item else "코인"

                st.markdown(
                    f"""
                    ### {rank_emoji} : **{item[name_key]}**
                    * 🔥 **포착 시스템 점수**: `{item['점수']}점`
                    * **현재가**: {symbol}{item['현재가']:,} *(RSI: {item['RSI']})*
                    * 🎯 **권장 진입 타점**: `{item['매수구간']}`
                    * 📈 **목표가 (익절 기준)**: {symbol}{item['목표가']:,}
                    * 📉 **손절가 (리스크 관리)**: {symbol}{item['손절가']:,}
                    """
                )
                
                # 📰 실시간 뉴스 연동 파트 렌더링 (코인이 아닐 때만 야후 뉴스 연동)
                if item.get("ticker"):
                    st.markdown("<div class='news-box'><b>📰 최근 동향 뉴스</b>", unsafe_allow_html=True)
                    news_items = fetch_ticker_news(item["ticker"])
                    if news_items:
                        for n in news_items:
                            st.markdown(f"• <a href='{n['link']}' target='_blank' class='news-title'>{n['title']}</a>", unsafe_allow_html=True)
                    else:
                        st.write("최근 24시간 내 연동된 주요 뉴스가 없습니다.")
                    st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.warning(f"{market_title} 조건에 부합하는 타점 종목이 현재 존재하지 않습니다.")
    st.divider()