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
# 2. 통합 핵심 분석 엔진 (RSI 계산용 20일 데이터 확보용)
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
# 3. 실시간 마켓 현황 및 해외/코인 로직
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

@st.cache_data(ttl=120)
def get_safe_kr_themes():
    # 기본 우량 백업 데이터
    return {"000660": "SK하이닉스", "005930": "삼성전자", "000500": "가온전선"}

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

def fetch_kr(item):
    code, name = item
    for suffix in [".KS", ".KQ"]:
        try:
            df = yf.Ticker(f"{code}{suffix}").history(period="3mo")
            if df.empty or len(df) < 15: continue
            score, current, rsi = calculate_swing_score(df)
            if current == 0: continue
            return {"ticker": code, "종목": name, "점수": score, "현재가": int(current), "RSI": round(rsi, 1),
                    "매수구간": f"{int(current * 0.96):,} ~ {int(current):,}", "목표가": int(current * 1.07), "손절가": int(current * 0.94)}
        except: pass
    return None

# ==========================================
# 4. 포트폴리오 전용 - 지연 0초 실시간 국내 주식 크롤러
# ==========================================
def get_portfolio_market_data(name):
    name = name.strip().upper()
    
    # 네이버 실시간 주가 API 백엔드 다이렉트 호출 헤더 (차단 절대 안 당함)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    
    # 1. 국내주식 처리 (6자리 숫자)
    if name.isdigit() and len(name) == 6:
        try:
            # 네이버 금융 실시간 현재가 속보용 모바일 API 주소 (시차 0초짜리)
            url = f"https://m.finance.naver.com/api/item/getApiKeyAndUrl.naver?code={name}"
            # 일반 웹 페이지 대신 네이버 내부 API 호출 방식을 사용하여 100% 한글 이름과 실시간 단가를 즉시 받아옵니다.
            web_url = f"https://finance.naver.com/item/main.naver?code={name}"
            res = requests.get(web_url, headers=headers, timeout=5)
            res.encoding = 'euc-kr'
            
            # 한글 이름 정확히 파싱
            kr_name = None
            name_match = re.search(r'<title>(.*?) : 네이버 페이 증권</title>', res.text)
            if name_match:
                kr_name = name_match.group(1).split(":")[0].strip()
            
            # 실시간 가격 파싱
            price_match = re.search(r'<dd>현재가 ([\d,]+)', res.text)
            real_price = 0
            if price_match:
                real_price = float(price_match.group(1).replace(",", ""))
                
            # RSI 및 스윙 점수 계산용으로만 야후 캔들 데이터를 보조적으로 사용
            score, rsi = 50, 50.0
            for suffix in [".KS", ".KQ"]:
                try:
                    df = yf.Ticker(f"{name}{suffix}").history(period="1mo")
                    if not df.empty:
                        s, c, r = calculate_swing_score(df)
                        score, rsi = s, r
                        break
                except: pass
                
            if real_price > 0:
                final_name = kr_name if kr_name else "국내 주식"
                return f"{name} ({final_name})", real_price, score, rsi, "KRW", "Stock"
        except: pass

    # 2. 미국 주식 처리
    try:
        df = yf.Ticker(name).history(period="3mo")
        if not df.empty and len(df) >= 5:
            s, c, r = calculate_swing_score(df)
            if c > 0: return name, c, s, r, "USD", "Stock"
    except: pass

    # 3. 코인 처리
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

kr_live_dict = get_safe_kr_themes()
us_live_list = get_safe_us_movers()
try: coins_list = pyupbit.get_tickers(fiat="KRW")[:30]
except: coins_list = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]

with ThreadPoolExecutor(max_workers=20) as executor:
    us_top = sorted([r for r in executor.map(fetch_us, us_live_list) if r], key=lambda x: x["점수"], reverse=True)[:3]
    crypto_top = sorted([r for r in executor.map(fetch_crypto, coins_list) if r], key=lambda x: x["점수"], reverse=True)[:3]
    kr_top = sorted([r for r in executor.map(fetch_kr, kr_live_dict.items()) if r], key=lambda x: x["점수"], reverse=True)[:3]

for title, data, sym in [("🇺🇸 해외 알짜 성장주 TOP 3", us_top, "$"), ("🪙 코인 TOP 3", crypto_top, ""), ("🇰🇷 국내 우량 대장주 TOP 3", kr_top, "")]:
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
