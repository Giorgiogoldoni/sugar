#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAPTOR Sugar — Data Fetch
Scarica dati ICE Sugar No.11 Futures (25 anni) e LSUG.MI (dalla nascita)
Calcola: stagionalità, momentum Antonacci, indicatori RAPTOR, livelli supporto, SAR flip

Schedule:
- 05:30 CET: Analisi completa notturna + aggiornamento storico
- 16:45 CET: Rilevazione intra-day (segnali aggiornati)
- 17:00 CET: Chiusura giornaliera + salvataggio completo
"""

import json, math, os
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import yfinance as yf

# ── RILEVAMENTO ORARIO ─────────────────────────────────
def get_execution_type():
    """Determina il tipo di esecuzione basato sull'orario UTC"""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    minute = now_utc.minute

    # 05:30 CET = 04:30 UTC
    if 4 <= hour < 5 or (hour == 4 and minute >= 30):
        return 'morning'
    # 16:45 CET = 15:45 UTC
    elif 15 <= hour < 16 or (hour == 15 and minute >= 45):
        return 'intraday'
    # 17:00 CET = 16:00 UTC
    elif 16 <= hour < 17 or (hour == 16 and minute >= 0):
        return 'close'
    else:
        return 'manual'

# ── INDICATORI ────────────────────────────────────────
def calc_kama(closes, n=10, fast=2, slow=30):
    fsc = 2/(fast+1); ssc = 2/(slow+1)
    kama = [None]*len(closes)
    if len(closes) <= n: return kama
    kama[n] = closes[n]
    for i in range(n+1, len(closes)):
        d = abs(closes[i]-closes[i-n])
        v = sum(abs(closes[j]-closes[j-1]) for j in range(i-n+1, i+1))
        er = d/v if v else 0
        sc = (er*(fsc-ssc)+ssc)**2
        kama[i] = kama[i-1] + sc*(closes[i]-kama[i-1])
    return kama

def calc_rsi(closes, n=14):
    res = [None]*len(closes)
    for i in range(n+1, len(closes)):
        gs=[]; ls=[]
        for j in range(i-n, i+1):
            dd = closes[j]-closes[j-1]
            gs.append(max(dd,0)); ls.append(max(-dd,0))
        ag=sum(gs)/n; al=sum(ls)/n
        res[i] = round(100-100/(1+ag/al),2) if al>0 else 100.0
    return res

def calc_ao(highs, lows):
    mid = [(h+l)/2 for h,l in zip(highs,lows)]
    def ema(arr, p):
        k=2/(p+1); e=arr[0]; out=[e]
        for x in arr[1:]: e=x*k+e*(1-k); out.append(e)
        return out
    if len(mid)<13: return [0]*len(mid)
    e3=ema(mid,3); e13=ema(mid,13)
    return [round(a-b,4) for a,b in zip(e3,e13)]

def calc_sar(high, low, step=0.03, max_af=0.25):
    """Calcola Parabolic SAR e restituisce anche l'array dei flip (cambio di lato bull/bear)."""
    n=len(high); sar=[None]*n; flip=[False]*n
    if n<5: return sar, flip
    bull=high[1]>high[0]; af=step
    ep=max(high[:2]) if bull else min(low[:2])
    sar[1]=min(low[:2]) if bull else max(high[:2])
    for i in range(2,n):
        ps=sar[i-1]
        was_bull=bull
        if bull:
            sar[i]=min(ps+af*(ep-ps), low[i-1], low[i-2] if i>=2 else low[i-1])
            if low[i]<sar[i]: bull=False; af=step; sar[i]=ep; ep=low[i]
            else:
                if high[i]>ep: ep=high[i]; af=min(af+step,max_af)
        else:
            sar[i]=max(ps+af*(ep-ps), high[i-1], high[i-2] if i>=2 else high[i-1])
            if high[i]>sar[i]: bull=True; af=step; sar[i]=ep; ep=high[i]
            else:
                if low[i]<ep: ep=low[i]; af=min(af+step,max_af)
        if bull!=was_bull:
            flip[i]=True
    return sar, flip

def calc_er(closes, n=10):
    res=[0]*len(closes)
    for i in range(n,len(closes)):
        d=abs(closes[i]-closes[i-n])
        v=sum(abs(closes[j]-closes[j-1]) for j in range(i-n+1,i+1))
        res[i]=round(d/v,4) if v else 0
    return res

def sanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj): return None
        return obj
    if isinstance(obj, dict): return {k:sanitize(v) for k,v in obj.items()}
    if isinstance(obj, list): return [sanitize(v) for v in obj]
    return obj

