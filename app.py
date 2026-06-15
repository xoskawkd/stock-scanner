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
    if len(df) < 20: return 0, 0, 0
    df.columns = [c.lower() for c in df.columns]
    current = df["close"].iloc[-1]
    rsi = RSIIndicator(df["close"]).rsi().iloc[-1]
    ma10 = df["close"].rolling(10).mean().iloc[-1]
    ma20 = df["close"].rolling(20).mean().iloc[-1]
    
    volume_now = df["volume"].iloc[-1]
    volume_avg = df["volume"].rolling(20).mean().iloc[-1]
    
    score = 0
    if 40 <= rsi <= 60: score += 40
    elif rsi < 40: score += 20
    if current > ma10: score += 20
    if current > ma20: score += 20
    if volume_now > volume_avg * 1.8: score += 40
    return int(score), float(current), float(rsi)

# ==========================================
# 3. 실시간 크롤러 및 스캐너 로직 (국내 테마/급등주 발굴단)
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

@st.cache_data(ttl=600)
def get_safe_kr_themes():
    """네이버 금융 거래상위에서 대형주를 제외한 실시간 유망 종목 추출"""
    tickers_dict = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # 코스피(sosok=0) 및 코스닥(sosok=1) 거래상위 둘 다 긁기
    for sosok in [0, 1]:
        try:
            url = f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}"
            res = requests.get(url, headers=headers)
            dfs = pd.read_html(res.text)
            df = dfs[1].dropna(subset=['종목명'])
            
            # 잡주/지수연동 품목 필터링
            df = df[~df['종목명'].str.contains('ETN|ETF|레버리지|인버스|스팩|금융투자|우|우B|고려|지수', na=False)]
            # 가격 필터 (동전주 제외 및 너무 무거운 주식 제외 -> 3천원 ~ 15만원 사이의 테마성 좋은 종목들)
            df = df[(df['현재가'] >= 3000) & (df['현재가'] <= 150000)]
            
            # 시가총액 초상위 대형주 강제 필터링 제거 (움직임 가벼운 중소형/테마 대장주 위주)
            super_heavy = ["삼성전자", "SK하이닉스", "현대차", "기아", "LG에너지솔루션", "삼성바이오로직스", "셀트리온", "POSCO홀딩스", "네이버", "카카오"]
            df = df[~df['종목명'].isin(super_heavy)]
            
            # 상위 10개씩 뽑아서 종목코드 매핑
            for _, row in df.head(10).iterrows():
                name = row['종목명']
                # 네이버 테이블 구조상 코드를 직접 파싱하기 까다로우므로 야후 파이낸스 검색 API로 티커 치환
                try:
                    search_url = f"https://query2.finance.yahoo.com/v1/finance/search?q={name}"
                    s_res = requests.get(search_url, headers=headers).json()
                    symbol = s_res['quotes'][0]['symbol']
                    if ".KQ" in symbol or ".KS" in symbol:
                        # 티커에서 6자리 숫자 코드 추출
                        code = symbol.split(".")[0]
                        tickers_dict[code] = name
                except: pass
        except: pass

    # 만약 크롤링이 일시적으로 막힐 때를 대비한 핫한 테마/성장주 베이스라인 셋업 (대형주 제외 버전)
    if not tickers_dict:
        tickers_dict = {
            "293490": "카카오게임즈", "066970": "엘앤에프", "271560": "오리온", 
            "035760": "CJ ENM", "036570": "엔씨소프트", "192820": "코스맥스",
            "403550": "Alcera", "253450": "스튜디오드래곤", "028300": "에이치엘비"
        }
    return tickers_dict

def get_safe_us_movers():
    return ["PLTR", "MSTR", "HOOD", "ASTS", "MARA", "RIOT", "UPST", "AFRM", "SOFI", "RIVN", "IONQ", "COIN"]

