# scripts/run_tracks.py
# usage: python -m scripts.run_tracks --config config/tracks.yml

import os, time, argparse, json
from pathlib import Path
import requests, pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import yaml, plotly.express as px
from scripts.report_utils import fig_to_div, make_track_page, write_page

UA = "crypto-tracks-bot/1.1 (+github-actions)"
DEBUG_DIR = Path("docs/debug")

def warn(msg): print(f"[WARN] {msg}")

def save_debug(obj, name):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    p = DEBUG_DIR / name
    if isinstance(obj, pd.DataFrame):
        obj.to_csv(p.with_suffix(".csv"), index=False)
    else:
        # json or str
        try:
            (p.with_suffix(".json")).write_text(json.dumps(obj)[:200000], encoding="utf-8")
        except Exception:
            (p.with_suffix(".txt")).write_text(str(obj)[:200000], encoding="utf-8")

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
        save_debug(j, "stable_overview_raw")
        total = None
        if isinstance(j, dict) and "total" in j:
            tot = j["total"]
            series = None
            for k in ["chart","circulating","circulatingUSD","totalCirculatingUSD","total"]:
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
        if isinstance(total, pd.DataFrame):
            save_debug(total, "stable_total")
        if isinstance(total, pd.DataFrame) and not total.empty:
            coins = dl_stablecoins_coins_hist_legacy()
            save_debug(coins.head(1000), "stable_coins_hist_sample")
            return total, coins
    except Exception as e:
        warn(f"stablecoins overview failed: {e}")
    # 兜底：legacy 汇总
    return dl_stablecoins_coins_hist_legacy_total()

def dl_stablecoins_coins_hist_legacy():
    try:
        j = dl_json("https://stablecoins.llama.fi/stablecoins?includePrices=true")
        save_debug({"count": len(j.get("peggedAssets", []))}, "stable_legacy_meta")
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
    coins = pd.DataFrame(rows)
    return coins

def dl_stablecoins_coins_hist_legacy_total():
    coins = dl_stablecoins_coins_hist_legacy()
    save_debug(coins.head(1000), "stable_coins_hist_legacy_sample")
    if coins.empty:
        return pd.DataFrame(columns=["date","stable_total"]), coins
    total = coins.groupby("date", as_index=False)["amount"].sum().rename(columns={"amount":"stable_total"})
    save_debug(total, "stable_total_from_legacy")
    return total, coins

# -------- DeFi Total --------
def dl_defi_total():
    try:
        j = dl_json("https://api.llama.fi/overview/defi",
                    params={"excludeTotalChart":"false","excludeProtocolChart":"true","dataType":"daily"})
        save_debug({"has_totalDataChart": bool(j.get("totalDataChart")), "len": len(j.get("totalDataChart", []))}, "defi_overview_meta")
        chart = (j.get("totalDataChart") or j.get("totalChart")) or []
        if chart:
            df = pd.DataFrame(chart, columns=["ts","tvl"])
            df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
            out = df[["date","tvl"]].rename(columns={"tvl":"defi_total_tvl"})
            save_debug(out.tail(10), "defi_total_tail")
            return out
    except Exception as e:
        warn(f"overview/defi failed: {e}")
    try:
        j = dl_json("https://api.llama.fi/charts/defi")
        df = pd.DataFrame(j)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"], unit="s", utc=True).dt.date
            out = df.rename(columns={"totalLiquidityUSD":"defi_total_tvl"})[["date","defi_total_tvl"]]
            save_debug(out.tail(10), "defi_total_tail_fallback")
            return out
    except Exception as e:
        warn(f"charts/defi failed: {e}")
    return pd.DataFrame(columns=["date","defi_total_tvl"])

def dl_protocols():
    j = dl_json("https://api.llama.fi/protocols")
    save_debug({"protocols_count": len(j)}, "protocols_meta")
    return j

def dl_protocol_history(slug):
    j = dl_json(f"https://api.llama.fi/protocol/{slug}")
    save_debug(j.get("tvl", [])[:5], f"protocol_{slug}_head")
    rows = []
    for pt in j.get("tvl", []):
        rows.append({
            "date": pd.to_datetime(pt.get("date"), unit="s", utc=True).date(),
            "tvl": pt.get("totalLiquidityUSD", 0)
        })
    return pd.DataFrame(rows)

# -------- RWA --------
def dl_rwa_total_and_projects():
    try:
        total_json = dl_json("https://api.llama.fi/charts/rwa")
        save_debug(total_json[:5], "rwa_total_raw_head")
        total = pd.DataFrame(total_json)
        if not total.empty:
            total["date"] = pd.to_datetime(total["date"], unit="s", utc=True).dt.date
            # 兼容不同键
            val_col = "totalLiquidityUSD"
            if "totalLiquidityUSD" not in total.columns:
                for k in ["totalValueLockedUSD","tvl","value","total"]:
                    if k in total.columns: val_col = k; break
            total = total.rename(columns={val_col:"rwa_total"})[["date","rwa_total"]]
        else:
            total = pd.DataFrame(columns=["date","rwa_total"])
    except Exception as e:
        warn(f"rwa total failed: {e}")
        total = pd.DataFrame(columns=["date","rwa_total"])

    plist=[]
    try:
        projs = dl_json("https://api.llama.fi/rwa")
        save_debug(projs.get("projects", [])[:5] if isinstance(projs, dict) else projs, "rwa_projects_raw_head")
        if isinstance(projs, dict) and "projects" in projs:
            plist = projs["projects"]
    except Exception:
        pass
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
    pdf = pd.DataFrame(norm)
    save_debug(pdf.head(20), "rwa_projects_top_meta")
    return total, pdf

