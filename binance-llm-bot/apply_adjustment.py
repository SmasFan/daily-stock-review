#!/usr/bin/env python3
"""进化调整执行器: 把 AI 复盘的建议应用到配置

用法:
  apply_adjustment.py list          # 列出全部建议 + 状态
  apply_adjustment.py apply <n>     # 应用第 n 条建议 (改 .env + 标记 + 重启)
  apply_adjustment.py applied       # 看已应用记录
"""
import os, sys, json, re, subprocess, time

BASE = '/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot'
EVO = os.path.join(BASE, 'evolution.json')
ENV = os.path.join(BASE, '.env')

# 参数 -> env 键 映射 (白名单, 防止任意改)
PARAM_MAP = {
    'SMA周期': {'BTC': 'SMA_BTC', 'ETH': 'SMA_ETH', 'SOL': 'SMA_SOL'},
    '杠杆': 'LEV', '回撤熔断': 'DD_STOP', '止损': 'SL_PCT',
    '均线周期': {'BTC': 'SMA_BTC', 'ETH': 'SMA_ETH', 'SOL': 'SMA_SOL'},
}


def load_evo():
    return json.load(open(EVO)) if os.path.exists(EVO) else {'adjustments': []}


def env_set(key, val):
    s = open(ENV).read()
    if re.search(rf'^{key}=', s, re.M):
        s = re.sub(rf'^{key}=.*$', f'{key}={val}', s, flags=re.M)
    else:
        s += f'{key}={val}\n'
    open(ENV, 'w').write(s)


def resolve_env(param):
    """把建议 param 名映射成 env 键。返回 None 若不支持"""
    if 'SMA' in param.upper() or '均线' in param:
        for sym in ['BTC', 'ETH', 'SOL']:
            if sym in param.upper():
                return {'SMA_BTC': None, 'SMA_ETH': None, 'SMA_SOL': None}[f'SMA_{sym}'] or f'SMA_{sym}'
        return None
    if '杠杆' in param:
        return 'LEV'
    if '回撤' in param or '熔断' in param:
        return 'DD_STOP'
    if '止损' in param:
        return 'SL_PCT'
    return None


def list_pending():
    evo = load_evo()
    items = []
    for a in evo.get('adjustments', []):
        for it in a.get('items', []):
            items.append({**it, 'ts': a.get('ts', '')})
    # 标已应用
    applied = {x.get('param') for x in evo.get('applied', [])}
    if not items:
        print('无调整建议记录')
    for i, it in enumerate(items):
        tag = '已应用' if it['param'] in applied else '待定'
        print(f"[{i}] [{tag}] {it.get('ts','')} {it.get('param')}: {it.get('from')} → {it.get('to')}")
        print(f"     理由: {it.get('reason','')}")
    return items


def apply_one(idx):
    evo = load_evo()
    items = []
    for a in evo.get('adjustments', []):
        for it in a.get('items', []):
            items.append({**it, 'ts': a.get('ts', '')})
    applied = [x.get('param') for x in evo.get('applied', [])]
    if idx >= len(items):
        print(f'无第 {idx} 条'); return
    it = items[idx]
    if it['param'] in applied:
        print(f"{it['param']} 已应用过, 跳过"); return
    env_key = resolve_env(it.get('param', ''))
    if not env_key:
        print(f"参数 '{it.get('param')}' 不在可调白名单 (支持: 均线SMA_BTC/ETH/SOL, 杠杆LEV, 回撤DD_STOP, 止损SL_PCT)")
        return
    # 解析 to 值 (LLM 可能给 "20" "2x" "5%" "2.0x")
    raw = str(it.get('to', ''))
    m = re.search(r'([\d.]+)', raw)
    if not m:
        print(f"无法解析 to 值: {raw}"); return
    val = m.group(1)
    # 百分比参数转小数
    if env_key == 'DD_STOP' and '%' in raw:
        val = str(float(val) / 100)
    if env_key == 'SL_PCT' and '%' in raw:
        val = str(float(val) / 100)
    env_set(env_key, val)
    evo.setdefault('applied', []).append({'param': it['param'], 'env': env_key,
                                          'value': val, 'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                                          'reason': it.get('reason', '')})
    json.dump(evo, open(EVO, 'w'), indent=2, ensure_ascii=False)
    print(f"✓ 已应用 {it['param']} → {env_key}={val}")
    print(f"  重启进程生效...")
    # 重启 trader100 + guard (watchdog 会补)
    for name in ['trader100.py', 'guard.py']:
        subprocess.run(['pkill', '-f', name], capture_output=True)
    print('已请求重启 trader100 + guard (watchdog 自动拉起新版)')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'list'
    if cmd == 'list':
        list_pending()
    elif cmd == 'apply' and len(sys.argv) > 2:
        apply_one(int(sys.argv[2]))
    elif cmd == 'applied':
        evo = load_evo()
        for x in evo.get('applied', []):
            print(f"[{x.get('ts')}] {x.get('param')} → {x.get('env')}={x.get('value')} | {x.get('reason','')}")
    else:
        print(__doc__)
