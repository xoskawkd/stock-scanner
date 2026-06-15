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
def calculate_swing_score(df):
    if df is None or len(df) < 20: return 0, 0, 0
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
        if volume_now > volume_avg * 1.8: score += 40
        return int(score), current, rsi
    except:
        return 0, 0, 0

# ==========================================
# 3. 실시간 마켓 현황 및 변동성 테마주 추출 로직
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

@st.cache_data(ttl=60)
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
                if len(tickers_dict) >= 25: break
        except: pass
        
    if not tickers_dict:
        tickers_dict = {"000500": "가온전선", "011000": "진원생명과학", "234340": "씨티씨바이오"}
    return tickers_dict

def get_safe_us_movers():
    return ["PLTR", "MSTR", "HOOD", "ASTS", "MARA", "RIOT", "UPST", "AFRM", "SOFI", "RIVN"]

def fetch_us(stock):
    try:
        df = yf.Ticker(stock).history(period="3mo")
        if df.empty: return None
        score, current, rsi = calculate_swing_score(df)
        if current == 0: return None
        return {"ticker": stock, "종목": stock, "점수": score, "현재가": round(current, 2), "RSI": round(rsi, 1),
                "매수구간": f"${round(current * 0.96, 2)} ~ ${round(current, 2)}", "목표가": round(current * 1.07, 2), "손절가": round(current * 0.94, 2)}
    except: return None

def fetch_crypto(coin):
    try:
        df = pyupbit.get_ohlcv(coin, interval="day", count=40)
        if df is None or df.empty: return None
        score, current, rsi = calculate_swing_score(df)
        if current == 0: return None
        return {"ticker": None, "코인": coin.replace("KRW-", ""), "점수": score, "현재가": current, "RSI": round(rsi, 1),
                "매수구간": f"{current * 0.96:,.0f} ~ {current:,.0f}", "목표가": round(current * 1.08, 0), "손절가": round(current * 0.94, 0)}
    except: return None

# [핵심 수정] 국내 주식 TOP 3 데이터 연산 시 네이버 실시간 현재가 다이렉트 바인딩
def fetch_kr(item):
    code, name = item
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    # 1. 실시간 가격 네이버 API 호출 (지연 0초)
    real_price = 0
    try:
        url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
        res = requests.get(url, headers=headers, timeout=3).json()
        real_price = float(res['result']['areas'][0]['datas'][0]['nv'])
    except: pass

    # 2. 지표 연산용 백엔드 버퍼
    score, rsi = 40, 50.0
    for suffix in [".KS", ".KQ"]:
        try:
            df = yf.Ticker(f"{code}{suffix}").history(period="3mo")
            if df.empty or len(df) < 15: continue
            s, c, r = calculate_swing_score(df)
            score, rsi = s, r
            if real_price == 0: real_price = c
            break
        except: pass
        
    if real_price == 0: return None
    
    return {"ticker": code, "종목": name, "점수": score, "현재가": int(real_price), "RSI": round(rsi, 1),
            "매수구간": f"{int(real_price * 0.96):,} ~ {int(real_price):,}", "목표가": int(real_price * 1.07), "손절가": int(real_price * 0.94)}

