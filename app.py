import streamlit as st
import pyupbit
import yfinance as yf
import pandas as pd
import requests
import json
import os
import re
import FinanceDataReader as fdr
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
# 2. 통합 핵심 분석 및 [동적 눌림목] 타점 산출 엔진
# ==========================================
def calculate_swing_score_and_bands(df):
    if df is None or len(df) < 60: return 0, 0, 0, "계산불가", 0, 0
    try:
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        current = float(df["close"].iloc[-1])
        rsi = float(RSIIndicator(df["close"]).rsi().iloc[-1])
        
        # 이동평균선 확보
        ma10 = float(df["close"].rolling(10).mean().iloc[-1])
        ma20 = float(df["close"].rolling(20).mean().iloc[-1])
        ma60 = float(df["close"].rolling(60).mean().iloc[-1])
        
        # 거래량 필터 업그레이드: 급등 상투 방지 및 눌림목 포착
        volume_now = float(df["volume"].iloc[-1])
        volume_avg = float(df["volume"].rolling(20).mean().iloc[-1])
        vol_ratio = volume_now / volume_avg if volume_avg > 0 else 1
        
        score = 0
        if 40 <= rsi <= 60: score += 40
        elif rsi < 40: score += 20
        if current > ma10: score += 20
        if current > ma20: score += 20
        
        # 거래량 로직: 2배 이상 과열 시 점수 차감, 0.8~1.3배(안정적 눌림) 가점
        if 0.8 <= vol_ratio <= 1.3: score += 40
        elif vol_ratio > 2.0: score -= 20
        
        # [동적 타점] 시장 상황에 따른 밴드 변경
        # RSI가 낮아 급락장일 경우 60일선까지 밴드 확장 (보수적 대응)
        if rsi < 40: buy_min, buy_max = ma60, ma20
        else: buy_min, buy_max = (ma20, ma10) if ma10 >= ma20 else (ma10, ma20)
            
        return int(score), current, rsi, buy_min, buy_max, ma20
    except:
        return 40, 0, 50.0, 0, 0, 0

# ==========================================
# 100% 국산 실시간 매싱 + 지지선 타점 연산 결합 (안정성 강화)
# ==========================================
def calculate_kr_realtime_score(code):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://finance.naver.com/item/main.naver?code={code}"
    }
    try:
        # 네이버 API 요청 시 타임아웃 및 예외 처리 강화
        api_url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
        api_res = requests.get(api_url, headers=headers, timeout=5).json()
        item_data = api_res['result']['areas'][0]['datas'][0]
        current = float(item_data['nv']) 
        volume_now = float(item_data['aq']) 
        
        df = yf.Ticker(f"{code}.KS" if code.startswith(('00','01','02','03','04','05','06')) else f"{code}.KQ").history(period="3mo")
        if df.empty: df = yf.Ticker(f"{code}.KQ").history(period="3mo")
            
        if df.empty:
            return 40, current, 50.0, f"{int(current*0.96):,} ~ {int(current):,}", int(current*1.07), int(current*0.94)
        
        df.iloc[-1, df.columns.get_loc('Close')] = current
        df.iloc[-1, df.columns.get_loc('Volume')] = volume_now
        
        score, _, rsi, buy_min, buy_max, ma20 = calculate_swing_score_and_bands(df)
        
        buy_range = f"{int(buy_min):,} ~ {int(buy_max):,}"
        target_price = int(current * 1.07)
        # 보수적 손절선 (지지선 이탈 혹은 -6%)
        stop_price = int(min(buy_min * 0.98, current * 0.94))
        
        return score, current, rsi, buy_range, target_price, stop_price
    except:
        return 40, 0, 50.0, "데이터 동기화 실패", 0, 0

# ==========================================
# 3. 실시간 마켓 현황 및 추출 로직
# ==========================================
@st.cache_data(ttl=30)
def get_market_status():
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=3).json()
        fg_val = fg["data"][0]["value"]
        fg_txt = "극단적 탐욕" if int(fg_val) >= 75 else "탐욕" if int(fg_val) >= 60 else "중립" if int(fg_val) >= 40 else "공포" if int(fg_val) >= 25 else "극단적 공포"
        usd = yf.Ticker("KRW=X").history(period="1d")['Close'].iloc[-1]
        return fg_val, fg_txt, f"{usd:,.2f}"
    except: return "50", "중립", "1,350.00"

