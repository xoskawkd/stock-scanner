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
# 2. 알짜배기 중형 테마주 자동 추출 함수 (잡주 차단)
# ==========================================

@st.cache_data(ttl=1800)
def get_safe_kr_themes():
    """초소형 잡주와 초우량주를 동시 차단하고, 짱짱한 알짜 테마주만 필터링"""
    tickers_dict = {}
    try:
        # 거래대금 상위 페이지를 기준으로 삼아 돈이 쏠린 곳을 먼저 봅니다.
        url = "https://finance.naver.com/sise/sise_quant.naver?sosok=1" # 코스닥 수급 풀
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers)
        dfs = pd.read_html(res.text)
        df = dfs[1]
        
        df = df.dropna(subset=['종목명'])
        
        # 🚫 안전장치 1: 리스크 덩어리(ETF, ETN, 스팩, 우선주) 무조건 제거
        df = df[~df['종목명'].str.contains('ETN|ETF|레버리지|인버스|스팩|제이티|금융투자|우|우B', na=False)]
        
        # 🚫 안전장치 2: 가격 필터링 (3,000원 이하 동전주/잡주는 쳐다보지도 않음)
        df = df[df['현재가'] >= 3000] 
        
        # 상위 알짜배기 종목 중 40개만 추출
        top_market = df.head(40)
        
        for _, row in top_market.iterrows():
            name = row['종목명']
            try:
                search_url = f"https://query2.finance.yahoo.com/v1/finance/search?q={name}"
                s_res = requests.get(search_url, headers=headers).json()
                symbol = s_res['quotes'][0]['symbol']
                
                if ".KQ" in symbol or ".KS" in symbol:
                    # 🚫 안전장치 3: 10조 원 이상 초대형주(삼전 등) 제외해서 회전율 극대화
                    if name in ["삼성전자", "SK하이닉스", "현대차", "기아", "LG에너지솔루션", "삼성바이오로직스", "셀트리온"]:
                        continue
                    tickers_dict[symbol] = name
            except:
                pass
    except:
        tickers_dict = {"036570.KS": "엔씨소프트", "066970.KQ": "엘앤에프", "293490.KQ": "카카오게임즈"}
    return tickers_dict

@st.cache_data(ttl=1800)
def get_safe_us_movers():
    """미국 주식 중 시총 1,000억 원 미만 잡주 제외, 기관 수급이 있는 알짜 중형 성장주만"""
    # 너무 가벼운 페니스탁(동전주)은 빼고, 확실한 비즈니스 모델이 있는 인기 성장주 리스트
    return [
        "PLTR", "MSTR", "HOOD", "ASTS", "MARA", "RIOT", "UPST", "AFRM", "SOFI", 
        "RIVN", "DKNG", "CELH", "IONQ", "COIN", "AI", "SQ", "RBLX", "U", "NET", "SNOW"
    ]

# ==========================================
# 3. 데이터 분석 및 핵심 스코어링 로직
# ==========================================

@st.cache_data(ttl=600)
def get_market_status():
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
    if len(df) < 25: return 0, 0, 0, 0, 0

    current = df["Close"].iloc[-1]
    rsi = RSIIndicator(df["Close"]).rsi().iloc[-1]
    ma10 = df["Close"].rolling(10).mean().iloc[-1]
    ma20 = df["Close"].rolling(20).mean().iloc[-1]
    volume_now = df["Volume"].iloc[-1]
    volume_avg = df["Volume"].rolling(20).mean().iloc[-1]

    score = 0

    # 무릎 자리 연산 (RSI 40~60 구간 가점)
    if 40 <= rsi <= 60: score += 40
    elif rsi < 40: score += 20

    if current > ma10: score += 20
    if current > ma20: score += 20

    # 찐 세력 수급 분석 (평균 거래량 대비 1.8배 돌파 시 가점)
    if volume_now > volume_avg * 1.8: score += 40

    return score, current, rsi


