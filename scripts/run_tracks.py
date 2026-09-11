# scripts/run_tracks.py
# usage: python -m scripts.run_tracks --config config/tracks.yml

import time, argparse
from pathlib import Path
import requests, pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import yaml, plotly.express as px
from scripts.report_utils import fig_to_div, make_track_page, write_page

UA = "crypto-tracks-bot/1.0 (+github-actions)"

def warn(msg): print(f"[WARN] {msg}")

@retry(stop=stop_after_attempt(6),
       wait=wait_exponential(multiplier=1, min=2, max=60),
       retry=retry_if_exception_type(requests.exceptions.RequestException))
def dl_json(url, params=None):
    headers = {"Accept":"application/json","User-Agent":UA}
    r = requests.get(url, headers=headers, params=params, timeout=60)
    if r.status_code in (429,403,502,503):
        time.sleep(5); raise requests.exceptions.RequestException(f"DL status {r.status_code}")
    r.raise_for_status()
    return r.json()

# -------- Stablecoins --------
def dl_stablecoins_total_and_hist():
    # 优先 overview，拿总量时间线
    try:
        j = dl_json("https://stablecoins.llama.fi/overview")
        total = None
        if isinstance(j, dict) and "total" in j:
            tot = j["total"]
            series = None
            for k in ["chart","circulating","circulatingUSD","totalCirculatingUSD"]:
                v = tot.get(k)
                if isinstance(v, list) and v:
                    series = v; break
            if series:
                rows=[]
                first=series[0]
                if isinstance(first, dict):
                    for pt in series:
                        ts = pt.get("date")
                        val = pt.get("totalCirculatingUSD") or pt.get("circulatingUSD") or pt.get("circulating") or pt.get("value")
                        if ts is None or val is None: continue
                        d = pd.to_datetime(ts, unit="s", utc=True).date() if isinstance(ts,(int,float)) else pd.to_datetime(ts).date()
                        rows.append({"date": d, "stable_total": float(val)})
                    total = pd.DataFrame(rows)
                else:
                    df = pd.DataFrame(series, columns=["ts","val"])
                    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
                    total = df[["date"]].assign(stable_total=df["val"].astype(float))
        if isinstance(total, pd.DataFrame) and not total.empty:
            coins = dl_stablecoins_coins_hist_legacy()
            return total, coins
    except Exception as e:
        warn(f"stablecoins overview failed: {e}")
    # 兜底：legacy 汇总
    return dl_stablecoins_coins_hist_legacy_total()

def dl_stablecoins_coins_hist_legacy():
    try:
        j = dl_json("https://stablecoins.llama.fi/stablecoins?includePrices=true")
    except Exception as e:
        warn(f"stablecoins legacy list failed: {e}")
        return pd.DataFrame(columns=["date","symbol","amount"])
    rows=[]
    for a in j.get("peggedAssets", []):
        sym=a.get("symbol"); 
        if not sym: continue
        hist=a.get("historicalCirculating")
        if isinstance(hist, dict) and hist:
            for k,v in hist.items():
                try: d=pd.to_datetime(k).date()
                except: 
                    try: d=pd.to_datetime(int(k), unit="s", utc=True).date()
                    except: continue
                rows.append({"date": d, "symbol": sym, "amount": float(v or 0)})
        circ=a.get("circulating")
        if isinstance(circ, list) and circ:
            for pt in circ:
                ts=pt.get("date"); val=pt.get("circulating") or pt.get("amount") or pt.get("value")
                if ts is None or val is None: continue
                d = pd.to_datetime(ts, unit="s", utc=True).date() if isinstance(ts,(int,float)) else pd.to_datetime(ts).date()
                rows.append({"date": d, "symbol": sym, "amount": float(val)})
    return pd.DataFrame(rows)

def dl_stablecoins_coins_hist_legacy_total():
    coins = dl_stablecoins_coins_hist_legacy()
    if coins.empty:
        return pd.DataFrame(columns=["date","stable_total"]), coins
    total = coins.groupby("date", as_index=False)["amount"].sum().rename(columns={"amount":"stable_total"})
    return total, coins

# -------- DeFi Total --------
def dl_defi_total():
    # 新端点优先
    try:
        j = dl_json("https://api.llama.fi/overview/defi",
                    params={"excludeTotalChart":"false","excludeProtocolChart":"true","dataType":"daily"})
        chart = (j.get("totalDataChart") or j.get("totalChart")) or []
        if chart:
            df = pd.DataFrame(chart, columns=["ts","tvl"])
            df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
            return df[["date","tvl"]].rename(columns={"tvl":"defi_total_tvl"})
    except Exception as e:
        warn(f"overview/defi failed: {e}")
    # 旧端点兜底
    try:
        j = dl_json("https://api.llama.fi/charts/defi")
        df = pd.DataFrame(j)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"], unit="s", utc=True).dt.date
            return df.rename(columns={"totalLiquidityUSD":"defi_total_tvl"})[["date","defi_total_tvl"]]
    except Exception as e:
        warn(f"charts/defi failed: {e}")
    return pd.DataFrame(columns=["date","defi_total_tvl"])

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
    return pd.DataFrame(rows)

