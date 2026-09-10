# scripts/run_tracks.py
# pip install -r requirements.txt
# usage: python scripts/run_tracks.py --config config/tracks.yml

import os, time, argparse
from pathlib import Path
import requests, pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import yaml, plotly.express as px
from scripts.report_utils import fig_to_div, make_track_page, write_page

CG = "https://api.coingecko.com/api/v3"

@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=30),
       retry=retry_if_exception_type(requests.exceptions.RequestException))
def cg_get(path, params=None):
    r = requests.get(f"{CG}/{path}", params=params, timeout=60)
    if r.status_code == 429:
        time.sleep(10)
        raise requests.exceptions.RequestException("429")
    r.raise_for_status()
    return r.json()

def cg_history(coin_id, days=1095, sleep_sec=0.6):
    j = cg_get(f"coins/{coin_id}/market_chart", params={"vs_currency":"usd","days":days,"interval":"daily"})
    dfp = pd.DataFrame(j["prices"], columns=["ts","price"])
    dfm = pd.DataFrame(j["market_caps"], columns=["ts","mcap"])
    df = dfp.merge(dfm, on="ts")
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.date
    df = df[["date","price","mcap"]].dropna()
    time.sleep(sleep_sec)
    return df

def dl_json(url):
    r = requests.get(url, timeout=60); r.raise_for_status()
    return r.json()

def dl_defi_total():
    j = dl_json("https://api.llama.fi/charts/defi")
    df = pd.DataFrame(j)
    df["date"] = pd.to_datetime(df["date"], unit="s", utc=True).dt.date
    return df.rename(columns={"totalLiquidityUSD":"defi_total_tvl"})[["date","defi_total_tvl"]]

def dl_protocols():
    return dl_json("https://api.llama.fi/protocols")

def dl_protocol_history(slug):
    j = dl_json(f"https://api.llama.fi/protocol/{slug}")
    rows = []
    for pt in j.get("tvl", []):
        rows.append({"date": pd.to_datetime(pt["date"], unit="s", utc=True).date(),
                     "tvl": pt.get("totalLiquidityUSD",0)})
    return pd.DataFrame(rows)

def dl_stablecoins_total_and_hist():
    j = dl_json("https://stablecoins.llama.fi/stablecoins?includePrices=true")
    rows_total, rows_coins = [], []
    for a in j["peggedAssets"]:
        sym = a.get("symbol")
        hist = a.get("historicalCirculating") or {}
        for k, v in hist.items():
            d = pd.to_datetime(k).date()
            amt = float(v)
            rows_total.append({"date": d, "amount": amt})
            rows_coins.append({"date": d, "symbol": sym, "amount": amt})
    total = pd.DataFrame(rows_total).groupby("date", as_index=False)["amount"].sum().rename(columns={"amount":"stable_total"})
    coins = pd.DataFrame(rows_coins)
    return total, coins

def dl_rwa_total_and_projects():
    total = pd.DataFrame(dl_json("https://api.llama.fi/charts/rwa"))
    total["date"] = pd.to_datetime(total["date"], unit="s", utc=True).dt.date
    total = total.rename(columns={"totalLiquidityUSD":"rwa_total"})[["date","rwa_total"]]
    # RWA 项目列表（兜底用 /protocols 过滤）
    try:
        projs = dl_json("https://api.llama.fi/rwa")
        plist = projs.get("projects", [])
        # 标准化字段
        norm = [{"name": p.get("name"), "slug": p.get("slug") or p.get("name","").lower().replace(" ","-"), "tvl": p.get("tvl",0)} for p in plist]
    except:
        plist = [p for p in dl_protocols() if p.get("category","").upper()=="RWA"]
        norm = [{"name": p.get("name"), "slug": p.get("slug") or p.get("name","").lower().replace(" ","-"), "tvl": p.get("tvl",0)} for p in plist]
    pdf = pd.DataFrame([x for x in norm if x["tvl"] is not None])
    return total, pdf

def run_chains(conf, lookback_days, top_k, out_dir):
    ids = conf["selection"]["ids"]
    include_price = conf.get("plot",{}).get("include_price_for_top", False)
    series, tops = [], []
    for cid in ids:
        try:
            df = cg_history(cid, days=lookback_days, sleep_sec=conf.get("cg_sleep_sec", 0.6))
            df["id"] = cid
            series.append(df)
            tops.append({"id": cid, "mcap": df["mcap"].iloc[-1] if len(df) else 0})
        except Exception as e:
            print("chains history fail:", cid, e)
    if not series:
        return
    allh = pd.concat(series, ignore_index=True)
    agg = allh.groupby("date", as_index=False)["mcap"].sum().rename(columns={"mcap":"chains_total_mcap"})
    # 构建页面
    charts = []
    fig_total = px.line(agg, x="date", y="chains_total_mcap", title=f"{conf['name']} - Total Mcap (3Y)")
    charts.append(("Total Mcap", fig_to_div(fig_total)))
    t5 = pd.DataFrame(tops).sort_values("mcap", ascending=False).head(top_k)["id"].tolist()
    for cid in t5:
        ad = allh[allh["id"]==cid].sort_values("date")
        figm = px.line(ad, x="date", y="mcap", title=f"{cid} - Market Cap (3Y)")
        charts.append((f"{cid} - Mcap", fig_to_div(figm)))
        if include_price:
            figp = px.line(ad, x="date", y="price", title=f"{cid} - Price (3Y)")
            charts.append((f"{cid} - Price", fig_to_div(figp)))
    html = make_track_page(conf["name"], charts, footer_note="Chains mcap from CoinGecko")
    write_page(Path(out_dir) / f"{conf['name']}.html", html)

