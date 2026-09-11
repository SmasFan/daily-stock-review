#!/usr/bin/env python3
"""C方案验证: DeepSeek 判断市场状态(上升/震荡/下降) 的准确率
抽 20 个历史日, 喂前90天数据 + 技术指标, 问它未来10天方向
对比实际未来10天涨跌, 算准确率
"""
import csv, os, json, random
import numpy as np
from dotenv import load_dotenv
load_dotenv('/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot/.env')

rows = []
with open('btc_1d.csv') as f:
    for r in csv.DictReader(f):
        rows.append({'t': int(r['ts']), 'c': float(r['close'])})
close = np.array([r['c'] for r in rows])
n = len(rows)

def sma(x, w):
    c = np.cumsum(np.insert(x, 0, 0.0)); out = np.full(len(x), np.nan)
    if len(x) >= w: out[w-1:] = (c[w:] - c[:-w])/w
    return out
s50, s200 = sma(close, 50), sma(close, 200)

def feats(i):
    """第 i 天往前 90 天的特征摘要"""
    w = close[i-90:i]
    cur = close[i]
    hi = w.max(); lo = w.min()
    return {
        '当前价': round(cur,0),
        '90日高点': round(hi,0), '90日低点': round(lo,0),
        '位置%': round((cur-lo)/(hi-lo)*100, 0),  # 0=最低 100=最高
        'vs50日均线': round((cur/s50[i]-1)*100, 1) if not np.isnan(s50[i]) else None,
        'vs200日均线': round((cur/s200[i]-1)*100, 1) if not np.isnan(s200[i]) else None,
        '30日涨幅%': round((cur/close[i-30]-1)*100, 1),
        '90日涨幅%': round((cur/w[0]-1)*100, 1),
    }

# 抽 20 个样本 (i 需 >110 保证特征完整, 且 i+10 < n)
random.seed(42)
cands = list(range(120, n-10))
samples = sorted(random.sample(cands, 20))

PROMPT = """你是BTC市场分析师。给你当前技术状态, 判断未来10天方向。
只输出JSON: {"dir":"UP|DOWN|SIDEWAYS","conf":0~1}
UP=未来10天涨>3%, DOWN=跌>3%, SIDEWAYS=介于之间"""

from openai import OpenAI
client = OpenAI(api_key=os.environ['COMMAND_CODE_API_KEY'], base_url='https://api.commandcode.ai/provider/v1')

correct = 0; tot = 0
for idx, i in enumerate(samples):
    actual = (close[i+10]/close[i]-1)*100
    label = 'UP' if actual > 3 else ('DOWN' if actual < -3 else 'SIDEWAYS')
    f = feats(i)
    try:
        r = client.chat.completions.create(model='deepseek/deepseek-v4-flash',
            messages=[{'role':'system','content':PROMPT},
                      {'role':'user','content':json.dumps(f, ensure_ascii=False)}],
            max_tokens=600, timeout=60)
        import re
        text = (r.choices[0].message.content or '').strip()
        m = re.search(r'\{[^}]*\}', text)
        d = json.loads(m.group(0)) if m else {}
        pred = d.get('dir','')
        hit = pred == label
        if hit: correct += 1
        tot += 1
        print(f'{idx:2d}. 实际10天 {actual:+6.1f}% [{label:<8}] LLM说[{pred:<8}] conf{d.get("conf","")} {"✓" if hit else "✗"}')
    except Exception as e:
        print(f'{idx:2d}. LLM 错误: {str(e)[:80]}')

print(f'\n准确率: {correct}/{tot} = {correct/max(tot,1)*100:.0f}%')
print('随机基线: UP/DOWN/SIDEWAYS 各 33% (纯猜)')
