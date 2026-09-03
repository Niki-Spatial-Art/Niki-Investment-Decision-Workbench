#!/usr/bin/env python3
"""全市场A股选股 v3 —— 大盘闸门 + 多因子打分 + 买卖点/止损价。
比 v2 多三层：①大盘三信号择时闸门 ②多因子综合打分(平衡权重) ③风险排除(腰斩反弹/高位放量)。
输出：综合分 Top20 + 每只的回踩买点价/止损价，而非"264只平铺列表"。
"""
import urllib.request, urllib.parse, json, ssl, time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
UA = {'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64)','Referer':'https://gu.qq.com/','Accept':'*/*'}
UT = 'fa5fd1943c7b386f172d6893dbfba10b'

def http_get(url, headers=UA, timeout=12):
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode('utf-8','ignore')

# ---------- 大盘闸门 ----------
def fetch_index_kline(sym, n=70):
    url = f'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param={sym},day,,,{n},'
    d = json.loads(http_get(url))
    k = d['data'][sym]
    arr = k.get('day') or k.get('qfqday')
    return [float(r[2]) for r in arr]

def _ma(c, n):
    return sum(c[-n:])/n if len(c) >= n else None

def check_market_gate():
    """大盘三信号闸门。返回 (gate_open: bool, detail: dict)"""
    detail = {}
    hs300 = fetch_index_kline('sh000300')
    sh = fetch_index_kline('sh000001')
    zz1000 = fetch_index_kline('sh000852')
    hs_ma20 = _ma(hs300, 20)
    gate = {
        '沪深300现价': round(hs300[-1],2),
        '沪深300_MA20': round(hs_ma20,2),
        '沪深300站上MA20': hs300[-1] > hs_ma20,
        '上证现价': round(sh[-1],2),
        '上证站上4000': sh[-1] > 4000,
        '中证1000_MA20': round(_ma(zz1000,20),2),
        '中证1000现价': round(zz1000[-1],2),
        '中证1000站上MA20': zz1000[-1] > _ma(zz1000,20),
    }
    # 三信号：沪深300站MA20 + 上证4000 + 中证1000不拖后腿(站上MA20)
    open3 = gate['沪深300站上MA20'] and gate['上证站上4000'] and gate['中证1000站上MA20']
    gate['三信号全开'] = open3
    return open3, gate

# ---------- 全市场列表 ----------
def fetch_universe():
    out = []
    fs = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
    fields = 'f2,f3,f5,f6,f12,f14,f100'
    pn = 1
    while True:
        qs = urllib.parse.urlencode({'pn':pn,'pz':100,'po':1,'np':1,'fltt':2,'invt':2,'fid':'f3','fs':fs,'fields':fields,'ut':UT})
        d = json.loads(http_get(f'https://push2delay.eastmoney.com/api/qt/clist/get?{qs}'))
        if not d or not d.get('data') or not d['data'].get('diff'):
            break
        out.extend(d['data']['diff'])
        if len(out) >= d['data'].get('total', 0) or pn > 60:
            break
        pn += 1
        time.sleep(0.1)
    return out

def fetch_kline(code, n=70):
    sym = ('sh' if code.startswith(('6','5','9','1','2')) else 'sz') + code
    url = f'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param={sym},day,,,{n},'
    d = json.loads(http_get(url))
    k = d['data'][sym]
    arr = k.get('day') or k.get('qfqday')
    # 返回 [日期,开,收,高,低,量]
    return arr

def ma(c, n):
    return sum(c[-n:])/n if len(c) >= n else None

# ---------- 多因子打分（平衡权重） ----------
def score_stock(k):
    """k = 日K数组[日期,开,收,高,低,量]。返回综合分0-100，越高越值得买。"""
    closes = [float(r[2]) for r in k]
    highs = [float(r[3]) for r in k]
    lows = [float(r[4]) for r in k]
    vols = [float(r[5]) for r in k]
    c = closes[-1]
    m5, m10, m20, m60 = ma(closes,5), ma(closes,10), ma(closes,20), ma(closes,60)
    if not (m5 and m10 and m20 and m60):
        return None
    b20 = (c/m20 - 1)*100
    r20 = (c/closes[-21] - 1)*100 if len(closes) >= 21 else 0
    r60 = (c/closes[-61] - 1)*100 if len(closes) >= 61 else 0
    hi60 = max(highs[-60:]) if len(highs) >= 60 else max(highs)

    # 因子1 动量(20/60日涨幅) —— 平衡：趋势向上但不过热。满分30。
    # 核心：必须区分"上涨"和"下跌"。r20/r60 为负 = 下跌趋势，动量分应低。
    mom = 0
    if r20 > 0 and r60 > 0:
        mom = min(r20, 25)          # 双正向，趋势健康，封顶25
    elif r20 > 0 and r60 <= 0:
        mom = r20 * 0.4             # 仅短期反弹(长期仍弱)，打折
    else:
        mom = r20 * 0.4             # 下跌中，负分(拖累总分)
    mom_score = max(0, min(30, 15 + mom))   # 映射 0~30，中性=15

    # 因子2 回踩度(乖离MA20) —— 越贴近MA20(0附近)越好，偏离越远越差
    bias_score = max(0, 30 - abs(b20)*6)                 # b20=0 满分30，±5% 归零

    # 因子3 量能(回踩是否缩量) —— 缩量回踩=洗盘，放量下跌=出货
    v5 = sum(vols[-5:])/5
    v20 = sum(vols[-20:])/20
    vol_ratio = v5/v20 if v20 else 1
    if b20 < 1.0:                                       # 贴近/跌破均线，看是否缩量
        vol_score = 20 if vol_ratio < 0.85 else (10 if vol_ratio < 1.1 else 3)
    else:
        vol_score = 12 if vol_ratio < 1.2 else 6        # 上攻中，温和放量即可
    vol_score = min(20, vol_score)

    # 因子4 趋势强度(多头排列) —— MA5>MA10>MA20 越顺越高
    trend = 0
    if m5 > m10: trend += 5
    if m10 > m20: trend += 5
    if c > m20: trend += 5
    if c > m60: trend += 5
    trend_score = min(20, trend)                        # 0~20

    # 因子5 波动(振幅) —— 低波动加分(胜率优先)
    atr = sum(h-l for h,l in zip(highs[-10:], lows[-10:]))/10
    atr_pct = atr/c*100 if c else 0
    vol_score2 = max(0, min(10, 10 - (atr_pct-3)))      # 振幅3%以内满分

    total = mom_score + bias_score + vol_score + trend_score + vol_score2
    return {'total': round(total,1), 'mom':round(mom_score,1), 'bias':round(bias_score,1),
            'vol':round(vol_score,1), 'trend':round(trend_score,1), 'vol2':round(vol_score2,1),
            'b20':round(b20,2), 'r20':round(r20,2), 'r60':round(r60,2),
            'hi60':round(hi60,2), 'm20':round(m20,2), 'm5':round(m5,2), 'm10':round(m10,2)}

