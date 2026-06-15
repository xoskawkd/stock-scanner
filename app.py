import streamlit as st
import pyupbit
import yfinance as yf
import pandas as pd
import requests
import json
import os
import re
from ta.momentum import RSIIndicator
from concurrent.futures import ThreadPoolExecutor

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
# 2. 통합 핵심 분석 엔진
# ==========================================
def calculate_swing_score_and_bands(df):
    if df is None or len(df) < 20: return 0, 0, 0, "계산불가", 0, 0
    try:
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        current = float(df["close"].iloc[-1])
        rsi = float(RSIIndicator(df["close"]).rsi().iloc[-1])
        ma10 = float(df["close"].rolling(10).mean().iloc[-1])
        ma20 = float(df["close"].rolling(20).mean().iloc[-1])
        volume_now = float(df["volume"].iloc[-1])
        volume_avg = float(df["volume"].rolling(20).mean().iloc[-1])
        
        score = 0
        if 40 <= rsi <= 60: score += 40
        elif rsi < 40: score += 20
        if current > ma10: score += 20
        if current > ma20: score += 20
        if volume_now > volume_avg * 1.5: score += 40
        
        if ma10 >= ma20: buy_min, buy_max = ma20, ma10
        else: buy_min, buy_max = ma10, ma20
            
        return int(score), current, rsi, buy_min, buy_max, ma20
    except:
        return 40, 0, 50.0, 0, 0, 0

# ==========================================
# 3. 실시간 매싱 (거래량 필터 적용 완료)
# ==========================================
def fetch_crypto(coin):
    try:
        df = pyupbit.get_ohlcv(coin, interval="day", count=40)
        if df is None or df.empty: return None
        
        # 🎯 [핵심] 거래대금 50억 미만 필터링 (거래 안 되는 잡코인 제거)
        if (df['volume'].iloc[-1] * df['close'].iloc[-1]) < 5000000000:
            return None
            
        score, current, rsi, buy_min, buy_max, ma20 = calculate_swing_score_and_bands(df)
        if current == 0: return None
        return {"ticker": None, "코인": coin.replace("KRW-", ""), "점수": score, "현재가": current, "RSI": round(rsi, 1),
                "매수구간": f"{int(buy_min):,} ~ {int(buy_max):,}", "목표가": round(current * 1.08, 0), "손절가": round(min(buy_min*0.98, current * 0.94), 0)}
    except: return None

# (나머지 국내 주식, 해외 주식 로직은 그대로 유지)
def calculate_kr_realtime_score(code):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        api_url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
        api_res = requests.get(api_url, headers=headers, timeout=3).json()
        item_data = api_res['result']['areas'][0]['datas'][0]
        current = float(item_data['nv']) 
        volume_now = float(item_data['aq']) 
        df = yf.Ticker(f"{code}.KS").history(period="3mo")
        if df.empty: df = yf.Ticker(f"{code}.KQ").history(period="3mo")
        df.iloc[-1, df.columns.get_loc('Close')] = current
        df.iloc[-1, df.columns.get_loc('Volume')] = volume_now
        score, _, rsi, buy_min, buy_max, ma20 = calculate_swing_score_and_bands(df)
        return score, current, rsi, f"{int(buy_min):,} ~ {int(buy_max):,}", int(current * 1.07), int(min(buy_min * 0.98, current * 0.94))
    except: return 40, 0, 50.0, "데이터 동기화 실패", 0, 0

@st.cache_data(ttl=30)
def get_market_status():
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=3).json()
        fg_val = fg["data"][0]["value"]
        fg_txt = "극단적 탐욕" if int(fg_val) >= 75 else "탐욕" if int(fg_val) >= 60 else "중립" if int(fg_val) >= 40 else "공포" if int(fg_val) >= 25 else "극단적 공포"
        usd = yf.Ticker("KRW=X").history(period="1d")['Close'].iloc[-1]
        return fg_val, fg_txt, f"{usd:,.2f}"
    except: return "50", "중립", "1,350.00"

@st.cache_data(ttl=30)
def get_realtime_kr_hot_stocks():
    tickers_dict = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for sosok in [0, 1]:
        try:
            url = f"https://finance.naver.com/sise/sise_tr_amount.naver?sosok={sosok}"
            res = requests.get(url, headers=headers, timeout=5)
            res.encoding = 'euc-kr'
            matches = re.findall(r'href="/item/main\.naver\?code=(\d{6})".*?class="tltle">(.*?)</a>', res.text)
            for code, name in matches:
                if any(x in name for x in ['ETN', 'ETF', '레버리지', '인버스', '스팩', '우', '지수', '홀딩스', '투자', '삼성전자', 'SK하이닉스', '현대차', '기아', 'LG에너지', '셀트리온']): continue
                tickers_dict[code] = name
                if len(tickers_dict) >= 20: break
        except: pass
    return tickers_dict

def get_safe_us_movers(): return ["PLTR", "MSTR", "HOOD", "ASTS", "MARA", "RIOT", "UPST", "AFRM", "SOFI", "RIVN"]

