"""
Tae Scanner v9 — 경량화 + 실전 최적화
코인 제거 / 폰 최적화 / KIS 우선 / 종목명 수정
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import json, os
from scipy import stats as sp
from datetime import datetime, timedelta
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import FinanceDataReader as fdr

try:
    from zoneinfo import ZoneInfo
except:
    ZoneInfo = None

# ============================================================
# API 키
# ============================================================
KRX_API_KEY    = "08810EEE8F724ED7BB7D35A2B79190956C2FFCB7"   # fallback
FINNHUB_API_KEY= "e196a49253d0408cadf883e01f6b78d9"
KIS_APP_KEY    = "PSmEd1aPpxC4GtQ5k23MW8iI4IdvwKRhnXiF"
KIS_APP_SECRET = "Pvmawb5cs8oIDi6KEgMbqx+115iKoUjKdMMj2DmcmdjyPmMtordm2EEfUoA+q15+23cUg2/7piYXimu+O42ZCS/tpJ2YpNAraf8W6TRV2cuwAgToJEWs8xBNHJeqFob6JUiVFhLbSGObuh1Z9ziXISrXBIF61+l/ZWoULdaIqAdYcjV2EIA="
KIS_IS_REAL    = True
KIS_BASE_URL   = "https://openapi.koreainvestment.com:9443" if KIS_IS_REAL else "https://openapivts.koreainvestment.com:29443"
_KIS_TOKEN     = {"token": "", "expires": None}

# ============================================================
# 설정
# ============================================================
KR_SCAN_N  = 300  # 코스피 상위
KQ_SCAN_N  = 200  # 코스닥 상위

THRESHOLDS = {
    "KR": {"min_vol":500_000,"max_rsi":83,"max_gain5":0.18,"max_ma20_dev":1.18,"max_hi60":0.98,"min_pass_score":38},
    "US": {"min_vol":500_000,"max_rsi":83,"max_gain5":0.18,"max_ma20_dev":1.18,"max_hi60":0.98,"min_pass_score":38},
}

# 가중치
DEFAULT_W = {
    "s1_strong":12,"s1_weak":6,"s2_strong":9,"s2_weak":5,
    "s2t_strong":5,"s2t_weak":3,"s3_strong":17,"s3_weak":10,
    "s4":12,"s5":3,"rsi_good":5,"rsi_oversold":3,"rsi_extreme":2,
    "min_pass_score":38,
}

def load_weights():
    if os.path.exists("weights.json"):
        try:
            w = json.load(open("weights.json","r",encoding="utf-8"))
            for k,v in DEFAULT_W.items():
                if k not in w: w[k]=v
            return w
        except: pass
    return DEFAULT_W.copy()

W = load_weights()

# ============================================================
# 포트폴리오 저장
# ============================================================
DATA_FILE = "portfolio.json"

def load_portfolio():
    if os.path.exists(DATA_FILE):
        try: return json.load(open(DATA_FILE,"r"))
        except: pass
    return []

def save_portfolio(data):
    json.dump(data, open(DATA_FILE,"w"), ensure_ascii=False)

# ============================================================
# KIS API
# ============================================================
def kis_token() -> str:
    global _KIS_TOKEN
    if not KIS_APP_KEY or not KIS_APP_SECRET: return ""
    now = datetime.now()
    if _KIS_TOKEN["token"] and _KIS_TOKEN["expires"] and now < _KIS_TOKEN["expires"]:
        return _KIS_TOKEN["token"]
    try:
        r = requests.post(f"{KIS_BASE_URL}/oauth2/tokenP",
            json={"grant_type":"client_credentials","appkey":KIS_APP_KEY,"appsecret":KIS_APP_SECRET},
            timeout=5).json()
        t = r.get("access_token","")
        if t:
            _KIS_TOKEN["token"] = t
            _KIS_TOKEN["expires"] = now + timedelta(hours=23)
        return t
    except: return ""

def kis_headers(tr_id):
    t = kis_token()
    if not t: return None
    return {"authorization":f"Bearer {t}","appkey":KIS_APP_KEY,"appsecret":KIS_APP_SECRET,"tr_id":tr_id}

@st.cache_data(ttl=30, show_spinner=False)
def kis_price(code: str) -> tuple:
    h = kis_headers("FHKST01010100")
    if not h: return 0.0, ""
    try:
        r = requests.get(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":code},
            headers=h, timeout=4).json()
        p = float(r.get("output",{}).get("stck_prpr",0) or 0)
        return p, "KIS"
    except: return 0.0, ""

@st.cache_data(ttl=30, show_spinner=False)
def kis_name(code: str) -> str:
    h = kis_headers("CTPF1002R")
    if not h: return ""
    try:
        r = requests.get(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/search-stock-info",
            params={"PRDT_TYPE_CD":"300","PDNO":code},
            headers=h, timeout=3).json()
        return r.get("output",{}).get("prdt_abrv_name","")
    except: return ""

@st.cache_data(ttl=1800, show_spinner=False)
def kis_investor(code: str) -> dict:
    h = kis_headers("FHKST01010900")
    if not h: return {}
    try:
        r = requests.get(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor",
            params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":code},
            headers=h, timeout=4).json()
        result = {"외국인":0,"기관":0,"개인":0,"연기금":0}
        for row in r.get("output",[]):
            inv = row.get("invst_nm","")
            try: net = int(row.get("netbuy_qty",0) or 0)
            except: net = 0
            if "외국인" in inv: result["외국인"] = net
            elif "기관계" in inv: result["기관"] = net
            elif "개인" in inv: result["개인"] = net
            elif "연기금" in inv: result["연기금"] = net
        return result
    except: return {}

@st.cache_data(ttl=1800, show_spinner=False)
def kis_investor_trend(code: str, days=5) -> list:
    h = kis_headers("FHKST01010600")
    if not h: return []
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now()-timedelta(days=days*2)).strftime("%Y%m%d")
        r = requests.get(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-investor",
            params={"fid_cond_mrkt_div_code":"J","fid_input_iscd":code,
                    "fid_begin_dt":start,"fid_end_dt":end},
            headers=h, timeout=4).json()
        trend = []
        for row in r.get("output",[])[:days]:
            try:
                trend.append({
                    "date": row.get("stck_bsop_date",""),
                    "외국인": int(row.get("frgn_ntby_qty",0) or 0),
                    "기관":   int(row.get("orgn_ntby_qty",0) or 0),
                    "개인":   int(row.get("prsn_ntby_qty",0) or 0),
                })
            except: pass
        return trend
    except: return []

def supply_signal(code: str) -> dict:
    trend = kis_investor_trend(code, 5)
    if not trend: return {"ok":False}
    today = trend[0]
    fore = today.get("외국인",0)
    inst = today.get("기관",0)
    pension = today.get("연기금",0) if "연기금" in today else 0

    fore_streak = sum(1 for t in trend if t.get("외국인",0)>0)
    inst_rev = len(trend)>=2 and trend[0].get("기관",0)>0 and trend[1].get("기관",0)<=0

    score = 0; signals = []
    if fore > 0:
        score += 2; signals.append(f"외국인 순매수")
        if fore_streak >= 3: score += 2; signals.append(f"{fore_streak}일 연속★")
    elif fore < 0: score -= 2; signals.append("외국인 순매도")
    if inst > 0:
        score += 2; signals.append("기관 순매수")
        if inst_rev: score += 2; signals.append("기관 전환★")
    elif inst < 0: score -= 1; signals.append("기관 순매도")

    if score >= 5: v,c = "🔥강한매수세","#10b981"
    elif score >= 3: v,c = "🟢매수세우위","#10b981"
    elif score >= 1: v,c = "🟡중립","#f59e0b"
    elif score <= -2: v,c = "🔴매도세","#ef4444"
    else: v,c = "⚪중립","#64748b"

    return {"ok":True,"verdict":v,"color":c,"score":score,
            "signals":signals,"fore":fore,"inst":inst,"streak":fore_streak}

# ============================================================
# 가격 조회
# ============================================================
@st.cache_data(ttl=30, show_spinner=False)
def kr_price(code: str) -> tuple:
    # KIS 우선 (실시간)
    if KIS_APP_KEY and KIS_APP_SECRET:
        p, src = kis_price(code)
        if p > 0: return p, src
    # KRX fallback
    # KRX fallback
    if KRX_API_KEY:
        try:
            d = datetime.now()
            for _ in range(5):
                ds = d.strftime("%Y%m%d")
                res = requests.get("http://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
                    params={"basDd":ds}, headers={"AUTH_KEY":KRX_API_KEY}, timeout=5).json()
                rows = res.get("OutBlock_1",[])
                if rows:
                    df = pd.DataFrame(rows)
                    row = df[df.get("ISU_SRT_CD","") == code] if "ISU_SRT_CD" in df.columns else pd.DataFrame()
                    if not row.empty:
                        p = float(str(row.iloc[0].get("TDD_CLSPRC","0")).replace(",",""))
                        if p > 0: return p, "KRX"
                d -= timedelta(days=1)
        except: pass
    # yfinance fallback
    try:
        sfx = ".KS" if code[:2] in ["00","01","02","03","04","05","06"] else ".KQ"
        t = yf.Ticker(f"{code}{sfx}")
        p = float(getattr(t.fast_info,"last_price",0) or 0)
        if p > 0: return p, "yfinance"
    except: pass
    return 0.0, "실패"

def is_us_open():
    if not ZoneInfo: return True
    try:
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5: return False
        return now.replace(hour=9,minute=30,second=0) <= now <= now.replace(hour=16,minute=0,second=0)
    except: return True

@st.cache_data(ttl=60, show_spinner=False)
def us_price(ticker: str) -> tuple:
    if FINNHUB_API_KEY:
        try:
            r = requests.get("https://finnhub.io/api/v1/quote",
                params={"symbol":ticker,"token":FINNHUB_API_KEY},timeout=3).json()
            c,pc = float(r.get("c",0) or 0), float(r.get("pc",0) or 0)
            if is_us_open() and c>0: return c,"Finnhub"
            if not is_us_open() and pc>0: return pc,"Finnhub(종가)"
            if c>0: return c,"Finnhub"
        except: pass
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="1d",interval="1m")
        if not df.empty:
            p = float(df["Close"].dropna().iloc[-1])
            if p>0: return p,"yfinance"
    except: pass
    return 0.0,"실패"

def us_prepost(ticker: str) -> tuple:
    try:
        t = yf.Ticker(ticker)
        reg = t.history(period="1d",interval="1m",prepost=False)
        reg_p = float(reg["Close"].dropna().iloc[-1]) if not reg.empty else 0
        pp = t.history(period="1d",interval="1m",prepost=True)
        if pp.empty: return 0,""
        pp_p = float(pp["Close"].dropna().iloc[-1])
        if not ZoneInfo: return pp_p,"장외"
        now = datetime.now(ZoneInfo("America/New_York"))
        o = now.replace(hour=9,minute=30,second=0)
        c = now.replace(hour=16,minute=0,second=0)
        pre = now.replace(hour=4,minute=0,second=0)
        aft = now.replace(hour=20,minute=0,second=0)
        if o<=now<=c: sess="🏛️정규장"
        elif pre<=now<o: sess="🌅프리마켓"
        elif c<now<=aft: sess="🌙애프터마켓"
        else: return 0,""
        diff = (pp_p-reg_p)/reg_p*100 if reg_p>0 else 0
        if abs(diff) < 0.1: return 0,""
        return pp_p, f"{sess} {diff:+.1f}%"
    except: return 0,""

# ============================================================
# OHLCV
# ============================================================
@st.cache_data(ttl=600, show_spinner=False)
def ohlcv_kr(code):
    try:
        df = fdr.DataReader(code, start="2024-01-01")
        if df is not None and len(df)>=60:
            df.columns=[c.lower() for c in df.columns]
            return df
    except: pass
    return None

@st.cache_data(ttl=600, show_spinner=False)
def ohlcv_us(ticker):
    try:
        df = yf.Ticker(ticker).history(period="1y")
        if not df.empty and len(df)>=60:
            df.columns=[c.lower() for c in df.columns]
            return df
    except: pass
    try:
        df = fdr.DataReader(ticker, start="2024-01-01")
        if df is not None and len(df)>=60:
            df.columns=[c.lower() for c in df.columns]
            return df
    except: pass
    return None

# ============================================================
# 종목 리스트
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def krx_listing():
    try:
        from pykrx import stock as pk
        today = datetime.now().strftime("%Y%m%d")
        rows=[]
        for mkt in ["KOSPI","KOSDAQ"]:
            for code in pk.get_market_ticker_list(today,market=mkt):
                try:
                    name = pk.get_market_ticker_name(code)
                    mc = pk.get_market_cap(today,today,code)
                    cap = int(mc["시가총액"].iloc[0]) if not mc.empty else 0
                    rows.append({"Code":code,"Name":name,"Marcap":cap,"Market":mkt})
                except: pass
        if rows: return pd.DataFrame(rows)
    except: pass
    try:
        df = fdr.StockListing("KRX")
        if df is not None and len(df)>0: return df
    except: pass
    # fallback
    data=[
        ("005930","삼성전자",400e12,"KOSPI"),("000660","SK하이닉스",120e12,"KOSPI"),
        ("207940","삼성바이오로직스",50e12,"KOSPI"),("373220","LG에너지솔루션",60e12,"KOSPI"),
        ("035420","NAVER",30e12,"KOSPI"),("005380","현대차",40e12,"KOSPI"),
        ("000270","기아",30e12,"KOSPI"),("105560","KB금융",15e12,"KOSPI"),
        ("055550","신한지주",12e12,"KOSPI"),("086790","하나금융지주",10e12,"KOSPI"),
        ("068270","셀트리온",10e12,"KOSPI"),("006400","삼성SDI",15e12,"KOSPI"),
        ("329180","HD현대중공업",8e12,"KOSPI"),("042700","한미반도체",8e12,"KOSPI"),
        ("051910","LG화학",20e12,"KOSPI"),("035720","카카오",15e12,"KOSPI"),
        ("003550","LG",10e12,"KOSPI"),("096770","SK이노베이션",7e12,"KOSPI"),
        ("010130","고려아연",8e12,"KOSPI"),("009150","삼성전기",7e12,"KOSPI"),
        ("247540","에코프로비엠",8e12,"KOSDAQ"),("086520","에코프로",6e12,"KOSDAQ"),
        ("196170","알테오젠",3e12,"KOSDAQ"),("091990","셀트리온헬스케어",3e12,"KOSDAQ"),
        ("036570","엔씨소프트",3e12,"KOSDAQ"),("263750","펄어비스",1e12,"KOSDAQ"),
        ("039030","이오테크닉스",1e12,"KOSDAQ"),("214150","클래시스",1e12,"KOSDAQ"),
        ("277810","레인보우로보틱스",1e12,"KOSDAQ"),("357780","솔브레인",2e12,"KOSDAQ"),
    ]
    return pd.DataFrame(data, columns=["Code","Name","Marcap","Market"])

@st.cache_data(ttl=86400, show_spinner=False)
def us_tickers():
    tickers=[]
    for url, id_attr in [
        ("https://en.wikipedia.org/wiki/Nasdaq-100", "constituents"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", None),
    ]:
        try:
            kw = {"attrs":{"id":id_attr}} if id_attr else {}
            tables = pd.read_html(url,**kw)
            if tables:
                df = tables[0]
                col = next((c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower()),None)
                if col:
                    tickers.extend([t.replace(".","-") for t in df[col].dropna()
                                    if isinstance(t,str) and len(t)<=6])
        except: pass
    seen=set(); unique=[]
    for t in tickers:
        if t not in seen: seen.add(t); unique.append(t)
    if len(unique)>=100: return unique[:500]
    return ["NVDA","META","GOOGL","AMZN","MSFT","AMD","TSLA","AAPL","NFLX","AVGO",
            "PLTR","CRM","SNOW","DDOG","NET","CRWD","PANW","PYPL","SOFI","COIN",
            "HIMS","AXSM","MRNA","ASTS","RIVN","JPM","BAC","GS","V","MA"]

US_LIST = us_tickers()

# ============================================================
# 퀀트 예측 엔진
# ============================================================
def _sf(v, d=0.0):
    try: r=float(v); return r if np.isfinite(r) else d
    except: return d

def quant_predict(df, market="KR"):
    OUT={"score":0,"grade":"C","signals":[],"pass":False,
         "buy_min":0.0,"buy_max":0.0,"target":0.0,"stop":0.0,
         "rsi":50.0,"current":0.0,"s1":False,"s2":False,"s3":False,"s4":False,"s5":False}
    th=THRESHOLDS.get(market,THRESHOLDS["KR"])
    try:
        if df is None or len(df)<60: return OUT
        df=df.copy(); df.columns=[c.lower() for c in df.columns]
        cl=df["close"].astype(float); hi=df["high"].astype(float)
        lo=df["low"].astype(float);   vo=df["volume"].astype(float)
        cur=_sf(cl.iloc[-1]); OUT["current"]=cur
        if cur<=0: return OUT

        rejected=False
        avg_vol=_sf(vo.rolling(20).mean().iloc[-1])
        if avg_vol<th["min_vol"]:
            OUT["signals"].append("❌ 유동성 부족"); rejected=True
        ma20=_sf(cl.rolling(20).mean().iloc[-1])
        if ma20>0 and cur>ma20*th["max_ma20_dev"]:
            OUT["signals"].append("❌ 이미 급등"); rejected=True
        p5=_sf(cl.iloc[-6]) if len(cl)>=6 else cur
        if p5>0 and (cur-p5)/p5>th["max_gain5"]:
            OUT["signals"].append("❌ 5일 급등"); rejected=True
        hi60=_sf(cl.rolling(60).max().iloc[-1])
        if hi60>0 and cur>=hi60*th["max_hi60"]:
            OUT["signals"].append("❌ 60일 고점권"); rejected=True

        ma5 =_sf(cl.rolling(5).mean().iloc[-1])
        ma60=_sf(cl.rolling(60).mean().iloc[-1])
        delta=cl.diff(); gain=delta.clip(lower=0).rolling(14).mean()
        loss=(-delta.clip(upper=0)).rolling(14).mean()
        rsi_s=100-100/(1+gain/loss.replace(0,np.nan))
        rsi=_sf(rsi_s.iloc[-1],50.0); OUT["rsi"]=rsi
        if rsi>th["max_rsi"]:
            OUT["signals"].append(f"❌ RSI 과열 ({rsi:.0f})"); rejected=True

        score=0; setup=0; strong=0; trigger=0

        # S1 BB수축
        bbw=(cl.rolling(20).std()*2)/cl.rolling(20).mean().replace(0,np.nan)
        bw=_sf(bbw.iloc[-1]); bwavg=_sf(bbw.rolling(20).mean().iloc[-1])
        s1=False
        if bwavg>0 and bw>0:
            if bw<bwavg*0.80: s1=True;setup+=1;strong+=1;score+=W["s1_strong"]; OUT["signals"].append("✅ [S1] BB강수축")
            elif bw<bwavg*0.92: s1=True;setup+=1;score+=W["s1_weak"]; OUT["signals"].append("🔶 [S1] BB수축")
            else: OUT["signals"].append("⬜ [S1] BB수축없음")
        OUT["s1"]=s1

        # S2 거래량눌림
        vm5=_sf(vo.rolling(5).mean().iloc[-1]); vm20=_sf(vo.rolling(20).mean().iloc[-1])
        vol_now=_sf(vo.iloc[-1]); s2=False
        if vm20>0:
            if vm5<vm20*0.65: s2=True;setup+=1;strong+=1;score+=W["s2_strong"]; OUT["signals"].append(f"✅ [S2] 거래량강눌림 ({vm5/vm20*100:.0f}%)")
            elif vm5<vm20*0.80: s2=True;setup+=1;score+=W["s2_weak"]; OUT["signals"].append(f"🔶 [S2] 거래량눌림 ({vm5/vm20*100:.0f}%)")
            else: OUT["signals"].append(f"⬜ [S2] 거래량눌림없음 ({vm5/vm20*100:.0f}%)")
        if vm5>0:
            if vol_now>vm5*2.0: trigger+=1;score+=W["s2t_strong"]; OUT["signals"].append(f"➕ [S2T] 거래량폭발")
            elif vol_now>vm5*1.5: trigger+=1;score+=W["s2t_weak"]; OUT["signals"].append(f"➕ [S2T] 거래량증가")
        OUT["s2"]=s2

        # S3 정배열+눌림목
        aligned=ma5>0 and ma20>0 and ma60>0 and ma5>ma20>ma60
        mid_up=ma20>0 and ma60>0 and ma20>ma60
        near=ma20>0 and abs(cur-ma20)/ma20<=0.05; s3=False
        if aligned and near: s3=True;setup+=1;strong+=1;score+=W["s3_strong"]; OUT["signals"].append("✅ [S3] 정배열+눌림목 ★")
        elif mid_up and near: s3=True;setup+=1;score+=W["s3_weak"]; OUT["signals"].append("🔶 [S3] 중기상승+눌림목")
        else: OUT["signals"].append(f"⬜ [S3] 눌림목없음 (이격 {abs(cur-ma20)/ma20*100:.1f}%)")
        OUT["s3"]=s3

        # S4 RSI다이버전스
        s4=False
        try:
            if len(cl)>=20:
                pw=cl.iloc[-20:]; rw=rsi_s.iloc[-20:]
                i1=pw.iloc[:10].idxmin(); i2=pw.iloc[10:].idxmin()
                p1=_sf(pw.loc[i1]); p2=_sf(pw.loc[i2])
                r1=_sf(rw.loc[i1],50); r2=_sf(rw.loc[i2],50)
                s4=(p2<p1) and (r2>r1+3)
                if s4: trigger+=1;score+=W["s4"]; OUT["signals"].append("✅ [S4] RSI다이버전스 ★")
                else: OUT["signals"].append("⬜ [S4] 다이버전스없음")
        except: OUT["signals"].append("⬜ [S4] 계산실패")
        OUT["s4"]=s4

        # S5 캔들
        s5=False
        try:
            if "open" in df.columns and len(df)>=2:
                op=df["open"].astype(float)
                o1,c1=_sf(op.iloc[-1]),_sf(cl.iloc[-1])
                h1,l1=_sf(hi.iloc[-1]),_sf(lo.iloc[-1])
                o2,c2=_sf(op.iloc[-2]),_sf(cl.iloc[-2])
                if all(v>0 for v in [o1,c1,h1,l1,o2,c2]):
                    body=abs(c1-o1); lower=min(o1,c1)-l1; upper=h1-max(o1,c1)
                    hammer=body>0 and lower>body*2 and upper<body*0.5
                    bull=c2<o2 and c1>o1
                    s5=hammer or bull
                    if s5: trigger+=1;score+=W["s5"]; OUT["signals"].append(f"✅ [S5] {'망치형' if hammer else '양봉전환'}")
                    else: OUT["signals"].append("⬜ [S5] 캔들없음")
        except: OUT["signals"].append("⬜ [S5] 캔들실패")
        OUT["s5"]=s5

        # RSI 보너스
        if 40<=rsi<=55: score+=W["rsi_good"]; OUT["signals"].append(f"✅ RSI 매수구간 ({rsi:.0f})")
        elif 30<=rsi<40: score+=W["rsi_oversold"]; OUT["signals"].append(f"🔶 RSI 과매도 ({rsi:.0f})")
        elif rsi<30: score+=W["rsi_extreme"]; OUT["signals"].append(f"🔶 RSI 극과매도 ({rsi:.0f})")
        else: OUT["signals"].append(f"⬜ RSI 보너스없음 ({rsi:.0f})")

        # ATR 목표가/손절가
        _tgt=cur*1.08; _stp=cur*0.93; _blo=cur*0.97; _bhi=cur*1.02
        try:
            tr=pd.concat([hi-lo,(hi-cl.shift()).abs(),(lo-cl.shift()).abs()],axis=1).max(axis=1)
            atr=_sf(tr.rolling(14).mean().iloc[-1])
            if atr>0:
                std20=_sf(cl.rolling(20).std().iloc[-1])
                bb_top=ma20+std20*2 if ma20>0 and std20>0 else 0
                t_atr=cur+atr*2; t_bb=bb_top if bb_top>cur else cur+atr*2
                _tgt=min(t_atr,t_bb)
                _tgt=max(_tgt,cur*1.05); _tgt=min(_tgt,cur*1.15)
                _stp=cur-atr*1.5; _stp=max(_stp,cur*0.93); _stp=min(_stp,cur*0.97)
                _blo=max(cur*0.97,cur-atr*0.5); _bhi=min(cur*1.02,cur+atr*0.3)
        except: pass
        OUT["target"]=round(_tgt,4); OUT["stop"]=round(_stp,4)
        OUT["buy_min"]=round(_blo,4); OUT["buy_max"]=round(_bhi,4)
        OUT["score"]=int(score)

        # 통과 게이트
        # S3 AND S4 → 너무 적음 → S3 OR S4 + 점수 높임
        core = (s3 and s4) or (s3 and score >= 45) or (s4 and score >= 45)
        OUT["pass"]=(not rejected) and core and (score>=W["min_pass_score"])

        # 등급
        if score>=65 and strong>=1 and trigger>=1: g="A+"
        elif score>=55 and setup>=1 and trigger>=1: g="A"
        elif score>=45 and setup>=1: g="B+"
        elif score>=38 and setup>=1: g="B"
        else: g="C"
        OUT["grade"]=g

    except Exception as e:
        OUT["signals"].append(f"오류:{e}")
    return OUT

# ============================================================
# 스캐너
# ============================================================
@st.cache_data(ttl=300, show_spinner=False)
def scan_kr():
    listing = krx_listing()
    kospi = listing[listing["Market"].str.contains("KOSPI",na=False)] if "Market" in listing.columns else listing
    kosdaq = listing[listing["Market"].str.contains("KOSDAQ",na=False)] if "Market" in listing.columns else pd.DataFrame()
    kp = kospi[kospi["Marcap"]>3e11].nlargest(KR_SCAN_N,"Marcap")
    kq = kosdaq[kosdaq["Marcap"]>5e10].nlargest(KQ_SCAN_N,"Marcap") if not kosdaq.empty else pd.DataFrame()
    targets = pd.concat([kp,kq]).drop_duplicates("Code")
    codes = list(zip(targets["Code"],targets["Name"]))

    def _fetch(item):
        code,name = item
        df = ohlcv_kr(code)
        if df is None: return {"_skip":True,"why":"데이터없음"}
        r = quant_predict(df,"KR")
        if not r["pass"]:
            why=next((s for s in r["signals"] if "❌" in s),"조건미충족")
            return {"_skip":True,"why":why}
        p, src = kr_price(code)
        if p<=0: p=r["current"]
        tgt = int(r["target"]) if r["target"]>p*1.03 and r["target"]<=p*1.15 else int(p*1.08)
        stp = int(r["stop"])   if r["stop"]>p*0.85  and r["stop"]<p*0.98  else int(p*0.93)
        return {"_skip":False,"종목":name,"코드":code,"등급":r["grade"],"점수":r["score"],
                "현재가":int(p),"RSI":round(r["rsi"],1),
                "매수구간":f"₩{int(p*0.97):,}~₩{int(p*1.02):,}",
                "목표가":tgt,"손절가":stp,"signals":r["signals"],"source":src,
                "s_flags":[r["s1"],r["s2"],r["s3"],r["s4"],r["s5"]]}

    with ThreadPoolExecutor(max_workers=30) as ex:
        raw=list(ex.map(_fetch,codes))
    skips=[r for r in raw if r.get("_skip")]
    top5=sorted([r for r in raw if not r.get("_skip")],key=lambda x:x["점수"],reverse=True)[:5]
    return top5, skips

@st.cache_data(ttl=300, show_spinner=False)
def scan_us():
    rt = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs={t:ex.submit(us_price,t) for t in US_LIST}
        for t,f in futs.items():
            try: rt[t]=f.result()
            except: rt[t]=(0.0,"실패")

    def _fetch(ticker):
        df=ohlcv_us(ticker)
        if df is None: return {"_skip":True,"why":"데이터없음"}
        r=quant_predict(df,"US")
        if not r["pass"]:
            why=next((s for s in r["signals"] if "❌" in s),"조건미충족")
            return {"_skip":True,"why":why}
        p,src=rt.get(ticker,(0.0,"없음"))
        if p<=0: p,src=us_price(ticker)
        if p<=0: p=r["current"]
        def uf(v): return f"${v:,.4f}" if v<1 else f"${v:,.3f}" if v<10 else f"${v:,.2f}"
        tgt=round(r["target"],2) if r["target"]>p*1.03 and r["target"]<=p*1.15 else round(p*1.08,2)
        stp=round(r["stop"],2)   if r["stop"]>p*0.85  and r["stop"]<p*0.98  else round(p*0.93,2)
        return {"_skip":False,"종목":ticker,"등급":r["grade"],"점수":r["score"],
                "현재가":round(p,2),"RSI":round(r["rsi"],1),
                "매수구간":f"{uf(p*0.97)}~{uf(p*1.02)}",
                "목표가":tgt,"손절가":stp,"signals":r["signals"],"source":src,
                "s_flags":[r["s1"],r["s2"],r["s3"],r["s4"],r["s5"]]}

    with ThreadPoolExecutor(max_workers=40) as ex:
        raw=list(ex.map(_fetch,US_LIST))
    skips=[r for r in raw if r.get("_skip")]
    top5=sorted([r for r in raw if not r.get("_skip")],key=lambda x:x["점수"],reverse=True)[:5]
    return top5, skips

# ============================================================
# 포트폴리오 데이터
# ============================================================
def get_stock_name(code: str) -> str:
    """종목명 조회 — KIS → listing"""
    if KIS_APP_KEY:
        n = kis_name(code)
        if n: return n
    try:
        listing = krx_listing()
        row = listing[listing["Code"]==code]
        if not row.empty: return row["Name"].values[0]
    except: pass
    return code

def portfolio_data(name: str) -> dict:
    FAIL = {"label":None,"curr":0,"score":0,"grade":"F","rsi":0,
            "currency":"KRW","stop":0,"target":0,"buy_min":0,"buy_max":0,
            "source":"실패","ok":False,"signals":[]}

    if name.isdigit() and len(name)==6:
        p, src = kr_price(name)
        df = ohlcv_kr(name)
        stock_name = get_stock_name(name)
        # 종목명이 코드와 같으면 KIS/listing 재시도
        if stock_name == name:
            stock_name = get_stock_name(name)
        label = f"{stock_name} ({name})" if stock_name and stock_name != name else name

        if df is not None:
            r = quant_predict(df,"KR")
            curr = p if p>0 else r["current"]
            if curr <= 0: return FAIL
            tgt = int(r["target"]) if r["target"]>curr*1.03 and r["target"]<=curr*1.15 else int(curr*1.08)
            stp = int(r["stop"])   if r["stop"]>curr*0.85  and r["stop"]<curr*0.98  else int(curr*0.93)
            return {"label":label,"curr":curr,"score":r["score"],"grade":r["grade"],
                    "rsi":round(r["rsi"],1),"currency":"KRW","stop":stp,"target":tgt,
                    "buy_min":int(curr*0.97),"buy_max":int(curr*1.02),
                    "source":src,"ok":curr>0,"signals":r["signals"]}
        if p>0:
            return {"label":label,"curr":p,"score":0,"grade":"-","rsi":50,"currency":"KRW",
                    "stop":int(p*0.93),"target":int(p*1.08),
                    "buy_min":int(p*0.97),"buy_max":int(p*1.02),
                    "source":src,"ok":True,"signals":[]}
        return FAIL

    # 해외
    p, src = us_price(name)
    pp, pp_label = us_prepost(name)
    df = ohlcv_us(name)
    def ur(v): return round(v,4) if v<1 else round(v,3) if v<10 else round(v,2)

    if df is not None:
        r = quant_predict(df,"US")
        curr = p if p>0 else r["current"]
        if curr<=0: return FAIL
        tgt=ur(r["target"]) if r["target"]>curr*1.03 and r["target"]<=curr*1.15 else ur(curr*1.08)
        stp=ur(r["stop"])   if r["stop"]>curr*0.85  and r["stop"]<curr*0.98  else ur(curr*0.93)
        return {"label":f"{name} ({src})","curr":ur(curr),"score":r["score"],"grade":r["grade"],
                "rsi":round(r["rsi"],1),"currency":"USD","stop":stp,"target":tgt,
                "buy_min":ur(curr*0.97),"buy_max":ur(curr*1.02),
                "source":src,"ok":curr>0,"signals":r["signals"],
                "prepost":pp,"prepost_label":pp_label}
    if p>0:
        return {"label":f"{name} ({src})","curr":ur(p),"score":0,"grade":"-","rsi":50,
                "currency":"USD","stop":ur(p*0.93),"target":ur(p*1.08),
                "buy_min":ur(p*0.97),"buy_max":ur(p*1.02),
                "source":src,"ok":True,"signals":[],"prepost":pp,"prepost_label":pp_label}
    return FAIL

# ============================================================
# UI 시작
# ============================================================
st.set_page_config(page_title="Tae Scanner v9", layout="wide")
if "portfolio" not in st.session_state:
    st.session_state.portfolio = load_portfolio()
if "my_portfolio" in st.session_state:
    # 구버전 마이그레이션
    st.session_state.portfolio = st.session_state.my_portfolio
    del st.session_state["my_portfolio"]

# 사이드바
st.sidebar.title("🛡️ Tae Scanner v9")
with st.sidebar.expander("🔑 API 상태"):
    st.write("KIS:",  "✅" if KIS_APP_KEY  else "❌")
    st.write("KRX:",  "✅" if KRX_API_KEY  else "❌ (fallback)")
    st.write("Finnhub:", "✅" if FINNHUB_API_KEY else "❌")

try:
    fg = requests.get("https://api.alternative.me/fng/?limit=1",timeout=3).json()
    fgv = fg["data"][0]["value"]
    fgt = "극탐욕" if int(fgv)>=75 else "탐욕" if int(fgv)>=60 else "중립" if int(fgv)>=40 else "공포" if int(fgv)>=25 else "극공포"
    st.sidebar.metric("공포탐욕", f"{fgv} ({fgt})")
except: pass
st.sidebar.metric("🇺🇸 미국장", "OPEN" if is_us_open() else "CLOSED")

# 재스캔
col_scan, _ = st.sidebar.columns([1,1])
if col_scan.button("🔄 재스캔"):
    scan_kr.clear(); scan_us.clear()
    ohlcv_kr.clear(); ohlcv_us.clear()
    st.rerun()

st.title("🚀 Tae Scanner v9")
st.caption("S3 정배열눌림목 AND S4 RSI다이버전스 — 코스피+코스닥+나스닥+S&P500")

# ============================================================
# 자산관리
# ============================================================
st.header("💼 자산관리")

tab_watch, tab_hold = st.tabs(["👀 관심종목", "💰 보유종목"])

with tab_watch:
    st.caption("스캐너 추천 종목 등록 → 다음날 매수 여부 자동 판단")
    with st.form("watch_form", clear_on_submit=True):
        w1,w2,w3 = st.columns([2,2,1])
        wn = w1.text_input("종목코드", placeholder="005930 / AAPL")
        wm = w2.text_input("메모", placeholder="스캐너 추천, 관심 이유")
        if w3.form_submit_button("👀 추가"):
            if wn:
                nm = wn.strip().upper()
                if not any(p["name"]==nm for p in st.session_state.portfolio):
                    st.session_state.portfolio.append({"name":nm,"buy":0.0,"date":"","type":"watch","memo":wm.strip()})
                    save_portfolio(st.session_state.portfolio)
                    st.rerun()

with tab_hold:
    with st.form("hold_form", clear_on_submit=True):
        c1,c2,c3,c4 = st.columns([2,1,1,1])
        hn = c1.text_input("종목코드", placeholder="005930 / AAPL")
        hb = c2.number_input("평단가", min_value=0.0, step=0.01, format="%.4f")
        hd = c3.text_input("매수일자", placeholder="2024-01-15")
        if c4.form_submit_button("➕ 추가"):
            if hn and hb>0:
                nm = hn.strip().upper()
                # 관심→보유 업그레이드
                upgraded = False
                for p in st.session_state.portfolio:
                    if p["name"]==nm and p.get("type")=="watch":
                        p.update({"buy":float(hb),"date":hd.strip(),"type":"hold"})
                        upgraded = True; break
                if not upgraded:
                    st.session_state.portfolio.append({"name":nm,"buy":float(hb),"date":hd.strip(),"type":"hold","memo":""})
                save_portfolio(st.session_state.portfolio)
                st.rerun()

# 포트폴리오 카드
to_remove = None
for i, p in enumerate(st.session_state.portfolio):
    name = p["name"]; buy = p.get("buy",0); ptype = p.get("type","hold")
    d = portfolio_data(name)
    if not d["ok"] or d["curr"]<=0:
        st.error(f"⚠️ {name} 조회 실패")
        if st.button(f"❌ 삭제", key=f"del_err_{i}"): to_remove=i
        continue

    curr = d["curr"]; is_kr = d["currency"]=="KRW"
    profit = (curr-buy)/buy*100 if buy>0 else 0
    def uf(v): return f"${v:,.4f}" if v<1 else f"${v:,.3f}" if v<10 else f"${v:,.2f}"
    fmt = (lambda v: f"₩{int(v):,}") if is_kr else uf
    gc = {"A+":"#f59e0b","A":"#10b981","B+":"#3b82f6","B":"#94a3b8","C":"#64748b"}.get(d["grade"],"#64748b")
    sigs = d.get("signals",[])
    s3_on = any("S3" in s and "✅" in s for s in sigs)
    s4_on = any("S4" in s and "✅" in s for s in sigs)

    # ── 관심종목 카드 ──
    if ptype == "watch":
        # 갭 계산
        gap_pct = 0.0
        try:
            df_tmp = ohlcv_kr(name) if is_kr else ohlcv_us(name)
            if df_tmp is not None and len(df_tmp)>=2:
                prev = float(df_tmp["close"].iloc[-2])
                if prev>0: gap_pct = (curr-prev)/prev*100
        except: pass

        if abs(gap_pct)<3: gv="🟢 갭 양호"; gc2="#10b981"; gd=f"갭 {gap_pct:+.1f}% — 매수 검토"
        elif abs(gap_pct)<5: gv="🟡 소폭 갭"; gc2="#f59e0b"; gd=f"갭 {gap_pct:+.1f}% — 눌림 기다려"
        else: gv="🔴 갭 과다"; gc2="#ef4444"; gd=f"갭 {gap_pct:+.1f}% — 추격 위험"

        signal_ok = s3_on and s4_on

        # 수급
        sup_html=""
        if is_kr and KIS_APP_KEY:
            sup = supply_signal(name)
            if sup.get("ok"):
                sup_sigs = " / ".join(sup.get("signals",[])[:3])
                sup_html = f'<div style="background:#0f172a;padding:8px;border-radius:6px;margin:6px 0;font-size:12px;">수급: <span style="color:{sup["color"]};font-weight:bold;">{sup["verdict"]}</span> <span style="color:#64748b;">{sup_sigs}</span></div>'

        # 종합 판단
        bs = 0
        if abs(gap_pct)<3: bs+=2
        elif abs(gap_pct)<5: bs+=1
        if signal_ok: bs+=2
        if is_kr and KIS_APP_KEY:
            sup2 = supply_signal(name)
            if sup2.get("ok") and sup2.get("score",0)>=3: bs+=2
        if bs>=5: bv="🟢 매수 적극 고려"; bc="#10b981"
        elif bs>=3: bv="🟡 조건부 매수"; bc="#f59e0b"
        else: bv="🔴 매수 보류"; bc="#ef4444"

        memo = p.get("memo","")
        st.markdown(f"""