# -------- RWA --------
def dl_rwa_total_and_projects():
    # total
    try:
        total = pd.DataFrame(dl_json("https://api.llama.fi/charts/rwa"))
        total["date"] = pd.to_datetime(total["date"], unit="s", utc=True).dt.date
        total = total.rename(columns={"totalLiquidityUSD":"rwa_total"})[["date","rwa_total"]]
    except Exception as e:
        warn(f"rwa total failed: {e}")
        total = pd.DataFrame(columns=["date","rwa_total"])
    # list
    plist=[]
    try:
        projs = dl_json("https://api.llama.fi/rwa")
        if isinstance(projs, dict) and "projects" in projs:
            plist = projs["projects"]
    except: pass
    if not plist:
        try:
            allp = dl_protocols()
            plist = [p for p in allp if str(p.get("category","")).upper()=="RWA"]
        except Exception as e:
            warn(f"rwa list fallback failed: {e}"); plist=[]
    norm=[]
    for p in plist:
        name = p.get("name"); 
        if not name: continue
        slug = p.get("slug") or name.lower().replace(" ","-")
        tvl  = p.get("tvl", 0) or 0
        norm.append({"name": name, "slug": slug, "tvl": tvl})
    return total, pd.DataFrame(norm)

# -------- Chains (TVL) --------
def dl_chains_overview():
    return dl_json("https://api.llama.fi/overview/chains",
                   params={"excludeTotalChart":"false","excludeChains":"true","dataType":"daily"})

def run_chains_tvl(conf, lookback_days, top_k, out_dir):
    charts=[]
    try:
        ov = dl_chains_overview()
        total_chart = ov.get("totalDataChart") or ov.get("totalChart") or []
        if total_chart:
            df = pd.DataFrame(total_chart, columns=["ts","tvl"])
            df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
            df = df[df["date"] >= (df["date"].max() - pd.Timedelta(days=lookback_days))]
            charts.append(("Total Chain TVL", fig_to_div(px.line(df, x="date", y="tvl",
                                                          title=f"{conf['name']} - Total Chain TVL (3Y)"))))
        else:
            charts.append(("No Data", fig_to_div(px.line(pd.DataFrame({"date":[],"value":[]}), x="date", y="value",
                                                          title=f"{conf['name']} - No total TVL data"))))
        chains = ov.get("chains") or []
        cdf = pd.DataFrame([{"name": c.get("name"), "tvl": c.get("tvl",0)} for c in chains if c.get("name")])
        if not cdf.empty:
            wl = conf.get("selection", {}).get("whitelist")
            if wl: cdf = cdf[cdf["name"].isin(wl)]
            top = cdf.sort_values("tvl", ascending=False).head(top_k)
            for _, r in top.iterrows():
                chain_name = r["name"]
                try:
                    hc = dl_json(f"https://api.llama.fi/charts/{chain_name}")
                    hdf = pd.DataFrame(hc)
                    if hdf.empty: continue
                    if "date" in hdf.columns and "totalLiquidityUSD" in hdf.columns:
                        hdf["date"] = pd.to_datetime(hdf["date"], unit="s", utc=True).dt.date
                        hdf = hdf.rename(columns={"totalLiquidityUSD":"tvl"})
                    elif "tvl" in hdf.columns and "date" in hdf.columns:
                        hdf["date"] = pd.to_datetime(hdf["date"], unit="s", utc=True).dt.date
                    else:
                        if len(hdf.columns)>=2:
                            hdf.columns = ["ts","tvl"] + list(hdf.columns[2:])
                            hdf["date"] = pd.to_datetime(hdf["ts"], unit="s", utc=True).dt.date
                        else:
                            continue
                    hdf = hdf[["date","tvl"]]
                    hdf = hdf[hdf["date"] >= (hdf["date"].max() - pd.Timedelta(days=lookback_days))]
                    charts.append((f"{chain_name} - TVL", fig_to_div(px.line(hdf.sort_values("date"),
                                                                             x="date", y="tvl",
                                                                             title=f"{chain_name} - TVL (3Y)"))))
                except Exception as e:
                    warn(f"chain chart fail {chain_name}: {e}")
    except Exception as e:
        warn(f"chains overview failed: {e}")
        charts.append(("No Data", fig_to_div(px.line(pd.DataFrame({"date":[],"value":[]}), x="date", y="value",
                                                     title=f"{conf['name']} - No data"))))
    html = make_track_page(conf["name"], charts, footer_note="Chains TVL from DeFiLlama")
    write_page(Path(out_dir) / f"{conf['name']}.html", html)

