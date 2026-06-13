import streamlit as st
import pyupbit
import yfinance as yf
import pandas as pd
import requests
from ta.momentum import RSIIndicator
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# 1. 캐시 함수 (속도 최적화)
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

# ==========================================
# 2. 스캐너 로직 (기존 기능)
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
# 3. UI 및 상태 관리
# ==========================================
st.set_page_config(page_title="Tae Scanner", layout="wide")
if 'my_portfolio' not in st.session_state: st.session_state.my_portfolio = []

fg_val, fg_txt, exchange = get_market_status()
st.sidebar.title("🛡️ Safety Theme Pulse")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.title("🚀 Tae's Balanced Smart TOP 3 Scanner")

# 실행 및 렌더링
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

# ==========================================
# 4. 실시간 자산 관리 모듈 (완전체)
# ==========================================
st.divider()
st.subheader("💰 나의 실시간 자산 관리")
with st.form(key='portfolio_form', clear_on_submit=True):
    cols = st.columns([2, 1, 1])
    n_in = cols[0].text_input("종목명/코인")
    b_in = cols[1].number_input("매수가", step=100.0)
    if cols[2].form_submit_button("➕ 추가"):
        if n_in: st.session_state.my_portfolio.append({"name": n_in.upper(), "buy": b_in})

for i, p in enumerate(st.session_state.my_portfolio):
    curr = get_live_price(p['name'])
    if curr == 0: continue
    st.markdown("---")
    st.write(f"### 📈 {p['name']} (매수: {int(p['buy']):,})")
    
    # 전략 테이블
    df_guide = pd.DataFrame({"구분": ["현재가", "익절(7%)", "손절(6%)", "관망"], 
                             "가격": [f"{int(curr):,}", f"{int(p['buy']*1.07):,}", f"{int(p['buy']*0.94):,}", f"{int(p['buy']*0.98):,}"]})
    st.table(df_guide)
    st.metric("수익률", f"{((curr - p['buy']) / p['buy']) * 100:.2f}%")
    
    if curr >= p['buy']*1.07: st.success("✅ [익절] 실현하세요!")
    elif curr <= p['buy']*0.94: st.error("🚨 [손절] 즉시 대응!")
    else: st.warning("⚖️ [관망] 안전합니다.")
        
    if st.button(f"❌ 삭제 {p['name']}", key=f"del_{i}", use_container_width=True):
        st.session_state.my_portfolio.pop(i)
        st.rerun()