import FinanceDataReader as fdr

@st.cache_data(ttl=600)
def get_realtime_kr_hot_stocks():
    df = fdr.StockListing('KRX')
    
    # 1. 시총 5천억 이상 우량주
    df = df[df['Marcap'] > 500_000_000_000]
    
    # 2. 거래대금 필터
    if 'Amount' in df.columns:
        df = df[df['Amount'] >= 10_000_000_000]
        
    # 3. 🔥 핵심 수정: 너무 횡보만 남는 문제 해결
    # → "살짝 움직이기 시작한 종목"까지 포함
    if 'ChangeRate' in df.columns:
        df = df[(df['ChangeRate'] > -3.0) & (df['ChangeRate'] < 2.5)]
    
    # 4. 거래대금 상위 유지
    df = df.sort_values(by='Amount', ascending=False).head(20)
    
    # 5. 안정 샘플
    sampled = df.sample(n=min(5, len(df)), random_state=42)
    
    return dict(zip(sampled['Code'], sampled['Name']))






















def get_safe_us_movers():
    return ["PLTR", "MSTR", "HOOD", "ASTS", "MARA", "RIOT", "UPST", "AFRM", "SOFI", "RIVN"]

def fetch_us(stock):
    try:
        df = yf.Ticker(stock).history(period="3mo")
        if df.empty: return None
        score, current, rsi, buy_min, buy_max, ma20 = calculate_swing_score_and_bands(df)
        if current == 0: return None
        return {"ticker": stock, "종목": stock, "점수": score, "현재가": round(current, 2), "RSI": round(rsi, 1),
                "매수구간": f"${int(buy_min)} ~ ${int(buy_max)}", "목표가": round(current * 1.07, 2), "손절가": round(min(buy_min*0.98, current * 0.94), 2)}
    except: return None

def fetch_crypto(coin):
    try:
        df = pyupbit.get_ohlcv(coin, interval="day", count=60)
        if df is None or df.empty: return None
        score, current, rsi, buy_min, buy_max, ma20 = calculate_swing_score_and_bands(df)
        if current == 0: return None
        return {"ticker": None, "코인": coin.replace("KRW-", ""), "점수": score, "현재가": current, "RSI": round(rsi, 1),
                "매수구간": f"{int(buy_min):,} ~ {int(buy_max):,}", "목표가": round(current * 1.08, 0), "손절가": round(min(buy_min*0.98, current * 0.94), 0)}
    except: return None

def fetch_kr(item):
    code, name = item
    score, real_price, rsi, buy_range, target_price, stop_price = calculate_kr_realtime_score(code)
    if real_price == 0: return None
    return {"ticker": code, "종목": name, "점수": score, "현재가": int(real_price), "RSI": round(rsi, 1),
            "매수구간": buy_range, "목표가": target_price, "손절가": stop_price}

# ==========================================
# 4. 포트폴리오 자산 실시간 매싱 연동
# ==========================================
def get_portfolio_market_data(name):
    name = name.strip().upper()
    if name.isdigit() and len(name) == 6:
        score, real_price, rsi, buy_range, target_price, stop_price = calculate_kr_realtime_score(name)
        return f"{name} (국내주식)", real_price, score, rsi, "KRW", "Stock", stop_price, target_price
    try:
        df = yf.Ticker(name).history(period="3mo")
        if not df.empty and len(df) >= 20:
            s, c, r, b_min, b_max, ma20 = calculate_swing_score_and_bands(df)
            if c > 0: return name, c, s, r, "USD", "Stock", min(b_min*0.98, c*0.94), c*1.07
    except: pass
    if name.isalpha():
        try:
            df = pyupbit.get_ohlcv(f"KRW-{name}", interval="day", count=60)
            if df is not None and not df.empty:
                s, c, r, b_min, b_max, ma20 = calculate_swing_score_and_bands(df)
                if c > 0: return f"{name} (업비트 코인)", c, s, r, "KRW", "Crypto", min(b_min*0.98, c*0.92), c*1.10
        except: pass
    return None, 0, 0, 0, "USD", "Stock", 0, 0