<div style="background:#1e293b;padding:14px;border-radius:10px;border-left:5px solid {gc};margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <b>👀 {d['label']}</b>
    <span style="background:{gc};color:#000;font-size:11px;padding:2px 6px;border-radius:4px;">{d['grade']} {d['score']}점</span>
  </div>
  {f'<div style="font-size:11px;color:#64748b;margin-top:4px;">📝 {memo}</div>' if memo else ''}
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px;">
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">현재가</div>
      <div style="font-weight:bold;">{fmt(curr)}</div>
    </div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">갭</div>
      <div style="color:{'#10b981' if gap_pct>=0 else '#ef4444'};font-weight:bold;">{gap_pct:+.1f}%</div>
    </div>
  </div>
  <div style="background:#0f172a;padding:8px;border-radius:6px;margin-top:6px;font-size:12px;">
    <span style="color:{gc2};font-weight:bold;">{gv}</span>
    <span style="color:#64748b;margin-left:8px;">{gd}</span>
  </div>
  <div style="background:#0f172a;padding:8px;border-radius:6px;margin-top:6px;font-size:12px;">
    신호: <span style="font-weight:bold;">{'✅ S3+S4 유지' if signal_ok else ('⚠️ S3만' if s3_on else '❌ 신호소멸')}</span>
  </div>
  {sup_html}
  <div style="background:#0f172a;padding:10px;border-radius:6px;margin-top:6px;border-left:3px solid {bc};">
    <span style="font-size:13px;font-weight:bold;color:{bc};">{bv}</span>
  </div>
  <div style="font-size:10px;color:#475569;margin-top:6px;">📡 {d['source']}</div>
