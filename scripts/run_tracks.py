# scripts/run_tracks.py
# pip install -r requirements.txt
# usage: python -m scripts.run_tracks --config config/tracks.yml

import os, time, argparse
from pathlib import Path
import requests, pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import yaml, plotly.express as px

from scripts.report_utils import fig_to_div, make_track_page, write_page

# ============ Config & Constants ============
CG_BASE = "https://api.coingecko.com/api/v3"
CG_API_KEY = os.getenv("COINGECKO_API_KEY")  # 可选：CoinGecko 免费/付费 Key
UA = "crypto-tracks-bot/1.0 (+github-actions)"

# ============ HTTP helpers with retry/backoff ============

@retry(stop=stop_after_attempt(6),
       wait=wait_exponential(multiplier=1, min=2, max=90),
       retry=retry_if_exception_type(requests.exceptions.RequestException))
def cg_get(path, params=None):
    headers = {
        "Accept": "application/json",
        "User-Agent": UA
    }
    if CG_API_KEY:
        # CoinGecko demo/enterprise header（按你的 key 类型对应 header；常见为 x-cg-demo-api-key）
        headers["x-cg-demo-api-key"] = CG_API_KEY
    url = f"{CG_BASE}/{path}"
    r = requests.get(url, params=params, headers=headers, timeout=60)
    if r.status_code in (429, 403, 502, 503):
        # 退避重试
        time.sleep(5)
        raise requests.exceptions.RequestException(f"CG status {r.status_code}")
    r.raise_for_status()
    return r.json()

@retry(stop=stop_after_attempt(6),
       wait=wait_exponential(multiplier=1, min=2, max=60),
       retry=retry_if_exception_type(requests.exceptions.RequestException))
def dl_json(url):
    headers = {"Accept": "application/json", "User-Agent": UA}
    r = requests.get(url, headers=headers, timeout=60)
    if r.status_code in (429, 403, 502, 503):
        time.sleep(5)
        raise requests.exceptions.RequestException(f"DL status {r.status_code}")
    r.raise_for_status()
    return r.json()

# ============ Data fetchers ============

