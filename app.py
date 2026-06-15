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
# 2. 통합 핵심 분석 및 [정석 지지선] 타점 산출 엔진
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
# 3. 실시간 매싱 (거래량 필터 추가)
# ==========================================
def fetch_crypto(coin):
    try:
        df = pyupbit.get_ohlcv(coin, interval="day", count=40)
        if df is None or df.empty: return None
        
        # 🎯 [최종 수정] 거래대금 50억 미만인 유령 코인 필터링
        if (df['volume'].iloc[-1] * df['close'].iloc[-1]) < 5000000000:
            return None
            
        score, current, rsi, buy_min, buy_max, ma20 = calculate_swing_score_and_bands(df)
        if current == 0: return None
        return {"ticker": None, "코인": coin.replace("KRW-", ""), "점수": score, "현재가": current, "RSI": round(rsi, 1),
                "매수구간": f"{int(buy_min):,} ~ {int(buy_max):,}", "목표가": round(current * 1.08, 0), "손절가": round(min(buy_min*0.98, current * 0.94), 0)}
    except: return None

# (이하 기존 코드와 동일하게 하단부 붙여넣으시면 됩니다)
# (포트폴리오 관리 및 대시보드 로직 유지)