</div>""", unsafe_allow_html=True)

        col_b, col_d = st.columns([2,1])
        if col_b.button("➕ 매수 확정", key=f"buy_{i}"):
            st.session_state[f"bc_{i}"] = True
        if st.session_state.get(f"bc_{i}"):
            bp = st.number_input("평단가", min_value=0.0, step=0.01, format="%.4f", key=f"bp_{i}")
            bd = st.text_input("매수일자", value=datetime.now().strftime("%Y-%m-%d"), key=f"bd_{i}")
            if st.button("✅ 확정", key=f"bok_{i}") and bp>0:
                p["buy"]=float(bp); p["date"]=bd; p["type"]="hold"
                st.session_state[f"bc_{i}"]=False
                save_portfolio(st.session_state.portfolio)
                st.rerun()
        if col_d.button("🗑️", key=f"dw_{i}"): to_remove=i
        continue

    # ── 보유종목 카드 ──
    # 보유일수
    hold_days=0
    if p.get("date"):
        try: hold_days=(datetime.now()-datetime.strptime(p["date"],"%Y-%m-%d")).days
        except: pass

    fixed_stop  = buy*0.93
    fixed_tgt   = buy*1.08
    rsi_v = d["rsi"]
    trend_broken = not s3_on

    if curr<=fixed_stop: act="🔴 즉시 손절"; ac="#ef4444"; ar=f"평단 -7% 이탈 ({profit:.1f}%)"
    elif trend_broken and profit<-3: act="🔴 손절 고려"; ac="#ef4444"; ar=f"정배열붕괴+손실 {profit:.1f}%"
    elif rsi_v>70 and profit>5: act="🟡 익절 고려"; ac="#f59e0b"; ar=f"RSI과열({rsi_v:.0f})+수익{profit:.1f}%"
    elif curr>=fixed_tgt: act="🟡 익절 고려"; ac="#f59e0b"; ar=f"목표가 도달"
    elif hold_days>=10 and trend_broken: act="🟡 재검토"; ac="#f59e0b"; ar=f"보유{hold_days}일+추세약화"
    elif s3_on and 40<=rsi_v<=60 and -3<=profit<=0: act="🟢 추가매수 검토"; ac="#10b981"; ar=f"정배열+RSI여유({rsi_v:.0f})+눌림"
    elif s3_on and profit>0: act="⚪ 홀딩"; ac="#94a3b8"; ar=f"정배열유지+수익{profit:.1f}%"
    elif hold_days>0 and hold_days<=3: act="⚪ 관망"; ac="#94a3b8"; ar=f"매수{hold_days}일차"
    else: act="⬜ 관망"; ac="#64748b"; ar="신호 대기"

    hold_str = f"{hold_days}일째" if hold_days>0 else ("날짜미입력" if not p.get("date") else "오늘")
    pc = "#10b981" if profit>=0 else "#ef4444"

    # 장외가 (해외) — 장외가격 + 장외기준 수익률
    pp_html=""
    if not is_kr and d.get("prepost",0)>0:
        pp=d["prepost"]; pl=d.get("prepost_label","")
        pp_profit=(pp-buy)/buy*100 if buy>0 and pp>0 else 0
        pp_reg_profit=(curr-buy)/buy*100 if buy>0 and curr>0 else 0
        pp_color="#10b981" if pp_profit>=0 else "#ef4444"
        pp_html=f'''<div style="background:#1a2744;border:1px solid #3b82f6;padding:8px;border-radius:6px;margin:6px 0;">
  <span style="color:#3b82f6;font-size:11px;font-weight:bold;">{pl}</span>
  <span style="color:{pp_color};font-size:13px;font-weight:bold;margin-left:8px;">{uf(pp)}</span>
  <span style="color:{pp_color};font-size:12px;margin-left:6px;">({pp_profit:+.1f}%)</span>
</div>'''

    # 수급 (국내)
    sup_html2=""
    if is_kr and KIS_APP_KEY:
        sup=supply_signal(name)
        if sup.get("ok"):
            sigs2=" / ".join(sup.get("signals",[])[:2])
            sup_html2=f'<div style="background:#0f172a;padding:8px;border-radius:6px;margin:6px 0;font-size:11px;">수급: <span style="color:{sup["color"]};font-weight:bold;">{sup["verdict"]}</span> <span style="color:#64748b;">{sigs2}</span></div>'

    st.markdown(f"""
