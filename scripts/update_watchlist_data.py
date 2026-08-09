#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉取腾讯实时行情，为 watchlist.html 生成 watchlist_data.js。
评分模型：权威五因子加权（技术面 25% + 估值面 20% + 资金面 25% + 活跃度 15% + 风险面 15%）。
"""
import json
import math
import os
import re
import urllib.request
from datetime import datetime

WATCHLIST = [
    {"name": "黄金ETF易方达", "code": "159934", "sector": "周期资源"},
    {"name": "红利低波ETF富国", "code": "159525", "sector": "红利金融"},
    {"name": "顺丰控股", "code": "002352", "sector": "交通运输"},
    {"name": "TCL科技", "code": "000100", "sector": "半导体"},
    {"name": "电池ETF富国", "code": "561160", "sector": "新能源电力"},
    {"name": "长高电气", "code": "002452", "sector": "新能源电力"},
    {"name": "牧原股份", "code": "002714", "sector": "大消费"},
    {"name": "标普油气ETF嘉实", "code": "159518", "sector": "宽基跨境"},
    {"name": "A500ETF华夏", "code": "512050", "sector": "宽基跨境"},
    {"name": "有色ETF大成", "code": "159980", "sector": "周期资源"},
    {"name": "港股通信息技术ETF华宝", "code": "159131", "sector": "科技-互联网传媒"},
    {"name": "中国铝业", "code": "601600", "sector": "周期资源"},
    {"name": "宁波银行", "code": "002142", "sector": "红利银行"},
    {"name": "中证A500ETF景顺", "code": "159353", "sector": "宽基跨境"},
    {"name": "上证指数ETF汇添富", "code": "510980", "sector": "宽基跨境"},
    {"name": "港股互联网ETF华宝", "code": "513770", "sector": "科技-互联网传媒"},
    {"name": "中远海控", "code": "601919", "sector": "交通运输"},
    {"name": "云计算ETF汇添富", "code": "159273", "sector": "AI算力"},
    {"name": "中百集团", "code": "000759", "sector": "大消费"},
    {"name": "招商银行", "code": "600036", "sector": "红利银行"},
    {"name": "中国神华", "code": "601088", "sector": "周期资源"},
    {"name": "C长鑫", "code": "688825", "sector": "半导体"},
    {"name": "化工ETF富国", "code": "516120", "sector": "周期资源"},
    {"name": "纳指ETF广发", "code": "159941", "sector": "宽基跨境"},
    {"name": "纳指ETF汇添富", "code": "159660", "sector": "宽基跨境"},
    {"name": "标普500ETF南方", "code": "513650", "sector": "宽基跨境"},
    {"name": "养殖ETF汇添富", "code": "159172", "sector": "大消费"},
    {"name": "养殖ETF易方达", "code": "159020", "sector": "大消费"},
    {"name": "航空航天ETF华泰柏瑞", "code": "563380", "sector": "军工"},
    {"name": "合肥城建", "code": "002208", "sector": "房地产"},
    {"name": "五 粮 液", "code": "000858", "sector": "大消费"},
    {"name": "香港证券ETF易方达", "code": "513090", "sector": "红利非银"},
    {"name": "工业富联", "code": "601138", "sector": "AI算力"},
    {"name": "未知", "code": "871894", "sector": "其他"},
    {"name": "创业板人工智能ETF华夏", "code": "159381", "sector": "AI算力"},
    {"name": "星网锐捷", "code": "002396", "sector": "CPO/光模块"},
    {"name": "电力ETF南方", "code": "560580", "sector": "新能源电力"},
    {"name": "中国中车", "code": "601766", "sector": "军工"},
    {"name": "奥海科技", "code": "002993", "sector": "AI算力"},
    {"name": "中国长城", "code": "000066", "sector": "AI算力"},
    {"name": "众生药业", "code": "002317", "sector": "医药医疗"},
    {"name": "甘李药业", "code": "603087", "sector": "医药医疗"},
    {"name": "信立泰", "code": "002294", "sector": "医药医疗"},
    {"name": "华东医药", "code": "000963", "sector": "医药医疗"},
    {"name": "恒瑞医药", "code": "600276", "sector": "医药医疗"},
    {"name": "神州数码", "code": "000034", "sector": "AI算力"},
    {"name": "港股通创新药ETF南方", "code": "159297", "sector": "医药医疗"},
    {"name": "京东方Ａ", "code": "000725", "sector": "半导体"},
    {"name": "恒生科技ETF博时", "code": "159742", "sector": "科技-互联网传媒"},
    {"name": "储能电池ETF易方达", "code": "159566", "sector": "新能源电力"},
    {"name": "万华化学", "code": "600309", "sector": "周期资源"},
    {"name": "巨化股份", "code": "600160", "sector": "周期资源"},
    {"name": "藏格矿业", "code": "000408", "sector": "周期资源"},
    {"name": "圣泉集团", "code": "605589", "sector": "周期资源"},
    {"name": "昊华科技", "code": "600378", "sector": "周期资源"},
    {"name": "宝丰能源", "code": "600989", "sector": "周期资源"},
    {"name": "新宙邦", "code": "300037", "sector": "周期资源"},
    {"name": "云天化", "code": "600096", "sector": "周期资源"},
    {"name": "华鲁恒升", "code": "600426", "sector": "周期资源"},
    {"name": "恒力石化", "code": "600346", "sector": "周期资源"},
    {"name": "卫星化学", "code": "002648", "sector": "周期资源"},
    {"name": "兴发集团", "code": "600141", "sector": "周期资源"},
    {"name": "金发科技", "code": "600143", "sector": "半导体"},
    {"name": "浙江龙盛", "code": "600352", "sector": "周期资源"},
    {"name": "恩捷股份", "code": "002812", "sector": "新能源电力"},
    {"name": "中复神鹰", "code": "688295", "sector": "周期资源"},
    {"name": "桐昆股份", "code": "601233", "sector": "周期资源"},
    {"name": "荣盛石化", "code": "002493", "sector": "周期资源"},
    {"name": "东方盛虹", "code": "000301", "sector": "周期资源"},
    {"name": "凯赛生物", "code": "688065", "sector": "医药医疗"},
    {"name": "彤程新材", "code": "603650", "sector": "周期资源"},
    {"name": "恒逸石化", "code": "000703", "sector": "周期资源"},
    {"name": "星源材质", "code": "300568", "sector": "新能源电力"},
    {"name": "龙佰集团", "code": "002601", "sector": "周期资源"},
    {"name": "三美股份", "code": "603379", "sector": "周期资源"},
    {"name": "亚钾国际", "code": "000893", "sector": "周期资源"},
    {"name": "宏达股份", "code": "600331", "sector": "周期资源"},
    {"name": "新凤鸣", "code": "603225", "sector": "周期资源"},
    {"name": "君正集团", "code": "601216", "sector": "周期资源"},
    {"name": "蓝晓科技", "code": "300487", "sector": "周期资源"},
    {"name": "博源化工", "code": "000683", "sector": "周期资源"},
    {"name": "广东宏大", "code": "002683", "sector": "周期资源"},
    {"name": "杭氧股份", "code": "002430", "sector": "周期资源"},
    {"name": "和邦生物", "code": "603077", "sector": "医药医疗"},
    {"name": "光威复材", "code": "300699", "sector": "周期资源"},
    {"name": "扬农化工", "code": "600486", "sector": "周期资源"},
    {"name": "德明利", "code": "001309", "sector": "半导体"},
    {"name": "钛能化学", "code": "002145", "sector": "周期资源"},
    {"name": "川发龙蟒", "code": "002312", "sector": "周期资源"},
    {"name": "东方铁塔", "code": "002545", "sector": "周期资源"},
    {"name": "川恒股份", "code": "002895", "sector": "周期资源"},
    {"name": "三棵树", "code": "603737", "sector": "周期资源"},
    {"name": "新洋丰", "code": "000902", "sector": "周期资源"},
    {"name": "润丰股份", "code": "301035", "sector": "周期资源"},
    {"name": "大唐发电", "code": "601991", "sector": "新能源电力"},
    {"name": "双环传动", "code": "002472", "sector": "机器人"},
    {"name": "深科技", "code": "000021", "sector": "半导体"},
    {"name": "中国核电", "code": "601985", "sector": "新能源电力"},
    {"name": "埃斯顿", "code": "002747", "sector": "机器人"},
    {"name": "信维通信", "code": "300136", "sector": "科技-通信电子"},
    {"name": "西部材料", "code": "002149", "sector": "周期资源"},
    {"name": "赤峰黄金", "code": "600988", "sector": "周期资源"},
    {"name": "红利ETF汇添富", "code": "560020", "sector": "红利金融"},
    {"name": "黄金股ETF国泰", "code": "517400", "sector": "周期资源"},
    {"name": "盐湖股份", "code": "000792", "sector": "周期资源"},
    {"name": "多氟多", "code": "002407", "sector": "新能源电力"},
    {"name": "化工ETF国泰", "code": "516220", "sector": "周期资源"},
    {"name": "中概互联网ETF易方达", "code": "513050", "sector": "科技-互联网传媒"},
    {"name": "港股创新药ETF广发", "code": "513120", "sector": "医药医疗"},
    {"name": "招商证券", "code": "600999", "sector": "红利非银"},
    {"name": "凯盛科技", "code": "600552", "sector": "半导体"},
    {"name": "证券ETF天弘", "code": "159841", "sector": "红利非银"},
    {"name": "力量钻石", "code": "301071", "sector": "周期资源"},
    {"name": "未知", "code": "920725", "sector": "其他"},
    {"name": "四方达", "code": "300179", "sector": "周期资源"},
    {"name": "黄河旋风", "code": "600172", "sector": "周期资源"},
    {"name": "中天科技", "code": "600522", "sector": "CPO/光模块"},
    {"name": "亨通股份", "code": "600226", "sector": "医药医疗"},
    {"name": "柏诚股份", "code": "601133", "sector": "半导体"},
    {"name": "圣晖集成", "code": "603163", "sector": "半导体"},
    {"name": "美邦股份", "code": "605033", "sector": "医药医疗"},
    {"name": "北方稀土", "code": "600111", "sector": "周期资源"},
    {"name": "金安国纪", "code": "002636", "sector": "PCB/覆铜板"},
    {"name": "东材科技", "code": "601208", "sector": "PCB/覆铜板"},
    {"name": "宏昌电子", "code": "603002", "sector": "PCB/覆铜板"},
    {"name": "火炬电子", "code": "603678", "sector": "半导体"},
    {"name": "博云新材", "code": "002297", "sector": "周期资源"},
    {"name": "宏和科技", "code": "603256", "sector": "PCB/覆铜板"},
    {"name": "证券保险ETF易方达", "code": "512070", "sector": "红利非银"},
    {"name": "东阳光", "code": "600673", "sector": "周期资源"},
    {"name": "风华高科", "code": "000636", "sector": "半导体"},
    {"name": "华升股份", "code": "600156", "sector": "大消费"},
    {"name": "沪电股份", "code": "002463", "sector": "PCB/覆铜板"},
    {"name": "华天科技", "code": "002185", "sector": "半导体"},
    {"name": "华盛昌", "code": "002980", "sector": "科技-通信电子"},
    {"name": "招金黄金", "code": "000506", "sector": "周期资源"},
    {"name": "均瑶健康", "code": "605388", "sector": "大消费"},
    {"name": "生益科技", "code": "600183", "sector": "PCB/覆铜板"},
    {"name": "科创半导体ETF华夏", "code": "588170", "sector": "半导体"},
    {"name": "长电科技", "code": "600584", "sector": "半导体"},
    {"name": "九号公司-WD", "code": "689009", "sector": "机器人"},
    {"name": "雷赛智能", "code": "002979", "sector": "机器人"},
    {"name": "滨海能源", "code": "000695", "sector": "周期资源"},
    {"name": "福耀玻璃", "code": "600660", "sector": "汽车零部件"},
    {"name": "宁波中百", "code": "600857", "sector": "大消费"},
    {"name": "宁波能源", "code": "600982", "sector": "新能源电力"},
    {"name": "中国人寿", "code": "601628", "sector": "红利非银"},
    {"name": "云南锗业", "code": "002428", "sector": "周期资源"},
    {"name": "宝鼎科技", "code": "002552", "sector": "周期资源"},
    {"name": "中国人保", "code": "601319", "sector": "红利非银"},
    {"name": "稀土ETF华泰柏瑞", "code": "516780", "sector": "周期资源"},
    {"name": "华电辽能", "code": "600396", "sector": "新能源电力"},
    {"name": "全球芯片LOF", "code": "501225", "sector": "半导体"},
    {"name": "航发科技", "code": "600391", "sector": "军工"},
    {"name": "浪潮软件", "code": "600756", "sector": "AI算力"},
    {"name": "澜起科技", "code": "688008", "sector": "半导体"},
    {"name": "日经225ETF华安", "code": "513880", "sector": "宽基跨境"},
    {"name": "永鼎股份", "code": "600105", "sector": "CPO/光模块"},
    {"name": "国晟科技", "code": "603778", "sector": "新能源电力"},
    {"name": "天赐材料", "code": "002709", "sector": "新能源电力"},
    {"name": "陕西黑猫", "code": "601015", "sector": "周期资源"},
    {"name": "立讯精密", "code": "002475", "sector": "科技-通信电子"},
    {"name": "未知", "code": "920685", "sector": "其他"},
    {"name": "半导体设备ETF国泰", "code": "159516", "sector": "半导体"},
    {"name": "美诺华", "code": "603538", "sector": "医药医疗"},
    {"name": "标普油气ETF富国", "code": "513350", "sector": "宽基跨境"},
    {"name": "杭电股份", "code": "603618", "sector": "科技-通信电子"},
    {"name": "光环新网", "code": "300383", "sector": "AI算力"},
    {"name": "利通电子", "code": "603629", "sector": "AI算力"},
    {"name": "云赛智联", "code": "600602", "sector": "AI算力"},
    {"name": "东山精密", "code": "002384", "sector": "PCB/覆铜板"},
    {"name": "通信ETF华夏", "code": "515050", "sector": "科技-通信电子"},
    {"name": "天齐锂业", "code": "002466", "sector": "新能源电力"},
    {"name": "拉卡拉", "code": "300773", "sector": "红利非银"},
    {"name": "电子ETF华宝", "code": "515260", "sector": "科技-通信电子"},
    {"name": "创业板ETF广发", "code": "159952", "sector": "宽基跨境"},
    {"name": "盛新锂能", "code": "002240", "sector": "新能源电力"},
    {"name": "中科曙光", "code": "603019", "sector": "AI算力"},
    {"name": "新能源ETF南方", "code": "516160", "sector": "新能源电力"},
    {"name": "金融科技ETF博时", "code": "516860", "sector": "红利非银"},
    {"name": "中国巨石", "code": "600176", "sector": "周期资源"},
    {"name": "中韩半导体ETF华泰柏瑞", "code": "513310", "sector": "半导体"},
    {"name": "通信ETF国泰", "code": "515880", "sector": "科技-通信电子"},
    {"name": "亨通光电", "code": "600487", "sector": "CPO/光模块"},
    {"name": "中证红利ETF招商", "code": "515080", "sector": "红利金融"},
    {"name": "电池ETF易方达", "code": "159175", "sector": "新能源电力"},
    {"name": "电力ETF工银", "code": "560270", "sector": "新能源电力"},
    {"name": "陕西煤业", "code": "601225", "sector": "周期资源"},
    {"name": "煤炭ETF国泰", "code": "515220", "sector": "周期资源"},
    {"name": "乐凯胶片", "code": "600135", "sector": "半导体"},
    {"name": "宁波建工", "code": "601789", "sector": "基建交通"},
    {"name": "华胜天成", "code": "600410", "sector": "AI算力"},
    {"name": "石油ETF国泰", "code": "561360", "sector": "周期资源"},
    {"name": "物流ETF富国", "code": "516910", "sector": "其他ETF"},
    {"name": "中海油服", "code": "601808", "sector": "周期资源"},
    {"name": "海油发展", "code": "600968", "sector": "周期资源"},
    {"name": "石油ETF景顺", "code": "159588", "sector": "周期资源"},
    {"name": "长江电力", "code": "600900", "sector": "新能源电力"},
    {"name": "化工ETF鹏华", "code": "159870", "sector": "周期资源"},
    {"name": "大位科技", "code": "600589", "sector": "AI算力"},
    {"name": "日经ETF华夏", "code": "513520", "sector": "宽基跨境"},
    {"name": "机器人ETF易方达", "code": "159530", "sector": "机器人"},
    {"name": "上证指数ETF富国", "code": "510210", "sector": "宽基跨境"},
    {"name": "省广集团", "code": "002400", "sector": "科技-互联网传媒"},
    {"name": "北方铜业", "code": "000737", "sector": "周期资源"},
    {"name": "江西铜业", "code": "600362", "sector": "周期资源"},
    {"name": "铜陵有色", "code": "000630", "sector": "周期资源"},
    {"name": "紫金矿业", "code": "601899", "sector": "周期资源"},
    {"name": "中国西电", "code": "601179", "sector": "新能源电力"},
    {"name": "电网设备ETF广发", "code": "159320", "sector": "新能源电力"},
    {"name": "中国黄金", "code": "600916", "sector": "周期资源"},
    {"name": "新特电气", "code": "301120", "sector": "新能源电力"},
    {"name": "特变电工", "code": "600089", "sector": "新能源电力"},
    {"name": "世纪华通", "code": "002602", "sector": "科技-互联网传媒"},
    {"name": "机器人ETF万家", "code": "560630", "sector": "机器人"},
    {"name": "方正电机", "code": "002196", "sector": "汽车零部件"},
    {"name": "五洲新春", "code": "603667", "sector": "汽车零部件"},
    {"name": "西部黄金", "code": "601069", "sector": "周期资源"},
    {"name": "中钨高新", "code": "000657", "sector": "周期资源"},
    {"name": "湖南黄金", "code": "002155", "sector": "周期资源"},
    {"name": "中信证券", "code": "600030", "sector": "红利非银"},
    {"name": "国投白银LOF", "code": "161226", "sector": "周期资源"},
    {"name": "电网设备ETF国泰", "code": "561380", "sector": "新能源电力"},
    {"name": "人工智能ETF易方达", "code": "159819", "sector": "AI算力"},
    {"name": "有色金属ETF南方", "code": "512400", "sector": "周期资源"},
    {"name": "白银有色", "code": "601212", "sector": "周期资源"},
    {"name": "工业有色ETF万家", "code": "560860", "sector": "周期资源"},
    {"name": "机器人ETF华夏", "code": "562500", "sector": "机器人"},
    {"name": "中国卫星", "code": "600118", "sector": "军工"},
    {"name": "中国海诚", "code": "002116", "sector": "基建交通"},
    {"name": "航天电子", "code": "600879", "sector": "军工"},
    {"name": "航天发展", "code": "000547", "sector": "军工"},
    {"name": "平安银行", "code": "000001", "sector": "红利银行"},
    {"name": "创业板指", "code": "399006", "sector": "宽基跨境"},
    {"name": "科创50ETF广发", "code": "588060", "sector": "半导体"},
    {"name": "金ETF富国", "code": "518680", "sector": "周期资源"},
    {"name": "深南电路", "code": "002916", "sector": "PCB/覆铜板"},
    {"name": "胜宏科技", "code": "300476", "sector": "PCB/覆铜板"},
    {"name": "景旺电子", "code": "603228", "sector": "PCB/覆铜板"},
    {"name": "中芯国际", "code": "688981", "sector": "半导体"},
    {"name": "北方华创", "code": "002371", "sector": "半导体"},
    {"name": "兆易创新", "code": "603986", "sector": "半导体"},
    {"name": "韦尔股份", "code": "603501", "sector": "半导体"},
    {"name": "通富微电", "code": "002156", "sector": "半导体"},
    {"name": "中际旭创", "code": "300308", "sector": "CPO/光模块"},
    {"name": "新易盛", "code": "300502", "sector": "CPO/光模块"},
    {"name": "天孚通信", "code": "300394", "sector": "CPO/光模块"},
    {"name": "光迅科技", "code": "002281", "sector": "CPO/光模块"},
    {"name": "太辰光", "code": "300570", "sector": "CPO/光模块"},
    {"name": "剑桥科技", "code": "603083", "sector": "CPO/光模块"},
    {"name": "浪潮信息", "code": "000977", "sector": "AI算力"},
    {"name": "寒武纪", "code": "688256", "sector": "AI算力"},
    {"name": "海光信息", "code": "688041", "sector": "AI算力"},
    {"name": "汇川技术", "code": "300124", "sector": "机器人"},
    {"name": "绿的谐波", "code": "688017", "sector": "机器人"},
    {"name": "鸣志电器", "code": "603728", "sector": "机器人"},
    {"name": "拓斯达", "code": "300607", "sector": "机器人"},
    {"name": "步科股份", "code": "688160", "sector": "机器人"},
    {"name": "伟创电气", "code": "688698", "sector": "机器人"},
]


def tencent_symbol(code: str) -> str:
    """根据代码判断上海/深圳前缀。"""
    if code.startswith(("6", "5", "11", "58")):
        return f"sh{code}"
    return f"sz{code}"


def fetch_quotes(codes):
    symbols = [tencent_symbol(c) for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
            return raw
    except Exception as e:
        print(f"拉取行情失败: {e}")
        return ""


def parse_quote(raw: str, code: str):
    sym = tencent_symbol(code)
    m = re.search(rf'v_{sym}="([^"]*)";', raw)
    if not m:
        return None
    parts = m.group(1).split("~")
    if len(parts) < 50:
        return None
    def f(idx, default=None):
        try:
            v = parts[idx]
            return float(v) if v != "" else default
        except (ValueError, IndexError):
            return default
    def s(idx, default=None):
        try:
            v = parts[idx]
            return v if v != "" else default
        except IndexError:
            return default
    # 成交额字段单位为万元
    amount_10k = f(37, 0) or 0
    price = f(3, 0) or 0
    prev_close = f(4, 0) or 0
    high = f(33, 0) or 0
    low = f(34, 0) or 0
    volatility = ((high - low) / prev_close * 100) if prev_close else 0
    return {
        "name": s(1, code),
        "code": code,
        "price": price,
        "prevClose": prev_close,
        "open": f(5, 0) or 0,
        "high": high,
        "low": low,
        "change": f(32, 0) or 0,
        "volume": int(f(36, 0) or 0),
        "amount": amount_10k * 10000,
        "turnover": f(38, 0) or 0,
        "pe": f(39, None),
        "pb": f(46, None),
        "weiBi": f(49, 0) or 0,
        "volatility": round(volatility, 2),
    }


def rank_score(values, higher_is_better=True):
    """把一列数值映射到 0-100 的分位分数。"""
    n = len(values)
    if n == 0:
        return []
    sorted_vals = sorted(values, reverse=higher_is_better)
    # 相同的值给相同分数：取平均值
    rank_map = {}
    for i, v in enumerate(sorted_vals):
        if v not in rank_map:
            rank_map[v] = []
        rank_map[v].append(i)
    return [sum(rank_map[v]) / len(rank_map[v]) / max(n - 1, 1) * 100 for v in values]


def rating(total):
    if total >= 80:
        return "A"
    if total >= 60:
        return "B"
    if total >= 40:
        return "C"
    if total >= 20:
        return "D"
    return "E"


def compute_scores(records):
    """
    权威五因子综合评分模型：
    - 技术面 25%：当日涨跌幅分位得分，衡量短期动量
    - 估值面 20%：PE/PB 越低越好，ETF/债券等无数据给中性 50
    - 资金面 25%：委比 60% + 换手率 40%，反映资金主动进攻/撤离意愿
    - 活跃度 15%：成交额分位得分，流动性越高得分越高
    - 风险面 15%：日内波动越低得分越高（低风险偏好）
    """
    changes = [r["change"] for r in records]
    turnover = [r["turnover"] for r in records]
    amount = [r["amount"] for r in records]
    wei_bi = [r["weiBi"] for r in records]
    volatility = [r.get("volatility", 0) for r in records]

    momentum = rank_score(changes, higher_is_better=True)
    amount_score = rank_score(amount, higher_is_better=True)
    turnover_score = rank_score(turnover, higher_is_better=True)
    wei_bi_score = rank_score(wei_bi, higher_is_better=True)
    risk_score = rank_score(volatility, higher_is_better=False)

    for i, r in enumerate(records):
        # 估值分：PE/PB 越低越好；ETF/债券等无数据给中性 50
        pe = r.get("pe")
        pb = r.get("pb")
        if pe and pb and pe > 0 and pb > 0:
            # PE: 0→100, 50→50, 100→0 的线性映射并截断
            pe_score = max(0, min(100, 100 - pe))
            # PB: 0→100, 3→50, 6→0 的线性映射并截断
            pb_score = max(0, min(100, 100 - (pb / 6) * 100))
            valuation = (pe_score + pb_score) / 2
        else:
            valuation = 50

        # 资金面：委比 60% + 换手率 40%
        fund = wei_bi_score[i] * 0.6 + turnover_score[i] * 0.4

        total = (
            momentum[i] * 0.25
            + valuation * 0.20
            + fund * 0.25
            + amount_score[i] * 0.15
            + risk_score[i] * 0.15
        )
        r["momentumScore"] = round(momentum[i], 1)
        r["valuationScore"] = round(valuation, 1)
        r["fundScore"] = round(fund, 1)
        r["activeScore"] = round(amount_score[i], 1)
        r["riskScore"] = round(risk_score[i], 1)
        r["totalScore"] = round(total, 1)
        r["rating"] = rating(total)
        # 估算净流入 = 成交额 * 委比 / 100（仅为方向/强度估算，非交易所真实净流入）
        r["estNetFlow"] = round(r["amount"] * (r["weiBi"] / 100), 2)
    return records


def is_a_share_market_open(now=None):
    """判断当前是否为 A 股交易时间（工作日 9:30-11:30、13:00-15:00，北京时间）。"""
    if now is None:
        now = datetime.now()
    # 若系统时区非北京时间，仍以本地时间近似；部署在 UTC 环境时请改用 pytz
    weekday = now.weekday()
    if weekday >= 5:
        return False
    hour = now.hour
    minute = now.minute
    time = hour * 60 + minute
    return (570 <= time <= 690) or (780 <= time <= 900)


def main():
    import sys
    force = "--force" in sys.argv
    if not force and not is_a_share_market_open():
        print("当前不在 A 股交易时间，跳过数据同步（可使用 --force 强制运行）。")
        return
    codes = [item["code"] for item in WATCHLIST]
    raw = fetch_quotes(codes)
    records = []
    for base in WATCHLIST:
        rec = parse_quote(raw, base["code"])
        if not rec:
            # 如果接口失败，保留基础信息并给出占位值
            rec = {
                "name": base["name"],
                "code": base["code"],
                "price": 0,
                "prevClose": 0,
                "open": 0,
                "high": 0,
                "low": 0,
                "change": 0,
                "volume": 0,
                "amount": 0,
                "turnover": 0,
                "pe": None,
                "pb": None,
                "weiBi": 0,
                "volatility": 0,
            }
        rec["sector"] = base["sector"]
        records.append(rec)

    records = compute_scores(records)

    data = {
        "stocks": records,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "watchlist_data.js")
    out_path = os.path.abspath(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("// Auto-generated by scripts/update_watchlist_data.py\n")
        f.write("window.watchlistData = ")
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write(";\n")
    print(f"已生成 {out_path}，共 {len(records)} 条记录。")


if __name__ == "__main__":
    main()