def run_defi_total(conf, lookback_days, top_k, out_dir):
    total = dl_defi_total()
    if total.empty: return
    # 限 3年窗口
    total = total[total["date"] >= (total["date"].max() - pd.Timedelta(days=lookback_days))]
    # Top5 协议
    prots = dl_protocols()
    df = pd.DataFrame([{"name": p["name"], "slug": p["slug"], "tvl": p.get("tvl",0), "category": p.get("category","")} for p in prots])
    df = df[df["tvl"].notna()]
    # 宽泛 DeFi 大类
    defi_cats = set(["Lending","Dexes","CDP","Derivatives","Yield","Staking","Assets","Insurance","Services"])
    df = df[df["category"].str.title().isin(defi_cats)]
    top = df.sort_values("tvl", ascending=False).head(top_k)
    charts = []
    fig_total = px.line(total, x="date", y="defi_total_tvl", title=f"{conf['name']} - Total TVL (3Y)")
    charts.append(("Total TVL", fig_to_div(fig_total)))
    for _, r in top.iterrows():
        hist = dl_protocol_history(r["slug"])
        if hist.empty: continue
        hist = hist[hist["date"] >= (hist["date"].max() - pd.Timedelta(days=lookback_days))]
        figp = px.line(hist.sort_values("date"), x="date", y="tvl", title=f"{r['name']} - TVL (3Y)")
        charts.append((f"{r['name']} - TVL", fig_to_div(figp)))
    html = make_track_page(conf["name"], charts, footer_note="DeFi TVL from DeFiLlama")
    write_page(Path(out_dir) / f"{conf['name']}.html", html)

def run_stablecoins(conf, lookback_days, top_k, out_dir):
    total, coins = dl_stablecoins_total_and_hist()
    if not total.empty:
        total = total[total["date"] >= (total["date"].max() - pd.Timedelta(days=lookback_days))]
    charts = []
    fig_total = px.line(total, x="date", y="stable_total", title=f"{conf['name']} - Total (3Y)")
    charts.append(("Total Supply/Cap", fig_to_div(fig_total)))
    if not coins.empty:
        bl = set(conf["selection"].get("blacklist_symbols", []))
        latest = coins[coins["date"]==coins["date"].max()].groupby("symbol", as_index=False)["amount"].sum()
        t5 = latest[~latest["symbol"].isin(bl)].sort_values("amount", ascending=False).head(top_k)["symbol"].tolist()
        for sym in t5:
            df = coins[coins["symbol"]==sym]
            df = df[df["date"] >= (df["date"].max() - pd.Timedelta(days=lookback_days))]
            fig = px.line(df, x="date", y="amount", title=f"{sym} - Supply/Cap (3Y)")
            charts.append((f"{sym} - Supply", fig_to_div(fig)))
    html = make_track_page(conf["name"], charts, footer_note="Stablecoins from DeFiLlama")
    write_page(Path(out_dir) / f"{conf['name']}.html", html)

def run_rwa(conf, lookback_days, top_k, out_dir):
    total, proj_df = dl_rwa_total_and_projects()
    if not total.empty:
        total = total[total["date"] >= (total["date"].max() - pd.Timedelta(days=lookback_days))]
    charts = []
    fig_total = px.line(total, x="date", y="rwa_total", title=f"{conf['name']} - Total (3Y)")
    charts.append(("Total TVL", fig_to_div(fig_total)))
    if not proj_df.empty:
        top = proj_df.sort_values("tvl", ascending=False).head(top_k)
        for _, r in top.iterrows():
            hist = dl_protocol_history(r["slug"])
            if hist.empty: continue
            hist = hist[hist["date"] >= (hist["date"].max() - pd.Timedelta(days=lookback_days))]
            fig = px.line(hist.sort_values("date"), x="date", y="tvl", title=f"{r['name']} - TVL (3Y)")
            charts.append((f"{r['name']} - TVL", fig_to_div(fig)))
    html = make_track_page(conf["name"], charts, footer_note="RWA from DeFiLlama")
    write_page(Path(out_dir) / f"{conf['name']}.html", html)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="config/tracks.yml")
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    g = cfg["global"]; tracks = cfg["tracks"]
    lookback = g.get("lookback_days", 1095)
    top_k = g.get("top_k", 5)
    out_dir = g.get("output_dir", "docs/tracks")
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    for t in tracks:
        name = t["name"]
        print(f"== Running track: {name} ==")
        src = t["source"].lower()
        if src == "coingecko":
            run_chains(t, lookback, top_k, out_dir)
        elif src == "defillama_defi_total":
            run_defi_total(t, lookback, top_k, out_dir)
        elif src == "defillama_stablecoins":
            run_stablecoins(t, lookback, top_k, out_dir)
        elif src == "defillama_rwa":
            run_rwa(t, lookback, top_k, out_dir)
        else:
            print(f"Source {src} not supported in minimal skeleton.")
    print("All tracks done.")

if __name__ == "__main__":
    from pathlib import Path
    main()
