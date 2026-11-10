# -*- coding: utf-8 -*-
"""配置：100 美元美股模拟盘。所有参数都可用环境变量覆盖。"""
import os

CAPITAL = float(os.environ.get("USIM_CAPITAL", "100"))          # 初始资金（美元）
BENCH = os.environ.get("USIM_BENCH", "SPY")                      # 基准

UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META",
    "TSLA", "AMD", "PLTR", "COIN", "HOOD", "NFLX",
]

# --- 趋势策略 ---
SMA_FAST = int(os.environ.get("USIM_SMA_FAST", "20"))
SMA_SLOW = int(os.environ.get("USIM_SMA_SLOW", "50"))
MOM_N = int(os.environ.get("USIM_MOM_N", "20"))                  # 动量回看日数
RSI_N = 14
RSI_MAX = float(os.environ.get("USIM_RSI_MAX", "78"))            # 超买不追
ATR_N = 14

# --- 风险与仓位 ---
STOP_ATR = float(os.environ.get("USIM_STOP_ATR", "1.75"))         # 初始止损 = 1.75×ATR
TRAIL_ATR = float(os.environ.get("USIM_TRAIL_ATR", "2.5"))       # 移动止损 = 峰值 - 2.5×ATR
TAKE_R = float(os.environ.get("USIM_TAKE_R", "0"))             # >0 时浮盈达 TAKE_R 平一半
RISK_PCT = float(os.environ.get("USIM_RISK_PCT", "0.03"))        # 单笔风险 = 权益 3%
MAX_POS = int(os.environ.get("USIM_MAX_POS", "6"))               # 最多持仓数
MAX_WEIGHT = float(os.environ.get("USIM_MAX_WEIGHT", "0.40"))    # 单票上限 40% 权益
CASH_BUFFER = float(os.environ.get("USIM_CASH_BUFFER", "0.03"))  # 留 3% 现金

BREAKOUT_N = int(os.environ.get("USIM_BREAKOUT", "45"))        # 45 日新高突破
TRAIL_R = float(os.environ.get("USIM_TRAIL_R", "2.0"))            # 浮盈 2R 后启用移动止损
BREAKEVEN_R = float(os.environ.get("USIM_BE_R", "0"))          # >0 时浮盈 N 倍 R 后止损抬到成本
TIME_STOP = int(os.environ.get("USIM_TIME_STOP", "0"))          # >0 时 N 日不涨就走
REGIME_FILTER = int(os.environ.get("USIM_REGIME", "1"))          # 只在 SPY > SMA50 时开仓
RS_FILTER = float(os.environ.get("USIM_RS", "0"))                # 预留：相对强弱过滤

SLIP_BPS = float(os.environ.get("USIM_SLIP_BPS", "5"))           # 模拟滑点 5bp
FEE_BPS = float(os.environ.get("USIM_FEE_BPS", "0"))             # 零佣金

MIN_NOTIONAL = 0.01        # 最小下单金额：一分钱（不做整数股限制）
QTY_DECIMALS = 6           # 碎股精度

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 账本与缓存独立存放，网页只读取公开快照 data/us-sim.json。
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "us-sim")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
QUOTE_CACHE = os.path.join(DATA_DIR, "quotes.json")
KLINE_CACHE = os.path.join(DATA_DIR, "kline.json")
LOG_FILE = os.path.join(DATA_DIR, "usim.log")
BACKTEST_FILE = os.path.join(DATA_DIR, "backtest.json")
WEB_JSON = os.path.join(PROJECT_ROOT, "data", "us-sim.json")

QUOTE_TTL = 300            # 实时报价缓存 5 分钟
KLINE_TTL = 1800           # 日线缓存 30 分钟