# -------- Chains (TVL via v2/historicalChainTvl) --------
def dl_chain_history(chain_name):
    # 更稳：/v2/historicalChainTvl/<chain> -> [{date, tvl}]
    j = dl_json(f"https://api.llama.fi/v2/historicalChainTvl/{chain_name}")
    save_debug(j[:5], f"chain_{chain_name}_raw_head")
    df = pd.DataFrame(j)
    if df.empty:
        return pd.DataFrame(columns=["date","tvl"])
    # 兼容键名
    if "date" in df.columns and "tvl" in df.columns:
        df["date"] = pd.to_datetime(df["date"], unit="s", utc=True).dt.date
        return df[["date","tvl"]]
    # 尝试其他键
    if "timestamp" in df.columns and "tvl" in df.columns:
        df["date"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.date
        return df[["date","tvl"]]
    # [ [ts, val] ] 形式
    if df.shape[1] >= 2:
        df.columns = ["ts","tvl"] + list(df.columns[2:])
        df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
        return df[["date","tvl"]]
    return pd.DataFrame(columns=["date","tvl"])

def dl_chains_latest():
    # 取链最新 TVL 排名
    j = dl_json("https://api.llama.fi/overview/chains",
                params={"excludeTotalChart":"true","excludeChains":"false","dataType":"daily"})
    chains = j.get("chains") or []
    cdf = pd.DataFrame([{"name": c.get("name"), "tvl": c.get("tvl",0)} for c in chains if c.get("name")])
    save_debug(cdf.sort_values("tvl", ascending=False).head(50), "chains_latest_top")
    return cdf

def run_chains_tvl(conf, lookback_days, top_k, out_dir):
    charts=[]
    # 汇总总 TVL（用 top 若干链也可先跑通）
    try:
        # 用 latest 排名前 top 20 链合成总量（避免 totalDataChart 不可用）
        latest = dl_chains_latest()
        if latest.empty:
            raise ValueError("empty chains latest")
        wl = conf.get("selection", {}).get("whitelist")
        pick = latest[latest["name"].isin(wl)] if wl else latest.sort_values("tvl", ascending=False).head(20)
        total_df = None
        chosen = []
        for name in pick["name"].tolist():
            h = dl_chain_history(name)
            if h.empty: continue
            h = h[h["date"] >= (h["date"].max() - pd.Timedelta(days=lookback_days))]
            if total_df is None:
                total_df = h.rename(columns={"tvl": f"tvl_{name}"})
            else:
                total_df = pd.merge(total_df, h.rename(columns={"tvl": f"tvl_{name}"}), on="date", how="outer")
            chosen.append(name)
            # 限制合成链数量，避免超时
            if len(chosen) >= 12:
                break
        if total_df is not None:
            total_df = total_df.sort_values("date").fillna(0.0)
            total_df["chain_total_tvl"] = total_df[[c for c in total_df.columns if c.startswith("tvl_")]].sum(axis=1)
            save_debug(total_df.tail(10), "chains_total_tvl_tail")
            fig_total = px.line(total_df, x="date", y="chain_total_tvl", title=f"{conf['name']} - Total Chain TVL (3Y, sum of {len(chosen)} chains)")
            charts.append(("Total Chain TVL", fig_to_div(fig_total)))
        else:
            charts.append(("No Data", fig_to_div(px.line(pd.DataFrame({"date":[],"value":[]}), x="date", y="value",
                                                          title=f"{conf['name']} - No chain TVL data"))))
        # TopK 单链图
        top = (latest[latest["name"].isin(wl)] if wl else latest).sort_values("tvl", ascending=False).head(top_k)
        for _, r in top.iterrows():
            name = r["name"]
            h = dl_chain_history(name)
            if h.empty: continue
            h = h[h["date"] >= (h["date"].max() - pd.Timedelta(days=lookback_days))]
            fig = px.line(h.sort_values("date"), x="date", y="tvl", title=f"{name} - TVL (3Y)")
            charts.append((f"{name} - TVL", fig_to_div(fig)))
    except Exception as e:
        warn(f"chains tvl failed: {e}")
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
            save_debug(total.tail(10), "defi_total_tail_final")
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
        save_debug(top, "defi_top_protocols_today")
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
            save_debug(total.tail(10), "rwa_total_tail_final")
            fig_total = px.line(total, x="date", y="rwa_total", title=f"{conf['name']} - Total (3Y)")
            charts.append(("Total TVL", fig_to_div(fig_total)))
        else:
            raise ValueError("empty total")
        if not proj_df.empty:
            top = proj_df.sort_values("tvl", ascending=False).head(top_k)
            save_debug(top, "rwa_top_projects_today")
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
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

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

