from datetime import datetime, timedelta
import pandas as pd
import requests
import streamlit as st
from ta.momentum import RSIIndicator
import yfinance as yf
import pyupbit
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# 1. 페이지 기본 설정 및 스타일 정의
# ==========================================
st.set_page_config(
    page_title="Tae Mid-Cap Theme TOP 3 Scanner", page_icon="🛡️", layout="wide"
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
# 2. 데이터 추출 함수 (기존 로직 유지)
# ==========================================
@st.cache_data(ttl=1800)
def get_safe_kr_themes():
    tickers_dict = {}
    try:
        url = "https://finance.naver.com/sise/sise_quant.naver?sosok=1"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers)
        dfs = pd.read_html(res.text)
        df = dfs[1].dropna(subset=['종목명'])
        df = df[~df['종목명'].str.contains('ETN|ETF|레버리지|인버스|스팩|제이티|금융투자|우|우B', na=False)]
        df = df[df['현재가'] >= 3000]
        top_market = df.head(40)
        for _, row in top_market.iterrows():
            name = row['종목명']
            try:
                search_url = f"https://query2.finance.yahoo.com/v1/finance/search?q={name}"
                s_res = requests.get(search_url, headers=headers).json()
                symbol = s_res['quotes'][0]['symbol']
                if ".KQ" in symbol or ".KS" in symbol:
                    if name not in ["삼성전자", "SK하이닉스", "현대차", "기아", "LG에너지솔루션", "삼성바이오로직스", "셀트리온"]:
                        tickers_dict[symbol] = name
            except: pass
    except: tickers_dict = {"036570.KS": "엔씨소프트", "066970.KQ": "엘앤에프", "293490.KQ": "카카오게임즈"}
    return tickers_dict

def get_safe_us_movers():
    return ["PLTR", "MSTR", "HOOD", "ASTS", "MARA", "RIOT", "UPST", "AFRM", "SOFI", "RIVN", "DKNG", "CELH", "IONQ", "COIN", "AI", "SQ", "RBLX", "U", "NET", "SNOW"]

# ==========================================
# 3. 데이터 분석 및 핵심 스코어링 (병렬 처리용)
# ==========================================
def calculate_swing_score(df):
    if len(df) < 25: return 0, 0, 0
    current = df["Close"].iloc[-1]
    rsi = RSIIndicator(df["Close"]).rsi().iloc[-1]
    ma10 = df["Close"].rolling(10).mean().iloc[-1]
    ma20 = df["Close"].rolling(20).mean().iloc[-1]
    volume_now = df["Volume"].iloc[-1]
    volume_avg = df["Volume"].rolling(20).mean().iloc[-1]
    score = 0
    if 40 <= rsi <= 60: score += 40
    elif rsi < 40: score += 20
    if current > ma10: score += 20
    if current > ma20: score += 20
    if volume_now > volume_avg * 1.8: score += 40
    return score, current, rsi

def fetch_us(stock):
    try:
        df = yf.Ticker(stock).history(period="3mo")
        score, current, rsi = calculate_swing_score(df)
        if current == 0: return None
        return {"ticker": stock, "종목": stock, "점수": score, "현재가": round(current, 2), "RSI": round(rsi, 1),
                "매수구간": f"${round(current * 0.96, 2)} ~ ${round(current, 2)}", "목표가": round(current * 1.07, 2), "손절가": round(current * 0.94, 2)}
    except: return None

def fetch_crypto(coin):
    try:
        df = pyupbit.get_ohlcv(coin, interval="day", count=40)
        if df is None: return None
        df = df.rename(columns={"close": "Close", "volume": "Volume"})
        score, current, rsi = calculate_swing_score(df)
        if current == 0: return None
        return {"ticker": None, "코인": coin.replace("KRW-", ""), "점수": score, "현재가": current, "RSI": round(rsi, 1),
                "매수구간": f"{current * 0.96:,.0f} ~ {current:,.0f}", "목표가": round(current * 1.08, 0), "손절가": round(current * 0.94, 0)}
    except: return None

def fetch_kr(item):
    ticker, name = item
    try:
        df = yf.Ticker(ticker).history(period="3mo")
        score, current, rsi = calculate_swing_score(df)
        if current == 0: return None
        return {"ticker": ticker, "종목": name, "점수": score, "현재가": int(current), "RSI": round(rsi, 1),
                "매수구간": f"{int(current * 0.96):,} ~ {int(current):,}", "목표가": int(current * 1.07), "손절가": int(current * 0.94)}
    except: return None

def fetch_ticker_news(ticker_symbol):
    try:
        t = yf.Ticker(ticker_symbol)
        return [{"title": item.get("title", "최신 뉴스"), "link": item.get("link", "#")} for item in t.news[:2]]
    except: return []

def get_market_status():
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=3).json()
        fg_val = fg["data"][0]["value"]
        fg_num = int(fg_val)
        fg_txt = "극단적 탐욕" if fg_num >= 75 else "탐욕" if fg_num >= 60 else "중립" if fg_num >= 40 else "공포" if fg_num >= 25 else "극단적 공포"
        usd = yf.Ticker("KRW=X").history(period="1d")['Close'].iloc[-1]
        return fg_val, fg_txt, f"{usd:,.2f}"
    except: return "50", "중립", "1,350.00"

# ==========================================
# 실행부 (병렬 처리 적용)
# ==========================================
kr_live_dict = get_safe_kr_themes()
us_live_list = get_safe_us_movers()
coins = pyupbit.get_tickers(fiat="KRW")

with ThreadPoolExecutor(max_workers=20) as executor:
    us_top = sorted([r for r in executor.map(fetch_us, us_live_list) if r], key=lambda x: x["점수"], reverse=True)[:3]
    crypto_top = sorted([r for r in executor.map(fetch_crypto, coins[:30]) if r], key=lambda x: x["점수"], reverse=True)[:3]
    kr_top = sorted([r for r in executor.map(fetch_kr, kr_live_dict.items()) if r], key=lambda x: x["점수"], reverse=True)[:3]

# ==========================================
# UI 렌더링 (기존 그대로)
# ==========================================
fg_val, fg_txt, exchange = get_market_status()
st.sidebar.title("🛡️ Safety Theme Pulse")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.title("🚀 Tae's Balanced Smart TOP 3 Scanner")
st.divider()

for market_title, data, symbol in [("🇺🇸 해외 알짜 성장주 TOP 3", us_top, "$"), ("🪙 가상화폐 알트코인 실시간 TOP 3", crypto_top, ""), ("🇰🇷 국내 검증된 테마 대장주 TOP 3", kr_top, "")]:
    st.header(market_title)
    if data:
        cols = st.columns(3)
        for i, item in enumerate(data):
            with cols[i]:
                name_key = "종목" if "종목" in item else "코인"
                st.markdown(f"### 🥇 {['1등', '2등', '3등'][i]} 추천 : **{item[name_key]}**\n* 🔥 점수: `{item['점수']}점`\n* 💰 현재가: {symbol}{item['현재가']:,} (RSI: {item['RSI']})\n* 🎯 타점: `{item['매수구간']}`\n* 📈 목표: {symbol}{item['목표가']:,}\n* 📉 손절: {symbol}{item['손절가']:,}")
                if item.get("ticker"):
                    news = fetch_ticker_news(item["ticker"])
                    if news:
                        st.markdown("<div class='news-box'><b>📰 뉴스</b>", unsafe_allow_html=True)
                        for n in news: st.markdown(f"• <a href='{n['link']}'>{n['title']}</a>", unsafe_allow_html=True)
                        st.markdown("</div>", unsafe_allow_html=True)
    st.divider()
