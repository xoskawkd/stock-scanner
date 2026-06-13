import streamlit as st
import pyupbit
import yfinance as yf
import pandas as pd
import requests
import json
import os
from ta.momentum import RSIIndicator
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# 0. 데이터 영구 저장 로직 (데이터 유지용)
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
# 2. 캐시 함수 및 스캐너 로직 (대표님 기능 그대로)
# ==========================================
@st.cache_data(ttl=30)
def get_live_price(name):
    try:
        price = pyupbit.get_current_price(f"KRW-{name}")
        if price: return price
        ticker = yf.Ticker(name)
        data = ticker.history(period="1d")
        return data['Close'].iloc[-1] if not data.empty else 0
    except: return 0

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
        fg_txt = "극단적 탐욕" if int(fg_val) >= 75 else "탐욕" if int(fg_val) >= 60 else "중립" if int(fg_val) >= 40 else "공포" if int(fg_val) >= 25 else "극단적 공포"
        usd = yf.Ticker("KRW=X").history(period="1d")['Close'].iloc[-1]
        return fg_val, fg_txt, f"{usd:,.2f}"
    except: return "50", "중립", "1,350.00"

# ==========================================
# 3. UI 렌더링
# ==========================================
fg_val, fg_txt, exchange = get_market_status()
st.sidebar.title("🛡️ Safety Theme Pulse")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.title("🚀 Tae's Balanced Smart TOP 3 Scanner")

kr_live_dict = get_safe_kr_themes()
us_live_list = get_safe_us_movers()
coins = pyupbit.get_tickers(fiat="KRW")

with ThreadPoolExecutor(max_workers=20) as executor:
    us_top = sorted([r for r in executor.map(fetch_us, us_live_list) if r], key=lambda x: x["점수"], reverse=True)[:3]
    crypto_top = sorted([r for r in executor.map(fetch_crypto, coins[:30]) if r], key=lambda x: x["점수"], reverse=True)[:3]
    kr_top = sorted([r for r in executor.map(fetch_kr, kr_live_dict.items()) if r], key=lambda x: x["점수"], reverse=True)[:3]

for title, data, sym in [("🇺🇸 해외 알짜 성장주 TOP 3", us_top, "$"), ("🪙 코인 TOP 3", crypto_top, ""), ("🇰🇷 국내 테마 대장주 TOP 3", kr_top, "")]:
    st.header(title)
    cols = st.columns(3)
    for i, item in enumerate(data):
        with cols[i]:
            key = "종목" if "종목" in item else "코인"
            st.markdown(f"### 🥇 {item[key]}\n* 🔥 점수: `{item['점수']}점`\n* 💰 현재가: {sym}{item['현재가']:,}\n* 🎯 타점: `{item['매수구간']}`\n* 📈 목표: {sym}{item['목표가']:,}\n* 📉 손절: {sym}{item['손절가']:,}")

import streamlit as st
import pyupbit
import yfinance as yf
import pandas as pd
import json
import os
from ta.momentum import RSIIndicator

# ==========================================
# 0. 데이터 저장 로직
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
# 1. 통합 분석 엔진 (TOP3와 동일한 계산식)
# ==========================================
def calculate_swing_score(df):
    if len(df) < 25: return 0, 0, 0
    current = df["Close"].iloc[-1]
    rsi = RSIIndicator(df["Close"]).rsi().iloc[-1]
    ma10 = df["Close"].rolling(10).mean().iloc[-1]
    ma20 = df["Close"].rolling(20).mean().iloc[-1]
    score = 0
    if 40 <= rsi <= 60: score += 40
    elif rsi < 40: score += 20
    if current > ma10: score += 20
    if current > ma20: score += 20
    return int(score), float(current), float(rsi)

@st.cache_data(ttl=60)
def get_detailed_info(name):
    # 1. 코인
    if name.isalpha():
        try:
            df = pyupbit.get_ohlcv(f"KRW-{name.upper()}", interval="day", count=40)
            if df is not None:
                df = df.rename(columns={"close": "Close", "volume": "Volume"})
                score, curr, rsi = calculate_swing_score(df)
                return name.upper(), curr, score, rsi, "KRW"
        except: pass
    
    # 2. 주식
    try:
        ticker_input = name + ".KS" if name.isdigit() and len(name) == 6 else name
        ticker = yf.Ticker(ticker_input)
        df = ticker.history(period="3mo")
        # 실패 시 .KQ 재시도
        if df.empty and name.isdigit():
            ticker = yf.Ticker(name + ".KQ")
            df = ticker.history(period="3mo")
            
        if not df.empty:
            score, curr, rsi = calculate_swing_score(df)
            currency = "KRW" if (".KS" in str(ticker.ticker) or ".KQ" in str(ticker.ticker)) else "USD"
            return ticker.info.get('longName', name), curr, score, rsi, currency
    except: pass
    return None, 0, 0, 0, "KRW"

# ==========================================
# 2. UI 및 자산 관리
# ==========================================
st.set_page_config(page_title="Tae Scanner", layout="wide")
st.title("🚀 Tae's Smart Scanner")

if 'my_portfolio' not in st.session_state:
    st.session_state.my_portfolio = load_portfolio()

with st.form(key='portfolio_form', clear_on_submit=True):
    cols = st.columns([2, 1, 1])
    n_in = cols[0].text_input("코드(005930) / 티커(PLTR) / 코인(BTC)")
    b_in = cols[1].number_input("매수가", min_value=0.0, value=0.0, step=100.0)
    if cols[2].form_submit_button("➕ 추가"):
        if n_in and b_in >= 0:
            st.session_state.my_portfolio.append({"name": n_in.strip(), "buy": float(b_in)})
            save_portfolio(st.session_state.my_portfolio)
            st.rerun()

# 보유 종목 출력
for i, p in enumerate(st.session_state.my_portfolio):
    name, buy = p['name'], p['buy']
    stock_name, curr, score, rsi, currency = get_detailed_info(name)
    
    if curr == 0:
        st.error(f"⚠️ {name} 조회 실패.")
        if st.button(f"❌ 삭제 {name}", key=f"del_{i}"):
            st.session_state.my_portfolio.pop(i)
            save_portfolio(st.session_state.my_portfolio)
            st.rerun()
        continue
    
    profit = ((curr - buy) / buy * 100) if buy > 0 else 0
    sym = "$" if currency == "USD" else "₩"
    
    st.markdown("---")
    st.write(f"### 📈 {stock_name if stock_name else name} ({name})")
    
    # TOP3처럼 분석 점수 표기
    st.info(f"📊 **분석 점수:** `{score}점` | **RSI:** `{rsi:.1f}`")
    
    df_guide = pd.DataFrame({
        "구분": ["현재가", "익절(7%)", "손절(6%)"],
        "가격": [f"{sym}{curr:,.2f}", f"{sym}{buy*1.07:,.2f}", f"{sym}{buy*0.94:,.2f}"]
    })
    st.table(df_guide)
    st.metric("수익률", f"{profit:.2f}%")
    
    if st.button(f"❌ 삭제 {name}", key=f"del_{i}"):
        st.session_state.my_portfolio.pop(i)
        save_portfolio(st.session_state.my_portfolio)
        st.rerun()