def fetch_ticker_news(ticker_symbol):
    news_list = []
    try:
        t = yf.Ticker(ticker_symbol)
        raw_news = t.news[:2]
        for item in raw_news:
            news_list.append({"title": item.get("title", "최신 뉴스"), "link": item.get("link", "#")})
    except:
        pass
    return news_list

# ==========================================
# 4. 시장별 TOP 3 실행 파트
# ==========================================

@st.cache_data(ttl=600)
def analyze_us_swing(stocks):
    results = []
    for stock in stocks:
        try:
            df = yf.Ticker(stock).history(period="3mo")
            score, current, rsi = calculate_swing_score(df)
            if current == 0: continue
            results.append({
                "ticker": stock, "종목": stock, "점수": score, "현재가": round(current, 2), "RSI": round(rsi, 1),
                "매수구간": f"${round(current * 0.96, 2)} ~ ${round(current, 2)}",
                "목표가": round(current * 1.07, 2), "손절가": round(current * 0.94, 2)
            })
        except:
            pass
    return sorted(results, key=lambda x: x["점수"], reverse=True)[:3]


@st.cache_data(ttl=300)
def analyze_crypto_swing():
    try: coins = pyupbit.get_tickers(fiat="KRW")
    except: return []

    results = []
    for coin in coins:
        try:
            df = pyupbit.get_ohlcv(coin, interval="day", count=40)
            if df is None: continue
            df = df.rename(columns={"close": "Close", "volume": "Volume"})
            score, current, rsi = calculate_swing_score(df, is_crypto=True)
            if current == 0: continue
            results.append({
                "ticker": None, "코인": coin.replace("KRW-", ""), "점수": score, "현재가": current, "RSI": round(rsi, 1),
                "매수구간": f"{current * 0.96:,.0f} ~ {current:,.0f}",
                "목표가": round(current * 1.08, 0), "손절가": round(current * 0.94, 0)
            })
        except:
            pass
    return sorted(results, key=lambda x: x["점수"], reverse=True)[:3]


@st.cache_data(ttl=600)
def analyze_kr_swing_yf(stocks_dict):
    results = []
    for ticker, name in stocks_dict.items():
        try:
            df = yf.Ticker(ticker).history(period="3mo")
            score, current, rsi = calculate_swing_score(df)
            if current == 0: continue
            results.append({
                "ticker": ticker, "종목": name, "점수": score, "현재가": int(current), "RSI": round(rsi, 1),
                "매수구간": f"{int(current * 0.96):,} ~ {int(current):,}",
                "목표가": int(current * 1.07), "손절가": int(current * 0.94)
            })
        except:
            pass
    return sorted(results, key=lambda x: x["점수"], reverse=True)[:3]

# 실행
kr_live_dict = get_safe_kr_themes()
us_live_list = get_safe_us_movers()

us_top = analyze_us_swing(us_live_list)
crypto_top = analyze_crypto_swing()
kr_top = analyze_kr_swing_yf(kr_live_dict)

# ==========================================
# 5. UI 메인 대시보드 렌더링
# ==========================================

fg_val, fg_txt, exchange = get_market_status()
st.sidebar.title("🛡️ Safety Theme Pulse")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.sidebar.caption("✅ 동전주 및 초대형 무거운 주식 필터링 완료")

st.title("🚀 Tae's Balanced Smart TOP 3 Scanner")
st.markdown("너무 무거운 우량주와 위험한 동전주를 모두 제외했습니다. **적당한 시가총액에 수급이 터진 알짜배기 대장 테마주**만 스캔합니다.")
st.divider()

for market_title, data, symbol in [
    ("🇺🇸 해외 알짜 성장주 TOP 3", us_top, "$"),
    ("🪙 가상화폐 알트코인 실시간 TOP 3", crypto_top, ""),
    ("🇰🇷 국내 검증된 테마 대장주 TOP 3", kr_top, ""),
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
                    * 📈 **목표가 (일당 익절)**: {symbol}{item['목표가']:,}
                    * 📉 **손절가 (리스크 관리)**: {symbol}{item['손절가']:,}
                    """
                )
                
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
        st.warning(f"{market_title} 조건에 맞는 종목을 필터링 중입니다.")
    st.divider()
