# -*- coding: utf-8 -*-
"""命令行入口：python3 -m usim.cli <init|tick|status|report|backtest|watch|reset>"""
import argparse
import sys
import time

from . import backtest as bt
from . import broker, config, engine, market, report


def cmd_init(a):
    st = broker.load()
    if a.force or not st["trades"]:
        st = broker.new_state()
        broker.save(st)
        print("已重置模拟盘：起始资金 $%.2f" % config.CAPITAL)
    else:
        print("账户已存在（%s 创建，权益 %.2f），要重置请加 --force"
              % (st["created"], report.equity_now(st)))


def cmd_tick(a):
    r = engine.tick(force_trade=(True if a.force else None))
    if r and a.report:
        print("\n" + report.status_text())
        print("\n报告已写入: " + report.write_report())


def cmd_status(a):
    print(report.status_text())
    et = market.now_et()
    print("-" * 62)
    print(" 美东 %s · %s" % (et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                           "交易中" if market.market_open(et) else "休市"))


def cmd_report(a):
    print(report.status_text())
    p = report.write_report()
    print("\nMarkdown 已写入: %s" % p)


def cmd_backtest(a):
    r = bt.run(days=a.days, verbose=True)
    import json
    out = {k: v for k, v in r.items() if k not in ("curve",)}
    out["trades"] = [list(t) for t in r["trades"]]
    path = config.DATA_DIR + "/backtest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n明细: %s" % path)


def cmd_watch(a):
    print("循环模式，每 %d 秒一轮（Ctrl+C 退出）" % a.interval)
    while True:
        try:
            engine.tick(verbose=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("轮次异常: %s" % e)
        time.sleep(a.interval)


def cmd_reset(a):
    broker.save(broker.new_state())
    print("已清空账户，资金重置为 $%.2f" % config.CAPITAL)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="usim")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化账户")
    p.add_argument("--force", action="store_true")
    p.set_defaults(f=cmd_init)

    p = sub.add_parser("tick", help="跑一轮（取价→管仓→开仓→记快照）")
    p.add_argument("--force", action="store_true", help="忽略交易时段，强制允许开仓（用于测试）")
    p.add_argument("--report", action="store_true", help="本轮后顺带刷新报告")
    p.set_defaults(f=cmd_tick)

    p = sub.add_parser("status", help="看账户")
    p.set_defaults(f=cmd_status)

    p = sub.add_parser("report", help="生成 Markdown 报告")
    p.set_defaults(f=cmd_report)

    p = sub.add_parser("backtest", help="历史回测")
    p.add_argument("--days", type=int, default=320)
    p.set_defaults(f=cmd_backtest)

    p = sub.add_parser("watch", help="循环跑")
    p.add_argument("--interval", type=int, default=300)
    p.set_defaults(f=cmd_watch)

    p = sub.add_parser("reset", help="清空账户")
    p.set_defaults(f=cmd_reset)

    a = ap.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
