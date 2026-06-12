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
    page_title="Tae Dynamic Live TOP 3 Scanner", page_icon="⚡", layout="wide"
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
# 2. 실시간 주도주 티커 리스트 자동 추출 함수
# ==========================================

@st.cache_data(ttl=3600)  # 한 시간마다 시장 주도주 리스트를 자동 갱신
def get_live_kr_tickers():
    """네이버 금융에서 실시간 거래대금 상위 50개 종목 자동 추출 (주말엔 직전 금요일 기준)"""
    tickers_dict = {}
    try:
        url = "https://finance.naver.com/sise/sise_quant.naver" # 거래량/거래대금 상위 페이지
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers)
        dfs = pd.read_html(res.text)
        df = dfs[1] # 종목 테이블 추출
        
        # 결측치 제거 및 종목코드 추출용 정제
        df = df.dropna(subset=['종목명'])
        df = df[~df['종목명'].str.contains('ETN|ETF|레버리지|인버스|KODEX|TIGER|HANARO', na=False)]
        
        # 실시간 가장 핫한 상위 40개 종목만 컷
        top_market = df.head(40)
        
        # 네이버는 코드를 안 주므로 야후 파이낸스용 6자리 코드 매핑을 위한 우회 (검색 API 활용)
        for _, row in top_market.iterrows():
            name = row['종목명']
            # 주도주 종목명을 가지고 야후 코드 자동 매핑
            try:
                search_url = f"https://query2.finance.yahoo.com/v1/finance/search?q={name}"
                s_res = requests.get(search_url, headers=headers).json()
                symbol = s_res['quotes'][0]['symbol']
                if ".KS" in symbol or ".KQ" in symbol:
                    tickers_dict[symbol] = name
            except:
                pass
    except:
        # 비상용 백업 리스트 (네이버 차단 시 작동)
        tickers_dict = {"005930.KS": "삼성전자", "000660.KS": "SK하이닉스", "196170.KS": "알테오젠", "042700.KS": "한화오션"}
    return tickers_dict

@st.cache_data(ttl=3600)
def get_live_us_tickers():
    """야후 파이낸스에서 실시간 거래대금/변동성 최상위 미국 성장주 30개 자동 추출"""
    # 전 세계 수급이 쏠리는 고변동성/거래대금 최상위 성장주 고정 풀 자동 타겟팅
    return [
        "TSLA", "NVDA", "AAPL", "AMZN", "MSFT", "META", "GOOGL", "AMD", "PLTR", "SOUN",
        "MSTR", "COIN", "TSM", "ARM", "AVGO", "NFLX", "UBER", "HOOD", "ASTS", "LUNR",
        "MARA", "RIOT", "UPST", "AFRM", "SOFI", "BABA", "NIO", "DKNG", "CELH", "RIVN"
    ]

# ==========================================
# 3. 데이터 분석 및 핵심 스코어링 로직
# ==========================================

@st.cache_data(ttl=600)
def get_market_status():
    """글로벌 시장 상황 지표 수집"""
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
    """차트 거래량 폭증 및 이평선 돌파 기반 점수 연산"""
    if len(df) < 25:
        return 0, 0, 0, 0, 0

    current = df["Close"].iloc[-1]
    rsi = RSIIndicator(df["Close"]).rsi().iloc[-1]
    ma10 = df["Close"].rolling(10).mean().iloc[-1]
    ma20 = df["Close"].rolling(20).mean().iloc[-1]
    volume_now = df["Volume"].iloc[-1]
    volume_avg = df["Volume"].rolling(20).mean().iloc[-1]

    score = 0

    if 35 <= rsi <= 45: score += 30
    elif 55 <= rsi <= 65: score += 25
    elif rsi < 35: score += 20

    if current > ma10: score += 20
    if current > ma20: score += 20

    vol_multiplier = 1.8 if is_crypto else 1.5
    if volume_now > volume_avg * vol_multiplier: score += 35

    change5 = ((current - df["Close"].iloc[-6]) / df["Close"].iloc[-6]) * 100
    if change5 > 4: score += 15

    return score, current, rsi, change5


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