# ── STAGIONALITÀ 25 ANNI ──────────────────────────────
def calc_stagionalita(closes, dates):
    """Rendimento medio mensile su 25 anni"""
    monthly_rets = defaultdict(list)
    for i in range(1, len(closes)):
        if closes[i] and closes[i-1]:
            month = int(dates[i][5:7])
            ret = (closes[i]-closes[i-1])/closes[i-1]*100
            monthly_rets[month].append(ret)

    stagionalita = []
    mesi = ['Gen','Feb','Mar','Apr','Mag','Giu','Lug','Ago','Set','Ott','Nov','Dic']
    for m in range(1,13):
        rets = monthly_rets[m]
        avg = sum(rets)/len(rets) if rets else 0
        positive = sum(1 for r in rets if r>0)
        wr = positive/len(rets)*100 if rets else 0
        stagionalita.append({
            'mese': m,
            'nome': mesi[m-1],
            'avg_ret': round(avg,3),
            'win_rate': round(wr,1),
            'n_anni': len(rets)
        })
    return stagionalita

# ── MOMENTUM ANTONACCI ────────────────────────────────
def calc_antonacci(closes, dates, lookback_months=12):
    """
    Dual Momentum assoluto: se il rendimento a 12 mesi > 0 → BUY, altrimenti → OUT
    """
    results = []
    approx_days = lookback_months * 21  # ~giorni di trading
    for i in range(approx_days, len(closes)):
        if closes[i] and closes[i-approx_days]:
            ret_12m = (closes[i]-closes[i-approx_days])/closes[i-approx_days]*100
            signal = 'BUY' if ret_12m > 0 else 'OUT'
            results.append({
                'date': dates[i],
                'price': closes[i],
                'ret_12m': round(ret_12m,2),
                'signal': signal
            })
    return results

# ── SUPPORTI ─────────────────────────────────────────
def find_supports(closes, dates, window=3):
    supports = []
    for i in range(window, len(closes)-window):
        if not closes[i]: continue
        is_min = all(closes[i] <= closes[i-j] for j in range(1,window+1) if closes[i-j]) and \
                 all(closes[i] <= closes[i+j] for j in range(1,window+1) if closes[i+j])
        if is_min:
            supports.append({'date': dates[i], 'price': closes[i]})
    return supports[-20:]  # ultimi 20 supporti

# ── SEGNALI RAPTOR + SAR FLIP ─────────────────────────
def calc_signals(closes, kama_fast, kama_slow, volumes, ao_arr, er_arr):
    signals = []
    avg_vol = sum(volumes[-21:-1])/20 if len(volumes)>21 else 1
    for i in range(25, len(closes)):
        kf=kama_fast[i]; ks=kama_slow[i]
        if kf is None or ks is None:
            signals.append(None); continue
        p=closes[i]
        if p>kf and kf>ks:   zona='LONG_CONF'
        elif p>kf and p>ks:  zona='LONG_EARLY'
        elif p<ks:           zona='STOP' if (ks-p)/ks*100>2 else 'USCITA'
        else:                zona='GRIGIA'
        vr=volumes[i]/avg_vol if avg_vol>0 else 1
        gap_ok=ks>0 and abs(kf-ks)/ks>=0.003
        ao=ao_arr[i] if i<len(ao_arr) else 0
        sig=None
        baff=0
        for j in range(max(0,i-5),i+1):
            if kama_fast[j] and closes[j]>kama_fast[j]: baff+=1
            else: baff=0
        if zona=='LONG_CONF' and ao>0 and vr>=2 and baff>=3 and er_arr[i]>=0.35 and gap_ok:
            sig='BUY3'
        elif zona=='LONG_EARLY' and ao>0 and vr>=1.5 and baff>=2 and er_arr[i]>=0.35:
            sig='BUY2'
        elif zona in ('STOP','USCITA'): sig='SELL'
        signals.append(sig)
    return [None]*25 + signals

def calc_sar_flip_events(dates, closes, sar, sar_flip):
    """Elenco eventi di flip SAR con data, prezzo e direzione (LONG/SHORT)"""
    events = []
    for i in range(len(sar_flip)):
        if sar_flip[i] and closes[i] is not None and sar[i] is not None:
            direzione = 'LONG' if closes[i] > sar[i] else 'SHORT'
            events.append({'date': dates[i], 'price': closes[i], 'direction': direzione})
    return events[-30:]  # ultimi 30 flip