def fetch_us(stock):
    try:
        df = yf.Ticker(stock).history(period="3mo")
        if df.empty: return None
        score, current, rsi, buy_min, buy_max, ma20 = calculate_swing_score_and_bands(df)
        if current == 0: return None
        return {"ticker": stock, "종목": stock, "점수": score, "현재가": round(current, 2), "RSI": round(rsi, 1),
                "매수구간": f"${round(buy_min, 2)} ~ ${round(buy_max, 2)}", "목표가": round(current * 1.07, 2), "손절가": round(min(buy_min*0.98, current * 0.94), 2)}
    except: return None

def fetch_kr(item):
    code, name = item
    score, real_price, rsi, buy_range, target_price, stop_price = calculate_kr_realtime_score(code)
    if real_price == 0: return None
    return {"ticker": code, "종목": name, "점수": score, "현재가": int(real_price), "RSI": round(rsi, 1),
            "매수구간": buy_range, "목표가": target_price, "손절가": stop_price}

def get_portfolio_market_data(name):
    name = name.strip().upper()
    if name.isdigit() and len(name) == 6:
        score, real_price, rsi, buy_range, target_price, stop_price = calculate_kr_realtime_score(name)
        if real_price > 0: return f"국내주식 {name}", real_price, score, rsi, "KRW", "Stock", stop_price, target_price
    try:
        df = yf.Ticker(name).history(period="3mo")
        if not df.empty and len(df) >= 5:
            s, c, r, b_min, b_max, ma20 = calculate_swing_score_and_bands(df)
            if c > 0: return name, c, s, r, "USD", "Stock", min(b_min*0.98, c*0.94), c*1.07
    except: pass
    if name.isalpha():
        try:
            df = pyupbit.get_ohlcv(f"KRW-{name}", interval="day", count=40)
            if df is not None and not df.empty:
                s, c, r, b_min, b_max, ma20 = calculate_swing_score_and_bands(df)
                if c > 0: return f"{name} (코인)", c, s, r, "KRW", "Crypto", min(b_min*0.98, c*0.92), c*1.10
        except: pass
    return None, 0, 0, 0, "USD", "Stock", 0, 0

# (UI 대시보드 및 포트폴리오 관리 로직은 기존과 동일)
fg_val, fg_txt, exchange = get_market_status()
st.sidebar.title("🛡️ Safety Theme Pulse")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.title("🚀 Tae's Balanced Smart TOP 3 Scanner")

kr_live_dict = get_realtime_kr_hot_stocks()
us_live_list = get_safe_us_movers()
try: coins_list = pyupbit.get_tickers(fiat="KRW")[:30]
except: coins_list = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]

with ThreadPoolExecutor(max_workers=20) as executor:
    us_top = sorted([r for r in executor.map(fetch_us, us_live_list) if r], key=lambda x: x["점수"], reverse=True)[:3]
    crypto_top = sorted([r for r in executor.map(fetch_crypto, coins_list) if r], key=lambda x: x["점수"], reverse=True)[:3]
    kr_top = sorted([r for r in executor.map(fetch_kr, kr_live_dict.items()) if r], key=lambda x: x["점수"], reverse=True)[:3]

for title, data, sym in [("🇺🇸 해외 알짜 성장주 TOP 3", us_top, "$"), ("🪙 코인 TOP 3", crypto_top, ""), ("🔥 국내 테마/거래대금 대장주 TOP 3", kr_top, "₩")]:
    st.header(title)
    if not data: st.warning("시장 데이터를 동기화 중입니다.")
    else:
        cols = st.columns(3)
        for i, item in enumerate(data):
            with cols[i]:
                key = "종목" if "종목" in item else "코인"
                st.markdown(f"""<div style="background-color:#1e293b; padding:20px; border-radius:10px; border-left: 5px solid #10b981;">
                    <h3>{item[key]}</h3><ul><li>점수: <b>{item['점수']}</b></li><li>현재가: <b>{sym}{item['현재가']:,}</b></li><li>매수구간: {item['매수구간']}</li></ul></div>""", unsafe_allow_html=True)

st.header("💼 실시간 내 자산 관리")
with st.form(key='portfolio_form', clear_on_submit=True):
    c1, c2, c3 = st.columns([2, 1, 1])
    n_in = c1.text_input("종목코드/티커 입력")
    b_in = c2.number_input("내 매수가", min_value=0.0, step=0.01)
    if c3.form_submit_button("➕ 추가"):
        st.session_state.my_portfolio.append({"name": n_in.strip().upper(), "buy": float(b_in)})
        save_portfolio(st.session_state.my_portfolio)
        st.rerun()

if st.session_state.my_portfolio:
    for i, p in enumerate(st.session_state.my_portfolio):
        name, buy = p['name'], p['buy']
        stock_label, curr, score, rsi, currency, cat, calc_stop, calc_target = get_portfolio_market_data(name)
        if curr > 0:
            st.write(f"**{stock_label}**: 현재 {curr:,} | 수익률 {((curr-buy)/buy*100):.2f}%")
            if st.button(f"삭제", key=f"del_{i}"):
                st.session_state.my_portfolio.pop(i)
                save_portfolio(st.session_state.my_portfolio)
                st.rerun()