def cg_history(coin_id, days=1095, sleep_sec=1.0):
    j = cg_get(f"coins/{coin_id}/market_chart",
               params={"vs_currency": "usd", "days": days, "interval": "daily"})
    dfp = pd.DataFrame(j.get("prices", []), columns=["ts", "price"])
    dfm = pd.DataFrame(j.get("market_caps", []), columns=["ts", "mcap"])
    if dfp.empty or dfm.empty:
        return pd.DataFrame(columns=["date","price","mcap"])
    df = dfp.merge(dfm, on="ts", how="inner")
    if df.empty:
        return pd.DataFrame(columns=["date","price","mcap"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.date
    df = df[["date", "price", "mcap"]].dropna()
    time.sleep(sleep_sec)
    return df

def dl_defi_total():
    # DeFi 全网 TVL 总曲线
    j = dl_json("https://api.llama.fi/charts/defi")
    df = pd.DataFrame(j)
    if df.empty:
        return pd.DataFrame(columns=["date","defi_total_tvl"])
    df["date"] = pd.to_datetime(df["date"], unit="s", utc=True).dt.date
    return df.rename(columns={"totalLiquidityUSD": "defi_total_tvl"})[["date", "defi_total_tvl"]]

def dl_protocols():
    return dl_json("https://api.llama.fi/protocols")

def dl_protocol_history(slug):
    j = dl_json(f"https://api.llama.fi/protocol/{slug}")
    rows = []
    for pt in j.get("tvl", []):
        rows.append({
            "date": pd.to_datetime(pt.get("date"), unit="s", utc=True).date(),
            "tvl": pt.get("totalLiquidityUSD", 0)
        })
    df = pd.DataFrame(rows)
    return df

def dl_stablecoins_total_and_hist():
    # 兼容 historicalCirculating 为 dict 和 circulating 为 list 两种结构
    try:
        j = dl_json("https://stablecoins.llama.fi/stablecoins?includePrices=true")
    except Exception:
        return (pd.DataFrame(columns=["date", "stable_total"]),
                pd.DataFrame(columns=["date", "symbol", "amount"]))

    if not j or "peggedAssets" not in j:
        return (pd.DataFrame(columns=["date", "stable_total"]),
                pd.DataFrame(columns=["date", "symbol", "amount"]))

    rows_total, rows_coins = [], []
    for a in j["peggedAssets"]:
        sym = a.get("symbol")
        if not sym:
            continue

        # 1) 历史为 dict: {"YYYY-MM-DD": amount, ...}
        hist = a.get("historicalCirculating")
        if isinstance(hist, dict) and hist:
            for k, v in hist.items():
                d = None
                try:
                    d = pd.to_datetime(k).date()
                except Exception:
                    try:
                        d = pd.to_datetime(int(k), unit="s", utc=True).date()
                    except Exception:
                        continue
                amt = float(v) if v is not None else 0.0
                rows_total.append({"date": d, "amount": amt})
                rows_coins.append({"date": d, "symbol": sym, "amount": amt})

        # 2) 历史为 list: [{"date": 1705814400, "circulating": ...}, ...]
        circ_list = a.get("circulating")
        if isinstance(circ_list, list) and circ_list:
            for pt in circ_list:
                ts = pt.get("date")
                val = pt.get("circulating") or pt.get("amount") or pt.get("value")
                if ts is None or val is None:
                    continue
                d = pd.to_datetime(ts, unit="s", utc=True).date() if isinstance(ts, (int, float)) else pd.to_datetime(ts).date()
                amt = float(val)
                rows_total.append({"date": d, "amount": amt})
                rows_coins.append({"date": d, "symbol": sym, "amount": amt})

    df_total = pd.DataFrame(rows_total)
    if df_total.empty:
        return (pd.DataFrame(columns=["date", "stable_total"]),
                pd.DataFrame(columns=["date", "symbol", "amount"]))

    total = (df_total.groupby("date", as_index=False)["amount"]
                     .sum()
                     .rename(columns={"amount": "stable_total"}))
    coins = pd.DataFrame(rows_coins)
    return total, coins

def dl_rwa_total_and_projects():
    # 总 TVL
    try:
        total = pd.DataFrame(dl_json("https://api.llama.fi/charts/rwa"))
    except Exception:
        total = pd.DataFrame()
    if total.empty:
        total = pd.DataFrame(columns=["date","rwa_total"])
    else:
        total["date"] = pd.to_datetime(total["date"], unit="s", utc=True).dt.date
        total = total.rename(columns={"totalLiquidityUSD": "rwa_total"})[["date", "rwa_total"]]

    # 项目列表
    plist = []
    try:
        projs = dl_json("https://api.llama.fi/rwa")
        if isinstance(projs, dict) and "projects" in projs:
            plist = projs["projects"]
    except Exception:
        pass

    if not plist:
        # 兜底：从 protocols 里筛选 category=RWA
        try:
            allp = dl_protocols()
            plist = [p for p in allp if str(p.get("category", "")).upper() == "RWA"]
        except Exception:
            plist = []

    norm = []
    for p in plist:
        name = p.get("name")
        slug = p.get("slug") or (name or "").lower().replace(" ", "-")
        tvl = p.get("tvl", 0) if p.get("tvl", 0) is not None else 0
        if name:
            norm.append({"name": name, "slug": slug, "tvl": tvl})

    pdf = pd.DataFrame(norm)
    return total, pdf

# ============ Track runners & plotting ============

def run_chains(conf, lookback_days, top_k, out_dir):
    ids = conf["selection"]["ids"]
    include_price = conf.get("plot", {}).get("include_price_for_top", False)
    cg_sleep = conf.get("cg_sleep_sec", 1.0)

    series, tops = [], []
    for cid in ids:
        try:
            df = cg_history(cid, days=lookback_days, sleep_sec=cg_sleep)
            if df.empty:
                print(f"[WARN] chains empty history: {cid}")
                continue
            df["id"] = cid
            series.append(df)
            tops.append({"id": cid, "mcap": df["mcap"].iloc[-1] if len(df) else 0})
        except Exception as e:
            print("chains history fail:", cid, e)

    charts = []
    if series:
        allh = pd.concat(series, ignore_index=True)
        agg = allh.groupby("date", as_index=False)["mcap"].sum().rename(columns={"mcap": "chains_total_mcap"})
        fig_total = px.line(agg, x="date", y="chains_total_mcap", title=f"{conf['name']} - Total Mcap (3Y)")
        charts.append(("Total Mcap", fig_to_div(fig_total)))

        t5 = pd.DataFrame(tops).sort_values("mcap", ascending=False).head(top_k)["id"].tolist()
        for cid in t5:
            ad = allh[allh["id"] == cid].sort_values("date")
            if ad.empty:
                continue
            figm = px.line(ad, x="date", y="mcap", title=f"{cid} - Market Cap (3Y)")
            charts.append((f"{cid} - Mcap", fig_to_div(figm)))
            if include_price:
                figp = px.line(ad, x="date", y="price", title=f"{cid} - Price (3Y)")
                charts.append((f"{cid} - Price", fig_to_div(figp)))
    else:
        # 占位图
        placeholder = px.line(pd.DataFrame({"date": [], "value": []}), x="date", y="value",
                              title=f"{conf['name']} - No data (API limited?)")
        charts.append(("No Data", fig_to_div(placeholder)))

    html = make_track_page(conf["name"], charts, footer_note="Chains mcap from CoinGecko")
    write_page(Path(out_dir) / f"{conf['name']}.html", html)

def run_defi_total(conf, lookback_days, top_k, out_dir):
    charts = []
    try:
        total = dl_defi_total()
        if not total.empty:
            total = total[total["date"] >= (total["date"].max() - pd.Timedelta(days=lookback_days))]
            fig_total = px.line(total, x="date", y="defi_total_tvl", title=f"{conf['name']} - Total TVL (3Y)")
            charts.append(("Total TVL", fig_to_div(fig_total)))
        else:
            raise ValueError("empty total")
    except Exception as e:
        print("[WARN] DeFi total fail:", e)
        placeholder = px.line(pd.DataFrame({"date": [], "value": []}), x="date", y="value",
                              title=f"{conf['name']} - No data")
        charts.append(("No Data", fig_to_div(placeholder)))

    # Top5 协议
    try:
        prots = dl_protocols()
        df = pd.DataFrame([{
            "name": p.get("name"),
            "slug": p.get("slug"),
            "tvl": p.get("tvl", 0),
            "category": p.get("category", "")
        } for p in prots if p.get("name")])
        df = df[df["tvl"].notna()]
        defi_cats = set(["Lending", "Dexes", "CDP", "Derivatives", "Yield", "Staking", "Assets", "Insurance", "Services"])
        df = df[df["category"].str.title().isin(defi_cats)]
        top = df.sort_values("tvl", ascending=False).head(top_k)
        for _, r in top.iterrows():
            hist = dl_protocol_history(r["slug"])
            if hist.empty: 
                continue
            hist = hist[hist["date"] >= (hist["date"].max() - pd.Timedelta(days=lookback_days))]
            figp = px.line(hist.sort_values("date"), x="date", y="tvl", title=f"{r['name']} - TVL (3Y)")
            charts.append((f"{r['name']} - TVL", fig_to_div(figp)))
    except Exception as e:
        print("[WARN] DeFi top protocols fail:", e)

    html = make_track_page(conf["name"], charts, footer_note="DeFi TVL from DeFiLlama")
    write_page(Path(out_dir) / f"{conf['name']}.html", html)

def run_stablecoins(conf, lookback_days, top_k, out_dir):
    charts = []
    try:
        total, coins = dl_stablecoins_total_and_hist()
        if not total.empty:
            total = total[total["date"] >= (total["date"].max() - pd.Timedelta(days=lookback_days))]
            fig_total = px.line(total, x="date", y="stable_total", title=f"{conf['name']} - Total (3Y)")
            charts.append(("Total Supply/Cap", fig_to_div(fig_total)))
        else:
            raise ValueError("empty total")
        # Top5
        if not coins.empty:
            bl = set(conf.get("selection", {}).get("blacklist_symbols", []))
            latest = coins[coins["date"] == coins["date"].max()].groupby("symbol", as_index=False)["amount"].sum()
            t5 = latest[~latest["symbol"].isin(bl)].sort_values("amount", ascending=False).head(top_k)["symbol"].tolist()
            for sym in t5:
                df = coins[coins["symbol"] == sym]
                if df.empty: 
                    continue
                df = df[df["date"] >= (df["date"].max() - pd.Timedelta(days=lookback_days))]
                fig = px.line(df, x="date", y="amount", title=f"{sym} - Supply/Cap (3Y)")
                charts.append((f"{sym} - Supply", fig_to_div(fig)))
    except Exception as e:
        print("[WARN] Stablecoins fail:", e)
        placeholder = px.line(pd.DataFrame({"date": [], "value": []}), x="date", y="value",
                              title=f"{conf['name']} - No data")
        charts.append(("No Data", fig_to_div(placeholder)))

    html = make_track_page(conf["name"], charts, footer_note="Stablecoins from DeFiLlama")
    write_page(Path(out_dir) / f"{conf['name']}.html", html)

def run_rwa(conf, lookback_days, top_k, out_dir):
    charts = []
    try:
        total, proj_df = dl_rwa_total_and_projects()
        if not total.empty:
            total = total[total["date"] >= (total["date"].max() - pd.Timedelta(days=lookback_days))]
            fig_total = px.line(total, x="date", y="rwa_total", title=f"{conf['name']} - Total (3Y)")
            charts.append(("Total TVL", fig_to_div(fig_total)))
        else:
            raise ValueError("empty total")

        if not proj_df.empty:
            top = proj_df.sort_values("tvl", ascending=False).head(top_k)
            for _, r in top.iterrows():
                hist = dl_protocol_history(r["slug"])
                if hist.empty: 
                    continue
                hist = hist[hist["date"] >= (hist["date"].max() - pd.Timedelta(days=lookback_days))]
                fig = px.line(hist.sort_values("date"), x="date", y="tvl", title=f"{r['name']} - TVL (3Y)")
                charts.append((f"{r['name']} - TVL", fig_to_div(fig)))
    except Exception as e:
        print("[WARN] RWA fail:", e)
        placeholder = px.line(pd.DataFrame({"date": [], "value": []}), x="date", y="value",
                              title=f"{conf['name']} - No data")
        charts.append(("No Data", fig_to_div(placeholder)))

    html = make_track_page(conf["name"], charts, footer_note="RWA from DeFiLlama")
    write_page(Path(out_dir) / f"{conf['name']}.html", html)

# ============ Main ============

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
        try:
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
                print(f"[WARN] Source {src} not supported.")
        except Exception as e:
            print(f"[WARN] Track {name} failed: {e}")
            # 即使某赛道失败，也不中断其它赛道
            try:
                # 写一个空的占位页，避免 index 生成后链接 404
                placeholder = px.line(pd.DataFrame({"date": [], "value": []}), x="date", y="value",
                                      title=f"{name} - No data")
                html = make_track_page(name, [("No Data", fig_to_div(placeholder))], footer_note="Fetch failed")
                write_page(Path(out_dir) / f"{name}.html", html)
            except Exception:
                pass
            continue

    print("All tracks done.")

if __name__ == "__main__":
    main()