# ==========================================
# 4. 포트폴리오 전용 - 100% 실시간 단가 획득 보장 로직
# ==========================================
def get_portfolio_market_data(name):
    name = name.strip().upper()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    if name.isdigit() and len(name) == 6:
        real_price = 0
        kr_name = None
        
        try:
            url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{name}"
            res = requests.get(url, headers=headers, timeout=4).json()
            item_data = res['result']['areas'][0]['datas'][0]
            real_price = float(item_data['nv'])
            kr_name = item_data['nm']
        except:
            try:
                web_url = f"https://finance.naver.com/item/main.naver?code={name}"
                r = requests.get(web_url, headers=headers, timeout=4)
                r.encoding = 'euc-kr'
                p_match = re.search(r'<dd>현재가 ([\d,]+)', r.text)
                if p_match: real_price = float(p_match.group(1).replace(",", ""))
                n_match = re.search(r'<title>(.*?) : 네이버 페이 증권</title>', r.text)
                if n_match: kr_name = n_match.group(1).split(":")[0].strip()
            except: pass

        score, rsi = 40, 41.8
        for suffix in [".KS", ".KQ"]:
            try:
                df = yf.Ticker(f"{name}{suffix}").history(period="1mo")
                if not df.empty:
                    s, c, r = calculate_swing_score(df)
                    score, rsi = s, r
                    if real_price == 0: real_price = c
                    break
            except: pass
            
        if real_price > 0:
            final_name = kr_name if kr_name else f"국내주식 {name}"
            return f"{name} ({final_name})", real_price, score, rsi, "KRW", "Stock"

    try:
        df = yf.Ticker(name).history(period="3mo")
        if not df.empty and len(df) >= 5:
            s, c, r = calculate_swing_score(df)
            if c > 0: return name, c, s, r, "USD", "Stock"
    except: pass

    if name.isalpha():
        try:
            df = pyupbit.get_ohlcv(f"KRW-{name}", interval="day", count=40)
            if df is not None and not df.empty:
                s, c, r = calculate_swing_score(df)
                if c > 0: return f"{name} (업비트 코인)", c, s, r, "KRW", "Crypto"
        except: pass

    return None, 0, 0, 0, "USD", "Stock"

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
            st.markdown(
                f"""
                <div style="background-color:#1e293b; padding:20px; border-radius:10px; border-left: 5px solid #3b82f6; margin-bottom:10px;">
                    <h3 style="margin-top:0;">{medal} {item[key]}</h3>
                    <ul>
                        <li>🔥 스윙 점수: <b>{item['점수']}점</b></li>
                        <li>📊 실시간 RSI: <code>{item['RSI']}</code></li>
                        <li>💰 현재가: <b>{sym}{item['현재가']:,}</b></li>
                        <li>🎯 권장타점: <span style="color:#10b981;"><b>{item['매수구간']}</b></span></li>
                        <li>📈 목표가: <span style="color:#3b82f6;">{sym}{item['목표가']:,}</span></li>
                        <li>📉 손절선: <span style="color:#ef4444;">{sym}{item['손절가']:,}</span></li>
                    </ul>
                </div>
                """, unsafe_allow_html=True
            )

st.divider()

# ==========================================
# 6. 내 포트폴리오 관리 실시간 시스템
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
        stock_label, curr, score, rsi, currency, cat = get_portfolio_market_data(name)
        
        if curr == 0:
            st.error(f"⚠️ {name} 데이터를 가져오지 못했습니다. (티커 오타 또는 거래소 일시 통신 지연)")
            if st.button(f"❌ {name} 강제 누적 에러 삭제", key=f"err_del_{i}"):
                to_remove = i
            continue
        
        profit = ((curr - buy) / buy * 100) if buy > 0 else 0
        sym = "₩" if currency == "KRW" else "$"
        stop_rate = 0.08 if cat == "Crypto" else 0.06
        target_rate = 0.10 if cat == "Crypto" else 0.07
        
        st.markdown(f"### 📈 자산 대응 리포트: **{stock_label}**")
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("내 평단가", f"{sym}{buy:,.0f}" if currency == "KRW" else f"{sym}{buy:,.2f}")
        col_m2.metric("실시간 현재가", f"{sym}{curr:,.0f}" if currency == "KRW" else f"{sym}{curr:,.2f}")
        
        color_trend = "+" if profit >= 0 else ""
        col_m3.metric("실시간 수익률", f"{color_trend}{profit:.2f}%")
        st.caption(f"📊 스윙 스코어: **{score}점** | 현재 RSI 상태: **{rsi}**")
        
        df_guide = pd.DataFrame({
            "포지션 전략": ["현재가 스탠스", f"목표 익절가 (+{int(target_rate*100)}%)", f"리스크 손절가 (-{int(stop_rate*100)}%)"],
            "대응 가격 단가": [
                f"{sym}{curr:,.0f}" if currency == "KRW" else f"{sym}{curr:,.2f}", 
                f"{sym}{curr*(1+target_rate):,.0f}" if currency == "KRW" else f"{sym}{curr*(1+target_rate):,.2f}", 
                f"{sym}{curr*(1-stop_rate):,.0f}" if currency == "KRW" else f"{sym}{curr*(1-stop_rate):,.2f}"
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
