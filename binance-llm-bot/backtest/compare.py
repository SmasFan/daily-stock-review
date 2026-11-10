#!/usr/bin/env python3
"""当前参数 vs 候选参数的逐标的对比。在 binance-llm-bot 目录下运行。"""
import sys, os
sys.path.insert(0, 'backtest')
import sweep
from sweep import POOLS, load, simulate, metrics

def show(pool, name, cfg):
    poolcfg = POOLS[pool]
    base = dict(cfg); base['min_notional']=poolcfg['min_notional']
    r = sweep.run_pool(pool, base)
    if not r:
        print(name, 'NO DATA'); return
    f,tr,oo=r['full'],r['train'],r['oos']
    print(f"{name:46s} full {f['total']*100:7.1f}% mdd {f['mdd']*100:5.1f}% sh {f['sharpe']:5.2f} | train {tr['sharpe']:5.2f} | oos {oo['total']*100:7.1f}% sh {oo['sharpe']:5.2f} | tr {r['trades']}")
    # per symbol
    each=100/len(poolcfg['symbols']); parts=[]
    for sym in poolcfg['symbols']:
        rows=load(sym,poolcfg['tf'])
        if not rows: continue
        c=dict(base); c['margin']=each
        ts,eq,trs=simulate(rows,c)
        if eq:
            m=metrics(eq, 365 if poolcfg['tf']=='1d' else 365*24)
            parts.append(f"{sym}:{m['total']*100:+.0f}%")
    print('   ', ' '.join(parts))

pg_d = POOLS['daily']['pg']; pg_s=POOLS['short']['pg']
print('=== daily ===')
show('daily','current n50 sl12 lev5 pg', dict(family='sma',n=50,sl=0.12,lev=5.0,pg=pg_d))
show('daily','n50 sl5 lev5 pg', dict(family='sma',n=50,sl=0.05,lev=5.0,pg=pg_d))
show('daily','n60 sl10 lev5 pg', dict(family='sma',n=60,sl=0.10,lev=5.0,pg=pg_d))
show('daily','n60 sl15 lev5 pg', dict(family='sma',n=60,sl=0.15,lev=5.0,pg=pg_d))
show('daily','mom n20 th2 sl8 lev5 pg', dict(family='mom',n=20,th=0.02,sl=0.08,lev=5.0,pg=pg_d))
show('daily','mom n30 th5 sl10 lev5 pg', dict(family='mom',n=30,th=0.05,sl=0.10,lev=5.0,pg=pg_d))
show('daily','sma_filter n100 sl10 lev5 pg', dict(family='sma_filter',n=100,sl=0.10,lev=5.0,pg=pg_d))
print('=== short ===')
show('short','current n50 sl3 lev5 pg', dict(family='sma',n=50,sl=0.03,lev=5.0,pg=pg_s))
show('short','n80 sl8 lev5 pg', dict(family='sma',n=80,sl=0.08,lev=5.0,pg=pg_s))
show('short','n80 sl8 lev3 pg', dict(family='sma',n=80,sl=0.08,lev=3.0,pg=pg_s))
show('short','sma_filter n80 sl8 lev5 pg', dict(family='sma_filter',n=80,sl=0.08,lev=5.0,pg=pg_s))
show('short','sma_filter n80 sl8 lev3 pg', dict(family='sma_filter',n=80,sl=0.08,lev=3.0,pg=pg_s))
show('short','n100 sl5 lev3 pg off', dict(family='sma',n=100,sl=0.05,lev=3.0,pg=None))
show('short','n50 sl3 lev3 pg', dict(family='sma',n=50,sl=0.03,lev=3.0,pg=pg_s))