# ==========================================
# 5. UI 메인 대시보드 렌더링
# ==========================================
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
    if not data:
        st.warning("시장 데이터를 동기화 중입니다.")
        continue
    cols = st.columns(3)
    for i, item in enumerate(data):
        with cols[i]:
            key = "종목" if "종목" in item else "코인"
            medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉"
            st.markdown(f"""<div style="background-color:#1e293b; padding:20px; border-radius:10px; border-left: 5px solid #10b981; margin-bottom:10px;">
                <h3 style="margin-top:0;">{medal} {item[key]}</h3>
                <ul><li>🔥 스윙 점수: <b>{item['점수']}점</b></li>
                <li>📊 실시간 RSI: <code>{item['RSI']}</code></li>
                <li>💰 현재가: <b>{sym}{item['현재가']:,}</b></li>
                <li>🎯 정석 지지타점: <span style="color:#10b981;"><b>{item['매수구간']}</b></span></li>
                <li>📈 목표가: <span style="color:#3b82f6;">{sym}{item['목표가']:,}</span></li>
                <li>📉 손절선: <span style="color:#ef4444;">{sym}{item['손절가']:,}</span></li></ul></div>""", unsafe_allow_html=True)

st.divider()
# ==========================================
# 6. 내 포트폴리오 관리 시스템
# ==========================================
st.header("💼 실시간 내 자산 관리 피드")

with st.form(key='portfolio_form', clear_on_submit=True):
    c1, c2, c3 = st.columns([2, 1, 1])
    n_in = c1.text_input("종목코드(예: 005930) / 티커(예: PLTR, VUZI, BTC)", placeholder="국내주식은 6자리 숫자, 해외주식/코인은 영문 티커 입력")
    b_in = c2.number_input("내 매수가", min_value=0.0, step=0.01, format="%.2f")
    if c3.form_submit_button("➕ 포트폴리오 추가"):
        if n_in:
            st.session_state.my_portfolio.append({"name": n_in.strip().upper(), "buy": float(b_in)})
            save_portfolio(st.session_state.my_portfolio)
            st.rerun()

if st.session_state.my_portfolio:
    to_remove = None
    for i, p in enumerate(st.session_state.my_portfolio):
        name, buy = p['name'], p['buy']
        stock_label, curr, score, rsi, currency, cat, calc_stop, calc_target = get_portfolio_market_data(name)
        
        if curr == 0:
            st.error(f"⚠️ {name} 데이터를 가져오지 못했습니다. (티커 오타 또는 거래소 일시 통신 지연)")
            if st.button(f"❌ {name} 강제 누적 에러 삭제", key=f"err_del_{i}"):
                to_remove = i
            continue
        
        profit = ((curr - buy) / buy * 100) if buy > 0 else 0
        sym = "₩" if currency == "KRW" else "$"
        
        st.markdown(f"### 📈 자산 대응 리포트: **{stock_label}**")
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("내 평단가", f"{sym}{buy:,.0f}" if currency == "KRW" else f"{sym}{buy:,.2f}")
        col_m2.metric("실시간 현재가", f"{sym}{curr:,.0f}" if currency == "KRW" else f"{sym}{curr:,.2f}")
        
        color_trend = "+" if profit >= 0 else ""
        col_m3.metric("실시간 수익률", f"{color_trend}{profit:.2f}%")
        st.caption(f"📊 스윙 스코어: **{score}점** | 현재 RSI 상태: **{rsi}**")
        
        df_guide = pd.DataFrame({
            "포지션 전략": ["현재가 스탠스", "목표 익절가 (정밀)", "리스크 손절가 (지지선 이탈)"],
            "대응 가격 단가": [
                f"{sym}{curr:,.0f}" if currency == "KRW" else f"{sym}{curr:,.2f}", 
                f"{sym}{calc_target:,.0f}" if currency == "KRW" else f"{sym}{calc_target:,.2f}", 
                f"{sym}{calc_stop:,.0f}" if currency == "KRW" else f"{sym}{calc_stop:,.2f}"
            ]
        })
        st.table(df_guide)
        
        if st.button(f"🗑️ {name} 삭제", key=f"del_final_{i}"):
            to_remove = i
        st.markdown("<br>", unsafe_allow_html=True)
        
    if to_remove is not None:
        st.session_state.my_portfolio.pop(to_remove)
        save_portfolio(st.session_state.my_portfolio)
        st.rerun()
else:
    st.info("현재 등록된 관심 자산이 없습니다. 위 입력창에 등록해 보세요!")