# 개별 자산 페칭 함수들
def fetch_us(stock):
    try:
        df = yf.Ticker(stock).history(period="3mo")
        if df.empty: return None
        score, current, rsi = calculate_swing_score(df)
        return {"ticker": stock, "종목": stock, "점수": score, "현재가": round(current, 2), "RSI": round(rsi, 1),
                "매수구간": f"${round(current * 0.96, 2)} ~ ${round(current, 2)}", "목표가": round(current * 1.07, 2), "손절가": round(current * 0.94, 2)}
    except: return None

def fetch_crypto(coin):
    try:
        df = pyupbit.get_ohlcv(coin, interval="day", count=40)
        if df is None or df.empty: return None
        score, current, rsi = calculate_swing_score(df)
        return {"ticker": None, "코인": coin.replace("KRW-", ""), "점수": score, "현재가": current, "RSI": round(rsi, 1),
                "매수구간": f"{current * 0.96:,.0f} ~ {current:,.0f}", "목표가": round(current * 1.08, 0), "손절가": round(current * 0.94, 0)}
    except: return None

def fetch_kr(item):
    code, name = item
    try:
        suffix = ".KS" if not code.startswith("3") and not code.startswith("2") and not code.startswith("0") else ".KQ"
        # 0으로 시작하는 코스피 종목 예외처리 조정
        if code in ["005930", "000660", "035420"]: suffix = ".KS"
        
        # 실제 등록된 시장 체크를 위해 양쪽 다 스캔 보완
        df = yf.Ticker(f"{code}.KS").history(period="3mo")
        if df.empty:
            df = yf.Ticker(f"{code}.KQ").history(period="3mo")
            suffix = ".KQ"
            
        if df.empty: return None
        score, current, rsi = calculate_swing_score(df)
        return {"ticker": f"{code}{suffix}", "종목": name, "점수": score, "현재가": int(current), "RSI": round(rsi, 1),
                "매수구간": f"{int(current * 0.96):,} ~ {int(current):,}", "목표가": int(current * 1.07), "손절가": int(current * 0.94)}
    except: return None

# ==========================================
# 4. 포트폴리오 전용 실시간 마켓 파인더 (해외주식 철벽 방어 완료)
# ==========================================
@st.cache_data(ttl=15)
def get_portfolio_market_data(name):
    name = name.strip().upper()
    
    # 1. 국내주식 판별 (6자리 숫자인 경우)
    if name.isdigit() and len(name) == 6:
        for suffix in [".KS", ".KQ"]:
            try:
                df = yf.Ticker(f"{name}{suffix}").history(period="1mo")
                if not df.empty and len(df) >= 5:
                    s, c, r = calculate_swing_score(df)
                    return f"{name}{suffix}", df["Close"].iloc[-1], s, r, "KRW", "Stock"
            except: continue

    # 2. 코인 판별 시도 (알파벳 2~5자리 자산 중 업비트에 실제 있는 경우)
    if name.isalpha() and 2 <= len(name) <= 5:
        try:
            df = pyupbit.get_ohlcv(f"KRW-{name}", interval="day", count=40)
            if df is not None and not df.empty:
                s, c, r = calculate_swing_score(df)
                return name, df["close"].iloc[-1], s, r, "KRW", "Crypto"
        except: pass

    # 3. 해외 주식 판별 (코인 조회에 실패했거나 미국 티커(VUZI, PLTR 등)인 경우 일로 빠짐)
    try:
        df = yf.Ticker(name).history(period="3mo")
        if not df.empty and len(df) >= 5:
            s, c, r = calculate_swing_score(df)
            currency = "KRW" if (".KS" in name or ".KQ" in name) else "USD"
            return name, df["Close"].iloc[-1], s, r, currency, "Stock"
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

# 풀 소스 데이터 로드
kr_live_dict = get_safe_kr_themes()
us_live_list = get_safe_us_movers()
try:
    coins_list = pyupbit.get_tickers(fiat="KRW")[:30]
except:
    coins_list = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-AVAX"]