<div style="background:#1e293b;padding:14px;border-radius:10px;border-left:5px solid {gc};margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <b>📈 {d['label']}</b>
    <span style="background:{gc};color:#000;font-size:11px;padding:2px 6px;border-radius:4px;">{d['grade']} {d['score']}점</span>
  </div>
  {pp_html}
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:10px;">
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">평단가</div>
      <div style="font-weight:bold;font-size:13px;">{fmt(buy)}</div>
    </div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">현재가</div>
      <div style="font-weight:bold;font-size:13px;">{fmt(curr)}</div>
    </div>
    <div style="background:#0f172a;padding:8px;border-radius:6px;text-align:center;">
      <div style="font-size:10px;color:#94a3b8;">수익률</div>
      <div style="color:{pc};font-weight:bold;font-size:13px;">{profit:+.1f}%</div>
    </div>
  </div>
  <div style="background:#0f172a;padding:10px;border-radius:8px;margin-top:6px;border-left:3px solid {ac};">
    <span style="font-weight:bold;color:{ac};">{act}</span>
    <span style="color:#64748b;font-size:11px;margin-left:8px;">{ar}</span>
  </div>
  {sup_html2}
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;margin-top:6px;">
    <div style="background:#0f172a;padding:6px;border-radius:6px;text-align:center;">
      <div style="font-size:9px;color:#94a3b8;">고정손절</div>
      <div style="color:#ef4444;font-size:11px;font-weight:bold;">{fmt(fixed_stop)}</div>
    </div>
    <div style="background:#0f172a;padding:6px;border-radius:6px;text-align:center;">
      <div style="font-size:9px;color:#94a3b8;">목표가</div>
      <div style="color:#3b82f6;font-size:11px;font-weight:bold;">{fmt(fixed_tgt)}</div>
    </div>
    <div style="background:#0f172a;padding:6px;border-radius:6px;text-align:center;">
      <div style="font-size:9px;color:#94a3b8;">RSI</div>
      <div style="font-size:11px;font-weight:bold;">{rsi_v}</div>
    </div>
    <div style="background:#0f172a;padding:6px;border-radius:6px;text-align:center;">
      <div style="font-size:9px;color:#94a3b8;">보유</div>
      <div style="font-size:11px;font-weight:bold;">{hold_str}</div>
    </div>
  </div>
  <div style="display:flex;justify-content:space-between;margin-top:6px;">
    <span style="font-size:10px;color:#475569;">📡 {d['source']}</span>
    <span style="font-size:11px;">{'🟢정배열' if s3_on else '🔴추세약화'}</span>
  </div>