# ── MAIN ─────────────────────────────────────────────
def main():
    now = datetime.now()
    exec_type = get_execution_type()

    # Bootstrap safety: se il file dati non esiste ancora o è incompleto
    # (manca la chiave 'ice'), forziamo comunque l'analisi completa,
    # a prescindere dalla finestra oraria rilevata.
    needs_full_analysis = True
    try:
        with open('sugar.json', 'r', encoding='utf-8') as f:
            existing = json.load(f)
        needs_full_analysis = 'ice' not in existing
    except (FileNotFoundError, json.JSONDecodeError):
        needs_full_analysis = True

    if needs_full_analysis and exec_type not in ('morning', 'close', 'manual'):
        print(f"⚠️  sugar.json assente o incompleto — forzo analisi completa (era: {exec_type})")
        exec_type = 'manual'
    print(f"RAPTOR Sugar Fetch — {now.strftime('%Y-%m-%d %H:%M')} [{exec_type.upper()}]")

    # ── ICE Sugar No.11 Futures (25 anni) ─────────────
    print("Scarico ICE Sugar Futures (SB=F)...")
    ice = yf.download("SB=F", start="2000-01-01", interval="1d",
                       auto_adjust=True, progress=False)

    if hasattr(ice.columns, 'levels'):
        ice.columns = ice.columns.get_level_values(0)
    ice_closes = [round(float(c),4) for c in ice['Close'].tolist()]
    ice_highs  = [round(float(c),4) for c in ice['High'].tolist()]
    ice_lows   = [round(float(c),4) for c in ice['Low'].tolist()]
    ice_dates  = [ts.strftime('%Y-%m-%d') for ts in ice.index]
    print(f"ICE Sugar: {len(ice_closes)} barre ({ice_dates[0]} → {ice_dates[-1]})")

    # ── LSUG.MI (dalla nascita) ────────────────────────
    print("Scarico LSUG.MI...")
    sug = yf.download("LSUG.MI", start="2018-01-01", interval="1d",
                      auto_adjust=True, progress=False)

    if hasattr(sug.columns, 'levels'):
        sug.columns = sug.columns.get_level_values(0)
    sug_closes  = [round(float(c),4) for c in sug['Close'].tolist()]
    sug_opens   = [round(float(c),4) for c in sug['Open'].tolist()]
    sug_highs   = [round(float(c),4) for c in sug['High'].tolist()]
    sug_lows    = [round(float(c),4) for c in sug['Low'].tolist()]
    sug_volumes = [int(v) for v in sug['Volume'].tolist()]
    sug_dates   = [ts.strftime('%Y-%m-%d') for ts in sug.index]
    print(f"LSUG: {len(sug_closes)} barre ({sug_dates[0]} → {sug_dates[-1]})")

    # ── ANALISI COMPLETA (MORNING + CLOSE) ─────────────
    if exec_type in ('morning', 'close', 'manual'):
        print(f"[{exec_type.upper()}] Calcolo analisi completa...")

        # KAMA su ICE Sugar
        ice_kama_fast = calc_kama(ice_closes, n=5,  fast=3, slow=20)
        ice_kama_slow = calc_kama(ice_closes, n=20, fast=2, slow=40)

        # Stagionalità 25 anni
        stagionalita = calc_stagionalita(ice_closes, ice_dates)

        # Momentum Antonacci
        antonacci_full = calc_antonacci(ice_closes, ice_dates)
        antonacci_latest = antonacci_full[-1] if antonacci_full else {}

        # Supporti ICE
        ice_supports = find_supports(ice_closes, ice_dates)

        # Indicatori RAPTOR su LSUG
        sug_kama_fast = calc_kama(sug_closes, n=5,  fast=3, slow=20)
        sug_kama_slow = calc_kama(sug_closes, n=20, fast=2, slow=40)
        sug_rsi14     = calc_rsi(sug_closes, 14)
        sug_rsi5      = calc_rsi(sug_closes, 5)
        sug_ao        = calc_ao(sug_highs, sug_lows)
        sug_sar, sug_sar_flip = calc_sar(sug_highs, sug_lows)
        sug_er        = calc_er(sug_closes, 10)

        # Segnali RAPTOR
        sug_signals = calc_signals(sug_closes, sug_kama_fast, sug_kama_slow, sug_volumes, sug_ao, sug_er)

        # Eventi flip SAR
        sug_sar_flip_events = calc_sar_flip_events(sug_dates, sug_closes, sug_sar, sug_sar_flip)
        last_flip = sug_sar_flip_events[-1] if sug_sar_flip_events else None

        # Antonacci su LSUG
        sug_antonacci = calc_antonacci(sug_closes, sug_dates)
        sug_antonacci_latest = sug_antonacci[-1] if sug_antonacci else {}

        # Supporti LSUG
        sug_supports = find_supports(sug_closes, sug_dates)

        def fmt(arr):
            return [round(v,4) if v is not None else None for v in arr]

        output = sanitize({
            'execution_type': exec_type,
            'updated_at': now.isoformat(),
            'updated_display': now.strftime('%d/%m/%Y %H:%M'),

            # ICE Sugar (ultimi 3 anni per il grafico principale)
            'ice': {
                'dates':     ice_dates[-756:],
                'closes':    ice_closes[-756:],
                'highs':     ice_highs[-756:],
                'lows':      ice_lows[-756:],
                'kama_fast': fmt(ice_kama_fast[-756:]),
                'kama_slow': fmt(ice_kama_slow[-756:]),
            },

            # Stagionalità (25 anni)
            'stagionalita': stagionalita,

            # Momentum Antonacci su ICE Sugar
            'antonacci_ice': antonacci_full[-252:],  # ultimo anno
            'antonacci_latest': antonacci_latest,

            # LSUG completo
            'sug': {
                'dates':     sug_dates,
                'closes':    sug_closes,
                'opens':     sug_opens,
                'highs':     sug_highs,
                'lows':      sug_lows,
                'volumes':   sug_volumes,
                'kama_fast': fmt(sug_kama_fast),
                'kama_slow': fmt(sug_kama_slow),
                'rsi14':     fmt(sug_rsi14),
                'rsi5':      fmt(sug_rsi5),
                'ao':        fmt(sug_ao),
                'sar':       fmt(sug_sar),
                'sar_flip':  sug_sar_flip,
                'er':        sug_er,
                'signals':   sug_signals,
            },

            # Antonacci su LSUG
            'antonacci_sug': sug_antonacci[-252:],
            'antonacci_sug_latest': sug_antonacci_latest,

            # Supporti
            'ice_supports': ice_supports,
            'sug_supports': sug_supports,

            # Flip SAR: eventi + ultimo flip
            'sar_flip_events': sug_sar_flip_events,
            'sar_flip_last': last_flip if last_flip else {'date': None, 'price': None, 'direction': None}
        })

    # ── ANALISI LEGGERA INTRADAY (16:45) ────────────────
    else:  # intraday
        print(f"[INTRADAY] Calcolo segnali veloci...")

        # Carica il JSON precedente per mantenere storico
        try:
            with open('sugar.json','r',encoding='utf-8') as f:
                output = json.load(f)
        except:
            output = {}

        # Aggiorna solo gli indicatori attuali
        sug_kama_fast = calc_kama(sug_closes, n=5,  fast=3, slow=20)
        sug_kama_slow = calc_kama(sug_closes, n=20, fast=2, slow=40)
        sug_rsi14     = calc_rsi(sug_closes, 14)
        sug_rsi5      = calc_rsi(sug_closes, 5)
        sug_ao        = calc_ao(sug_highs, sug_lows)
        sug_sar, sug_sar_flip = calc_sar(sug_highs, sug_lows)
        sug_er        = calc_er(sug_closes, 10)

        # Ricalcola i segnali RAPTOR (ricalcolo completo per garantire l'allineamento)
        sug_signals = calc_signals(sug_closes, sug_kama_fast, sug_kama_slow, sug_volumes, sug_ao, sug_er)

        # Eventi flip SAR
        sug_sar_flip_events = calc_sar_flip_events(sug_dates, sug_closes, sug_sar, sug_sar_flip)
        last_flip = sug_sar_flip_events[-1] if sug_sar_flip_events else None

        def fmt(arr):
            return [round(v,4) if v is not None else None for v in arr]

        # Aggiorna il JSON con nuovi indicatori
        output['execution_type'] = exec_type
        output['updated_at'] = now.isoformat()
        output['updated_display'] = now.strftime('%d/%m/%Y %H:%M')
        output.setdefault('sug', {})
        output['sug']['dates'] = sug_dates
        output['sug']['closes'] = sug_closes
        output['sug']['opens'] = sug_opens
        output['sug']['highs'] = sug_highs
        output['sug']['lows'] = sug_lows
        output['sug']['volumes'] = sug_volumes
        output['sug']['kama_fast'] = fmt(sug_kama_fast)
        output['sug']['kama_slow'] = fmt(sug_kama_slow)
        output['sug']['rsi14'] = fmt(sug_rsi14)
        output['sug']['rsi5'] = fmt(sug_rsi5)
        output['sug']['ao'] = fmt(sug_ao)
        output['sug']['sar'] = fmt(sug_sar)
        output['sug']['sar_flip'] = sug_sar_flip
        output['sug']['er'] = sug_er
        output['sug']['signals'] = sug_signals
        output['sar_flip_events'] = sug_sar_flip_events
        output['sar_flip_last'] = last_flip if last_flip else {'date': None, 'price': None, 'direction': None}

        output = sanitize(output)

    os.makedirs('data', exist_ok=True)
    with open('sugar.json','w',encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',',':'), allow_nan=False)
    print(f"✅ sugar.json aggiornato [{exec_type}]")

if __name__ == '__main__':
    main()