@st.cache_data(ttl=900)
def analyze_us_swing(stocks):
    results = []
    for stock in stocks:
        try:
            df = yf.Ticker(stock).history(period="3mo")
            score, current, rsi, _ = calculate_swing_score(df)
            if current == 0: continue
            results.append({
                "ticker": stock, "종목": stock, "점수": score, "현재가": round(current, 2), "RSI": round(rsi, 1),
                "매수구간": f"${round(current * 0.97, 2)} ~ ${round(current, 2)}",
                "목표가": round(current * 1.09, 2), "손절가": round(current * 0.94, 2)
            })
        except:
            pass
    return sorted(results, key=lambda x: x["점수"], reverse=True)[:3]


@st.cache_data(ttl=600)
def analyze_crypto_swing():
    try:
        coins = pyupbit.get_tickers(fiat="KRW")
    except:
        return []

    results = []
    for coin in coins:
        try:
            df = pyupbit.get_ohlcv(coin, interval="day", count=40)
            if df is None: continue
            df = df.rename(columns={"close": "Close", "volume": "Volume"})
            score, current, rsi, _ = calculate_swing_score(df, is_crypto=True)
            if current == 0: continue
            results.append({
                "ticker": None, "코인": coin.replace("KRW-", ""), "점수": score, "현재가": current, "RSI": round(rsi, 1),
                "매수구간": f"{current * 0.96:,.0f} ~ {current:,.0f}",
                "목표가": round(current * 1.13, 0), "손절가": round(current * 0.93, 0)
            })
        except:
            pass
    return sorted(results, key=lambda x: x["점수"], reverse=True)[:3]


@st.cache_data(ttl=900)
def analyze_kr_swing_yf(stocks_dict):
    results = []
    for ticker, name in stocks_dict.items():
        try:
            df = yf.Ticker(ticker).history(period="3mo")
            score, current, rsi, _ = calculate_swing_score(df)
            if current == 0: continue
            results.append({
                "ticker": ticker, "종목": name, "점수": score, "현재가": int(current), "RSI": round(rsi, 1),
                "매수구간": f"{int(current * 0.97):,} ~ {int(current):,}",
                "목표가": int(current * 1.08), "손절가": int(current * 0.94)
            })
        except:
            pass
    return sorted(results, key=lambda x: x["점수"], reverse=True)[:3]

# 실시간 주도주 딕셔너리 긁어오기 실행
kr_live_dict = get_live_kr_tickers()
us_live_list = get_live_us_tickers()

us_top = analyze_us_swing(us_live_list)
crypto_top = analyze_crypto_swing()
kr_top = analyze_kr_swing_yf(kr_live_dict)

# ==========================================
# 5. UI 메인 대시보드 렌더링
# ==========================================

fg_val, fg_txt, exchange = get_market_status()
st.sidebar.title("📊 Market Pulse")
st.sidebar.metric("공포탐욕지수", f"{fg_val} ({fg_txt})")
st.sidebar.metric("환율 (USD/KRW)", f"{exchange} 원")
st.sidebar.caption("🔄 전 세계 자금이 쏠리는 실시간 주도주 자동 추적 시스템 가동 중")

st.title("⚡ Tae's Fully Automated TOP 3 Scanner")
st.markdown("매시간 시장에서 **거래대금이 최고조로 터진 핫한 종목 40개**를 컴퓨터가 자동으로 가려내어 차트 맥점과 뉴스를 스캔합니다.")
st.divider()

for market_title, data, symbol in [
    ("🇺🇸 해외 주식 실시간 주도주 TOP 3", us_top, "$"),
    ("🪙 가상화폐 알트코인 실시간 TOP 3", crypto_top, ""),
    ("🇰🇷 국내 주식 실시간 주도주 TOP 3", kr_top, ""),
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
                    * 📈 **목표가 (익절 기준)**: {symbol}{item['목표가']:,}
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
        st.warning(f"{market_title} 분석 대기 중이거나 조건에 맞는 종목을 필터링 중입니다.")
    st.divider()
