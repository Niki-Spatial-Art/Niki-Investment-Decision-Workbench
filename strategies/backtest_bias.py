#!/usr/bin/env python3
"""乖离MA20因子轻量回测 —— 零账号、零依赖（纯urllib+腾讯日K）。
验证：历史上当某票"乖离MA20 ∈ 回踩区间"时买入，未来5/10/20日涨幅 vs 全市场平均，看因子是否有超额。
即回答"晶晶的选股因子到底有没有效、胜率多少"。
"""
import urllib.request, json, ssl, time, sys, random
from concurrent.futures import ThreadPoolExecutor, as_completed

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
UA = {'User-Agent':'Mozilla/5.0','Referer':'https://gu.qq.com/','Accept':'*/*'}
UT = 'fa5fd1943c7b386f172d6893dbfba10b'

def http_get(url, headers=UA, timeout=12):
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode('utf-8','ignore')

def fetch_universe(n=None):
    out = []
    fs = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
    fields = 'f2,f3,f6,f12,f14'
    pn = 1
    while True:
        qs = urllib.parse.urlencode({'pn':pn,'pz':100,'po':1,'np':1,'fltt':2,'invt':2,'fid':'f3','fs':fs,'fields':fields,'ut':UT})
        d = json.loads(http_get(f'https://push2delay.eastmoney.com/api/qt/clist/get?{qs}'))
        if not d or not d.get('data') or not d['data'].get('diff'):
            break
        out.extend(d['data']['diff'])
        if len(out) >= d['data'].get('total',0) or pn > 60:
            break
        pn += 1
        if n and len(out) >= n:
            break
    return out[:n] if n else out

def fetch_kline(code, n=250):
    sym = ('sh' if code.startswith(('6','5','9','1','2')) else 'sz') + code
    url = f'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param={sym},day,,,{n},'
    d = json.loads(http_get(url))
    k = d['data'][sym]
    arr = k.get('day') or k.get('qfqday')
    return [float(r[2]) for r in arr]

def ma(c, n):
    return sum(c[-n:])/n if len(c) >= n else None

def eval_one(item):
    code = str(item.get('f12','')).strip()
    name = str(item.get('f14','')).strip()
    if not code or not name or 'ST' in name.upper():
        return None
    try:
        closes = fetch_kline(code, 250)
    except Exception:
        return None
    if len(closes) < 80:
        return None
    # 找历史采样点：每隔约20个交易日取一次，避免同一票重复采样
    samples = []
    for i in range(20, len(closes)-20, 20):
        c = closes[i]
        m20 = sum(closes[i-19:i+1])/20
        b20 = (c/m20 - 1)*100
        # 因子命中条件：乖离MA20 ∈ [-4%, +6%]（对应 v3 回踩区间）
        if -4.0 <= b20 <= 6.0:
            fut5 = (closes[i+5]/c - 1)*100 if i+5 < len(closes) else None
            fut10 = (closes[i+10]/c - 1)*100 if i+10 < len(closes) else None
            fut20 = (closes[i+20]/c - 1)*100 if i+20 < len(closes) else None
            samples.append((b20, fut5, fut10, fut20))
        # 对照组：乖离 > 8%（追高区），看追高的下场
        if b20 > 8.0:
            fut5 = (closes[i+5]/c - 1)*100 if i+5 < len(closes) else None
            fut10 = (closes[i+10]/c - 1)*100 if i+10 < len(closes) else None
            fut20 = (closes[i+20]/c - 1)*100 if i+20 < len(closes) else None
            samples.append(('chase', fut5, fut10, fut20))
    return samples

def avg(lst):
    lst = [x for x in lst if x is not None]
    return sum(lst)/len(lst) if lst else None

def winrate(lst):
    lst = [x for x in lst if x is not None]
    if not lst: return None
    return sum(1 for x in lst if x > 0)/len(lst)*100

if __name__ == '__main__':
    print('拉取全市场列表...', file=sys.stderr)
    uni = fetch_universe()
    print(f'共{len(uni)}只，采样回测（随机抽800只代表）...', file=sys.stderr)
    random.seed(42)
    sample_uni = random.sample(uni, min(800, len(uni)))

    hit = []   # 因子命中组（回踩区间买入）
    chase = [] # 对照组（追高买入）

    with ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(eval_one, it): it for it in sample_uni}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 200 == 0:
                print(f'  进度 {done}/{len(sample_uni)}', file=sys.stderr)
            s = fut.result()
            if not s: continue
            for samp in s:
                b20, f5, f10, f20 = samp
            if b20 == 'chase':
                chase.append((0, f5, f10, f20))
            else:
                hit.append((b20, f5, f10, f20))
    print('\n===== 乖离MA20因子回测结果 =====')
    print(f'命中组(回踩区间[-4%,+6%]买入)样本数: {len(hit)}')
    print(f'对照组(追高乖离>8%买入)样本数: {len(chase)}')
    print()
    print(f"{'组别':<12}{'未来5日均涨':>12}{'5日胜率':>10}{'10日均涨':>12}{'10日胜率':>10}{'20日均涨':>12}{'20日胜率':>10}")
    for label, arr in [('命中(回踩)', hit), ('对照(追高)', chase)]:
        f5 = [x[1] for x in arr]; f10=[x[2] for x in arr]; f20=[x[3] for x in arr]
        print(f"{label:<12}{avg(f5):>11.2f}%{winrate(f5):>9.1f}%{avg(f10):>11.2f}%{winrate(f10):>9.1f}%{avg(f20):>11.2f}%{winrate(f20):>9.1f}%")

    # 超额收益
    if hit and chase:
        ex5 = avg([x[1] for x in hit]) - avg([x[1] for x in chase])
        ex20 = avg([x[3] for x in hit]) - avg([x[3] for x in chase])
        print(f'\n超额收益(回踩-追高): 5日 {ex5:+.2f}%, 20日 {ex20:+.2f}%')
    print('\n结论解读：若命中组胜率/均涨显著高于对照组，说明"回踩买点"因子有效；若接近，说明需调整区间或换因子。')
