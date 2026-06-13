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
# 1. 무적의 분석 엔진 (어떤 티커든 조회 가능)
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
def get_market_data(name):
    # 1. 코인 조회
    if name.isalpha() and len(name) <= 5:
        try:
            df = pyupbit.get_ohlcv(f"KRW-{name.upper()}", interval="day", count=40)
            if df is not None:
                df = df.rename(columns={"close": "Close"})
                s, c, r = calculate_swing_score(df)
                return name.upper(), c, s, r, "KRW"
        except: pass
    
    # 2. 주식 조회 (info 호출 없이 history만 사용해서 에러 원천 차단)
    try:
        t_input = name + ".KS" if name.isdigit() and len(name) == 6 else name
        ticker = yf.Ticker(t_input)
        df = ticker.history(period="3mo")
        
        # .KS 실패 시 .KQ 시도
        if df.empty and name.isdigit():
            t_input = name + ".KQ"
            ticker = yf.Ticker(t_input)
            df = ticker.history(period="3mo")
            
        if not df.empty:
            s, c, r = calculate_swing_score(df)
            # 통화 구분은 심플하게
            curr = "KRW" if (".KS" in t_input or ".KQ" in t_input) else "USD"
            return name.upper(), df["Close"].iloc[-1], s, r, curr
    except: pass
    return None, 0, 0, 0, "USD"

# ==========================================
# 2. UI
# ==========================================
st.set_page_config(page_title="Tae Scanner", layout="wide")
st.title("🚀 Tae's Smart Scanner")

if 'my_portfolio' not in st.session_state: st.session_state.my_portfolio = load_portfolio()

with st.form(key='portfolio_form', clear_on_submit=True):
    c1, c2, c3 = st.columns([2, 1, 1])
    n_in = c1.text_input("코드(005930) / 티커(VUZI, PLTR, NVDA)")
    b_in = c2.number_input("매수가", min_value=0.0, step=0.1)
    if c3.form_submit_button("➕ 추가"):
        if n_in:
            st.session_state.my_portfolio.append({"name": n_in.strip().upper(), "buy": float(b_in)})
            save_portfolio(st.session_state.my_portfolio)
            st.rerun()

for i, p in enumerate(st.session_state.my_portfolio):
    name, buy = p['name'], p['buy']
    stock_name, curr, score, rsi, currency = get_market_data(name)
    
    if curr == 0:
        st.error(f"⚠️ {name} 조회 실패. 티커 확인")
        if st.button(f"❌ 삭제 {name}", key=f"del_{i}"):
            st.session_state.my_portfolio.pop(i)
            save_portfolio(st.session_state.my_portfolio)
            st.rerun()
        continue
    
    profit = ((curr - buy) / buy * 100) if buy > 0 else 0
    sym = "$" if currency == "USD" else "₩"
    
    st.markdown("---")
    st.write(f"### 📈 {name} (실시간 분석 완료)")
    st.info(f"📊 분석 점수: {score}점 | RSI: {rsi:.1f}")
    
    df_guide = pd.DataFrame({
        "구분": ["현재가", "목표익절(+7%)", "방어손절(-6%)"],
        "가격": [f"{sym}{curr:,.2f}", f"{sym}{curr*1.07:,.2f}", f"{sym}{curr*0.94:,.2f}"]
    })
    st.table(df_guide)
    st.metric("수익률", f"{profit:.2f}%")
    
    if st.button(f"❌ 삭제 {name}", key=f"del_{i}"):
        st.session_state.my_portfolio.pop(i)
        save_portfolio(st.session_state.my_portfolio)
        st.rerun()