# -------- Orchestrator --------
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
        warn(f"DeFi total fail: {e}")
        charts.append(("No Data", fig_to_div(px.line(pd.DataFrame({"date":[],"value":[]}), x="date", y="value",
                                                     title=f"{conf['name']} - No data"))))

    try:
        prots = dl_protocols()
        df = pd.DataFrame([{
            "name": p.get("name"),
            "slug": p.get("slug"),
            "tvl": p.get("tvl", 0),
            "category": p.get("category", "")
        } for p in prots if p.get("name")])
        df = df[df["tvl"].notna()]
        defi_cats = set(["Lending","Dexes","CDP","Derivatives","Yield","Staking","Assets","Insurance","Services"])
        df = df[df["category"].str.title().isin(defi_cats)]
        top = df.sort_values("tvl", ascending=False).head(top_k)
        for _, r in top.iterrows():
            hist = dl_protocol_history(r["slug"])
            if hist.empty: continue
            hist = hist[hist["date"] >= (hist["date"].max() - pd.Timedelta(days=lookback_days))]
            figp = px.line(hist.sort_values("date"), x="date", y="tvl", title=f"{r['name']} - TVL (3Y)")
            charts.append((f"{r['name']} - TVL", fig_to_div(figp)))
    except Exception as e:
        warn(f"DeFi top protocols fail: {e}")

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
        if not coins.empty:
            bl = set(conf.get("selection", {}).get("blacklist_symbols", []))
            latest = coins[coins["date"]==coins["date"].max()].groupby("symbol", as_index=False)["amount"].sum()
            t5 = latest[~latest["symbol"].isin(bl)].sort_values("amount", ascending=False).head(top_k)["symbol"].tolist()
            for sym in t5:
                df = coins[coins["symbol"]==sym]
                if df.empty: continue
                df = df[df["date"] >= (df["date"].max() - pd.Timedelta(days=lookback_days))]
                fig = px.line(df, x="date", y="amount", title=f"{sym} - Supply/Cap (3Y)")
                charts.append((f"{sym} - Supply", fig_to_div(fig)))
    except Exception as e:
        warn(f"Stablecoins fail: {e}")
        charts.append(("No Data", fig_to_div(px.line(pd.DataFrame({"date":[],"value":[]}), x="date", y="value",
                                                     title=f"{conf['name']} - No data"))))

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
                if hist.empty: continue
                hist = hist[hist["date"] >= (hist["date"].max() - pd.Timedelta(days=lookback_days))]
                fig = px.line(hist.sort_values("date"), x="date", y="tvl", title=f"{r['name']} - TVL (3Y)")
                charts.append((f"{r['name']} - TVL", fig_to_div(fig)))
    except Exception as e:
        warn(f"RWA fail: {e}")
        charts.append(("No Data", fig_to_div(px.line(pd.DataFrame({"date":[],"value":[]}), x="date", y="value",
                                                     title=f"{conf['name']} - No data"))))

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
        try:
            if src == "defillama_chains":
                run_chains_tvl(t, lookback, top_k, out_dir)
            elif src == "defillama_defi_total":
                run_defi_total(t, lookback, top_k, out_dir)
            elif src == "defillama_stablecoins":
                run_stablecoins(t, lookback, top_k, out_dir)
            elif src == "defillama_rwa":
                run_rwa(t, lookback, top_k, out_dir)
            else:
                warn(f"Source {src} not supported in DL-only mode.")
                placeholder = px.line(pd.DataFrame({"date":[],"value":[]}), x="date", y="value",
                                      title=f"{name} - Unsupported source")
                html = make_track_page(name, [("No Data", fig_to_div(placeholder))], footer_note="Unsupported")
                write_page(Path(out_dir) / f"{name}.html", html)
        except Exception as e:
            warn(f"Track {name} failed: {e}")
            placeholder = px.line(pd.DataFrame({"date":[],"value":[]}), x="date", y="value",
                                  title=f"{name} - Error")
            html = make_track_page(name, [("No Data", fig_to_div(placeholder))], footer_note="Error")
            write_page(Path(out_dir) / f"{name}.html", html)
            continue

    print("All tracks done.")

if __name__ == "__main__":
    main()