# ---------- 逐票评估 ----------
def eval_one(item, gate_open):
    code = str(item.get('f12','')).strip()
    name = str(item.get('f14','')).strip()
    if not code or not name:
        return None
    if 'ST' in name.upper() or '退' in name or name[0] in ('N','C'):
        return None
    try:
        pct = float(item.get('f3'))
        amount = float(item.get('f6'))
    except (TypeError, ValueError):
        return None
    if amount < 2e8:
        return None
    if pct > 9.5 or pct < -5:
        return None
    try:
        k = fetch_kline(code)
    except Exception:
        return None
    if len(k) < 62:
        return None
    sc = score_stock(k)
    if sc is None:
        return None
    c = float(k[-1][2])

    # 基础形态筛选（保留 v2 的核心，但放宽为打分后过滤）
    if sc['b20'] > 6.0 or sc['b20'] < -4.0:
        return None
    if sc['m5'] <= sc['m10']:
        return None
    if c <= sc['m20']:
        return None
    if pct > 6.0:
        return None

    # 风险排除①：腰斩反弹陷阱 —— 60日高点回撤超30%的票，即使"回踩"也是下跌中继
    drawdown60 = (c/sc['hi60'] - 1)*100
    if drawdown60 < -30:
        return None
    # 风险排除②：高位放量滞涨 —— 近20日涨超20%且当日放量但涨幅<1%(出货迹象)
    if sc['r20'] > 20 and pct < 1.0:
        return None

    # 买卖点
    buy = round(sc['m10'],2)          # 回踩买点：MA10 附近
    buy_low = round(min(sc['m10'], c*0.985),2)
    stop = round(buy_low*0.965,2)     # 止损：买点下方3.5%
    return {'code':code,'name':name,'pct':pct,'amount':round(amount/1e8,2),
            'price':round(c,2),'industry':str(item.get('f100','')),
            'score':sc['total'],
            'factor':{'动量':sc['mom'],'回踩':sc['bias'],'量能':sc['vol'],'趋势':sc['trend'],'波动':sc['vol2']},
            'b20':sc['b20'],'r20':sc['r20'],'r60':sc['r60'],
            'ma20':sc['m20'],'drawdown60':round(drawdown60,1),
            'buy':buy,'buy_low':buy_low,'stop':stop}

if __name__ == '__main__':
    print('=== 步骤1/3：判断大盘三信号闸门 ===', file=sys.stderr)
    gate_open, gate = check_market_gate()
    print(json.dumps(gate, ensure_ascii=False, indent=2))
    if not gate_open:
        print('\n⚠️  大盘闸门未开：以下所有票仅作"预备弹药池"，暂不买入！', file=sys.stderr)

    print('\n=== 步骤2/3：拉全市场A股并多因子打分 ===', file=sys.stderr)
    uni = fetch_universe()
    print(f'共 {len(uni)} 只，并发打分...', file=sys.stderr)
    results = []
    with ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(eval_one, it, gate_open): it for it in uni}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 1000 == 0:
                print(f'  进度 {done}/{len(uni)}', file=sys.stderr)
            r = fut.result()
            if r:
                results.append(r)

    # 去重（同一代码可能在沪深两个板块列表重复出现）
    seen = set(); dedup = []
    for r in results:
        if r['code'] not in seen:
            seen.add(r['code']); dedup.append(r)
    results = dedup

    results.sort(key=lambda x: -x['score'])
    print(f'\n===== 通过筛选 {len(results)} 只，按综合分排序 =====\n')
    top = results[:20]
    print(f"{'排名':<4}{'代码':<8}{'名称':<10}{'行业':<12}{'现价':>7}{'回踩买点':>8}{'止损价':>7}{'综合分':>7}")
    for i, r in enumerate(top, 1):
        print(f"{i:<4}{r['code']:<8}{r['name']:<10}{r['industry']:<12}{r['price']:>7.2f}{r['buy_low']:>8.2f}{r['stop']:>7.2f}{r['score']:>7.1f}")
        print(f"     因子[动量{r['factor']['动量']}/回踩{r['factor']['回踩']}/量能{r['factor']['量能']}/趋势{r['factor']['趋势']}/波动{r['factor']['波动']}] 乖离20={r['b20']}% 20日={r['r20']}% 60日回撤={r['drawdown60']}%")

    out = {'gate':gate,'gate_open':gate_open,'total':len(results),'top':top,'all':results}
    with open('screen_result_v3.json','w',encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'\n结果已存 screen_result_v3.json（共{len(results)}只，Top20见上）')