</div>""", unsafe_allow_html=True)

    if st.button("🗑️ 삭제", key=f"del_{i}"): to_remove=i

if to_remove is not None:
    st.session_state.portfolio.pop(to_remove)
    save_portfolio(st.session_state.portfolio)
    st.rerun()

if st.button("🚨 전체 초기화"):
    st.session_state.portfolio=[]
    save_portfolio([])
    st.rerun()

st.divider()

# ============================================================
# 스캔 결과
# ============================================================
with st.spinner("스캔 중..."):
    kr_top, kr_skip = scan_kr()
    us_top, us_skip = scan_us()

with st.sidebar.expander(f"국내 제외 ({len(kr_skip)})"):
    cnt=Counter()
    for s in kr_skip:
        k=s.get("why","기타").split("(")[0].strip().lstrip("❌").strip()
        cnt[k]+=1
    for k,v in cnt.most_common(): st.write(f"- {k}: {v}")

S_LABELS=["S1:BB","S2:거래량","S3:정배열","S4:RSI","S5:캔들"]

def render(title, data, currency):
    st.header(title)
    if not data:
        st.info("조건 충족 종목 없음")
        return
    cols=st.columns(min(len(data),3))
    medals=["🥇","🥈","🥉","4️⃣","5️⃣"]
    for i,item in enumerate(data):
        gc={"A+":"#f59e0b","A":"#10b981","B+":"#3b82f6","B":"#94a3b8","C":"#64748b"}.get(item.get("등급","C"),"#64748b")
        is_kr=currency=="KRW"
        def ff(v): return f"${v:,.4f}" if v<1 else f"${v:,.3f}" if v<10 else f"${v:,.2f}"
        fmt2=(lambda v:f"₩{int(v):,}") if is_kr else ff
        flags=item.get("s_flags",[False]*5)
        badges=" ".join(
            f"<span style='background:{'#10b981' if ok else '#1e293b'};color:{'#fff' if ok else '#475569'};font-size:9px;padding:2px 4px;border-radius:3px;'>{lbl}</span>"
            for ok,lbl in zip(flags,S_LABELS))
        sigs_html="".join(f"<li style='font-size:11px;margin:2px 0;'>{s}</li>" for s in item.get("signals",[]))
        with cols[i%3]:
            st.markdown(f"""