# 멀티스레딩 고속 연산 스캔
with ThreadPoolExecutor(max_workers=20) as executor:
    us_top = sorted([r for r in executor.map(fetch_us, us_live_list) if r], key=lambda x: x["점수"], reverse=True)[:3]
    crypto_top = sorted([r for r in executor.map(fetch_crypto, coins_list) if r], key=lambda x: x["점수"], reverse=True)[:3]
    kr_top = sorted([r for r in executor.map(fetch_kr, kr_live_dict.items()) if r], key=lambda x: x["점수"], reverse=True)[:3]

# 스캐너 카드 대시보드 출력
for title, data, sym in [("🇺🇸 해외 알짜 성장주 TOP 3", us_top, "$"), ("🪙 코인 TOP 3", crypto_top, ""), ("🇰🇷 국내 테마 대장주 TOP 3", kr_top, "")]:
    st.header(title)
    if not data:
        st.warning("시장 데이터를 분석 레이어에 동기화 중입니다.")
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

# 자산 등록 양식
with st.form(key='portfolio_form', clear_on_submit=True):
    c1, c2, c3 = st.columns([2, 1, 1])
    n_in = c1.text_input("종목코드(예: 005930) / 티커(예: PLTR, VUZI, BTC)", placeholder="국내주식은 6자리 숫자, 해외주식/코인은 영문 티커 입력")
    b_in = c2.number_input("내 매수가", min_value=0.0, step=0.01, format="%.2f")
    if c3.form_submit_button("➕ 포트폴리오 추가"):
        if n_in:
            st.session_state.my_portfolio.append({"name": n_in.strip().upper(), "buy": float(b_in)})
            save_portfolio(st.session_state.my_portfolio)
            st.success(f"✅ {n_in} 등록 성공! 대시보드를 갱신합니다.")
            st.rerun()

# 포트폴리오 리스트 뷰어
if st.session_state.my_portfolio:
    for i, p in enumerate(st.session_state.my_portfolio):
        name, buy = p['name'], p['buy']
        stock_label, curr, score, rsi, currency, cat = get_portfolio_market_data(name)
        
        if curr == 0:
            st.error(f"⚠️ {name} 데이터를 가져오지 못했습니다. (티커 오타 또는 거래소 일시 통신 지연)")
            if st.button(f"❌ 목록에서 삭제", key=f"del_{i}"):
                st.session_state.my_portfolio.pop(i)
                save_portfolio(st.session_state.my_portfolio)
                st.rerun()
            continue
        
        profit = ((curr - buy) / buy * 100) if buy > 0 else 0
        sym = "$" if currency == "USD" else "₩"
        
        stop_rate = 0.08 if cat == "Crypto" else 0.06
        target_rate = 0.10 if cat == "Crypto" else 0.07
        
        st.markdown(f"### 📈 자산 대응 리포트: **{stock_label}**")
        
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("내 평단가", f"{sym}{buy:,.2f}")
        col_m2.metric("실시간 현재가", f"{sym}{curr:,.2f}")
        
        color_trend = "+" if profit >= 0 else ""
        col_m3.metric("실시간 수익률", f"{color_trend}{profit:.2f}%")
        
        st.caption(f"📊 스윙 스코어: **{score}점** | 현재 RSI 상태: **{rsi}**")
        
        df_guide = pd.DataFrame({
            "포지션 전략": ["현재가 스탠스", f"목표 익절가 (+{int(target_rate*100)}%)", f"리스크 손절가 (-{int(stop_rate*100)}%)"],
            "대응 가격 단가": [f"{sym}{curr:,.2f}", f"{sym}{curr*(1+target_rate):,.2f}", f"{sym}{curr*(1-stop_rate):,.2f}"]
        })
        st.table(df_guide)
        
        if st.button(f"🗑️ {name} 삭제", key=f"del_final_{i}"):
            st.session_state.my_portfolio.pop(i)
            save_portfolio(st.session_state.my_portfolio)
            st.rerun()
        st.markdown("<br>", unsafe_allow_html=True)
else:
    st.info("현재 등록된 관심 자산이 없습니다. 위 입력창에 등록해 보세요!")