<div style="background:#1e293b;padding:14px;border-radius:10px;border-left:4px solid {gc};margin-bottom:8px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
    <b>{medals[i]} {item['종목']}</b>
    <span style="background:{gc};color:#000;font-size:11px;padding:2px 6px;border-radius:4px;">{item.get('등급','?')}</span>
  </div>
  <div style="margin-bottom:6px;">{badges}</div>
  <div style="font-size:12px;line-height:1.8;">
    🎯 <b>{item['점수']}점</b> | RSI <b>{item['RSI']}</b><br>
    💰 <b>{fmt2(item['현재가'])}</b> <span style="font-size:10px;color:#64748b;">({item.get('source','')})</span><br>
    🟢 {item['매수구간']}<br>
    📈 <span style="color:#3b82f6;">{fmt2(item['목표가'])}</span>
    📉 <span style="color:#ef4444;">{fmt2(item['손절가'])}</span>
  </div>
  <details><summary style="font-size:11px;color:#94a3b8;cursor:pointer;">신호 상세</summary>
    <ul style="padding-left:14px;margin-top:4px;">{sigs_html}</ul>
  </details>
</div>""", unsafe_allow_html=True)

render("🔥 국내 폭등 예측 TOP 5", kr_top, "KRW")
render("🇺🇸 해외 폭등 예측 TOP 5", us_top, "USD")

# ============================================================
# 백테스트 (탭 맨 뒤 — 기존 유지)
# ============================================================
st.divider()
st.header("🔬 백테스트")
st.caption("백테스트 기능은 별도 탭에서 실행하세요 (무거워서 필요할 때만)")

with st.expander("⚙️ 백테스트 실행", expanded=False):
    import importlib.util
    st.info("백테스트는 기존 v8 코드 참고 — 필요시 별도 파일로 분리 권장")
