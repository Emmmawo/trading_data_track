# scripts/run_tracks.py
# usage: python -m scripts.run_tracks --config config/tracks.yml

import os, sys, argparse, json, time
from pathlib import Path
import requests, pandas as pd
import yaml, plotly.express as px
from scripts.report_utils import fig_to_div, make_track_page, write_page

UA = "crypto-tracks-bot/1.3a (+github-actions)"
DATA_RAW = Path("data/raw")
DATA_PROC = Path("data/processed")
DOCS_TRACKS = Path("docs/tracks")

def ensure_dirs():
    for p in [DATA_RAW, DATA_PROC, DOCS_TRACKS, Path("docs")]:
        p.mkdir(parents=True, exist_ok=True)
    (Path("docs")/".nojekyll").write_text("", encoding="utf-8")

def http_get_json(url, params=None, max_retry=4):
    for i in range(max_retry):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept":"application/json"}, params=params, timeout=60)
            if r.status_code in (429, 403, 500, 502, 503):
                time.sleep(2*(i+1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == max_retry - 1:
                raise
            time.sleep(2*(i+1))
    raise RuntimeError("unreachable")

def save_raw(obj, name):
    ts = pd.Timestamp.utcnow().strftime("%Y%m%d%H%M%S")
    (DATA_RAW / f"{name}_{ts}.json").write_text(json.dumps(obj)[:400000], encoding="utf-8")

def read_csv(path, cols):
    if path.exists():
        try:
            df = pd.read_csv(path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"]).dt.date
            return df
        except Exception:
            return pd.DataFrame(columns=cols)
    return pd.DataFrame(columns=cols)

def write_csv(df, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)

def merge_on_date(old_df, new_df):
    if old_df.empty: return new_df
    out = pd.concat([old_df, new_df], ignore_index=True)
    out = out.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return out

def assert_non_empty(df, name):
    n = len(df)
    print(f"[INFO] {name}: rows={n}, span={df['date'].min() if n else None} -> {df['date'].max() if n else None}")
    if n == 0:
        print(f"[ERROR] 0 rows for {name}")
        sys.exit(1)

def window_or_full(df, lookback_days):
    if df.empty: return df
    cutoff = df["date"].max() - pd.Timedelta(days=lookback_days)
    w = df[df["date"] >= cutoff]
    return w if not w.empty else df

# ---------- Stablecoins (修复：优先用 legacy 汇总，overview 失败不阻断) ----------
def fetch_stablecoins():
    # 1) 单币历史（Top5 用 + 汇总 total）
    j2 = http_get_json("https://stablecoins.llama.fi/stablecoins?includePrices=true")
    save_raw({"peggedAssets_count": len(j2.get("peggedAssets", []))}, "stable_legacy_meta")
    rows=[]
    for a in j2.get("peggedAssets", []):
        sym = a.get("symbol"); 
        if not sym: continue
        hist = a.get("historicalCirculating")
        if isinstance(hist, dict) and hist:
            for k, v in hist.items():
                try: d = pd.to_datetime(k).date()
                except:
                    try: d = pd.to_datetime(int(k), unit="s", utc=True).date()
                    except: continue
                rows.append({"date": d, "symbol": sym, "amount": float(v or 0)})
        circ = a.get("circulating")
        if isinstance(circ, list) and circ:
            for pt in circ:
                ts=pt.get("date"); val=pt.get("circulating") or pt.get("amount") or pt.get("value")
                if ts is None or val is None: continue
                d = pd.to_datetime(ts, unit="s", utc=True).date() if isinstance(ts,(int,float)) else pd.to_datetime(ts).date()
                rows.append({"date": d, "symbol": sym, "amount": float(val)})
    coins = pd.DataFrame(rows)

    # 2) total：先尝试 overview（可能 404），失败则用 coins 汇总
    total = pd.DataFrame(columns=["date","stable_total"])
    try:
        j = http_get_json("https://stablecoins.llama.fi/overview")
        save_raw(j, "stable_overview")
        if isinstance(j, dict) and "total" in j:
            tot = j["total"]
            series = None
            for k in ["chart","circulating","circulatingUSD","totalCirculatingUSD","total"]:
                v = tot.get(k)
                if isinstance(v, list) and v:
                    series = v; break
            if series:
                first = series[0]
                if isinstance(first, dict):
                    rows=[]
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
    except requests.HTTPError as e:
        # 概览不可用（404/429等）时，忽略，用 coins 汇总
        print(f"[WARN] stablecoins overview failed: {e}. Falling back to legacy aggregation.")
    except Exception as e:
        print(f"[WARN] stablecoins overview unexpected: {e}. Falling back to legacy aggregation.")

    # 用 coins 汇总作为兜底（也是主路径）
    if total.empty and not coins.empty:
        total = (coins.groupby("date", as_index=False)["amount"]
                 .sum().rename(columns={"amount":"stable_total"}))

    # 合并/落盘
    total_path = DATA_PROC / "stablecoins_total.csv"
    old_total = read_csv(total_path, ["date","stable_total"])
    if not total.empty:
        total["date"] = pd.to_datetime(total["date"]).dt.date
        total = merge_on_date(old_total, total)
        write_csv(total, total_path)
    else:
        total = old_total
    assert_non_empty(total, "Stablecoins total")

    # Top5 单币 CSV
    top_syms = []
    if not coins.empty:
        coins["date"] = pd.to_datetime(coins["date"]).dt.date
        latest = coins["date"].max()
        top_syms = (coins[coins["date"]==latest].groupby("symbol", as_index=False)["amount"]
                    .sum().sort_values("amount", ascending=False).head(5)["symbol"].tolist())
        for sym in top_syms:
            sdf = coins[coins["symbol"]==sym][["date","amount"]].dropna()
            path = DATA_PROC / f"stablecoins_top_{sym}.csv"
            old = read_csv(path, ["date","amount"])
            sdf = merge_on_date(old, sdf)
            write_csv(sdf, path)

    return total, top_syms

# ---------- DeFi Total + Top protocols ----------
def fetch_defi_total_and_top(top_k=5):
    try:
        j = http_get_json("https://api.llama.fi/overview/defi",
                          params={"excludeTotalChart":"false","excludeProtocolChart":"true","dataType":"daily"})
        chart = (j.get("totalDataChart") or j.get("totalChart")) or []
        if chart:
            df = pd.DataFrame(chart, columns=["ts","tvl"])
            df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
            out = df[["date","tvl"]].rename(columns={"tvl":"defi_total_tvl"})
        else:
            out = pd.DataFrame(columns=["date","defi_total_tvl"])
    except Exception:
        j = http_get_json("https://api.llama.fi/charts/defi")
        df = pd.DataFrame(j)
        if df.empty:
            out = pd.DataFrame(columns=["date","defi_total_tvl"])
        else:
            df["date"] = pd.to_datetime(df["date"], unit="s", utc=True).dt.date
            out = df.rename(columns={"totalLiquidityUSD":"defi_total_tvl"})[["date","defi_total_tvl"]]
    path = DATA_PROC / "defi_total.csv"
    old = read_csv(path, ["date","defi_total_tvl"])
    if not out.empty:
        out["date"] = pd.to_datetime(out["date"]).dt.date
        out = merge_on_date(old, out)
        write_csv(out, path)
    else:
        out = old
    assert_non_empty(out, "DeFi total")

    prots = http_get_json("https://api.llama.fi/protocols")
    pdf = pd.DataFrame([{
        "name": p.get("name"),
        "slug": p.get("slug"),
        "tvl": p.get("tvl", 0),
        "category": p.get("category", "")
    } for p in prots if p.get("name")])
    pdf = pdf[pdf["tvl"].notna()]
    defi_cats = set(["Lending","Dexes","CDP","Derivatives","Yield","Staking","Assets","Insurance","Services"])
    pdf = pdf[pdf["category"].str.title().isin(defi_cats)]
    top = pdf.sort_values("tvl", ascending=False).head(top_k)
    saved = []
    for _, r in top.iterrows():
        hist = http_get_json(f"https://api.llama.fi/protocol/{r['slug']}")
        rows = []
        for pt in hist.get("tvl", []):
            rows.append({
                "date": pd.to_datetime(pt.get("date"), unit="s", utc=True).date(),
                "tvl": pt.get("totalLiquidityUSD", 0)
            })
        hdf = pd.DataFrame(rows)
        if hdf.empty:
            continue
        hdf["date"] = pd.to_datetime(hdf["date"]).dt.date
        path = DATA_PROC / f"defi_top_{r['slug']}.csv"
        old = read_csv(path, ["date","tvl"])
        hdf = merge_on_date(old, hdf)
        write_csv(hdf, path)
        saved.append((r["name"], r["slug"]))
    return out, saved

# ---------- RWA total + Top projects ----------
def fetch_rwa_and_top(top_k=5):
    j = http_get_json("https://api.llama.fi/charts/rwa")
    save_raw(j[:10], "rwa_total_head")
    df = pd.DataFrame(j)
    if df.empty:
        out = pd.DataFrame(columns=["date","rwa_total"])
    else:
        df["date"] = pd.to_datetime(df["date"], unit="s", utc=True).dt.date
        val_col = None
        for k in ["totalLiquidityUSD","totalValueLockedUSD","tvl","value","total"]:
            if k in df.columns:
                val_col = k; break
        if val_col is None:
            out = pd.DataFrame(columns=["date","rwa_total"])
        else:
            out = df.rename(columns={val_col:"rwa_total"})[["date","rwa_total"]]
    path = DATA_PROC / "rwa_total.csv"
    old = read_csv(path, ["date","rwa_total"])
    if not out.empty:
        out["date"] = pd.to_datetime(out["date"]).dt.date
        out = merge_on_date(old, out)
        write_csv(out, path)
    else:
        out = old
    assert_non_empty(out, "RWA total")

    plist = []
    try:
        projs = http_get_json("https://api.llama.fi/rwa")
        if isinstance(projs, dict) and "projects" in projs:
            plist = projs["projects"]
    except Exception:
        pass
    if not plist:
        allp = http_get_json("https://api.llama.fi/protocols")
        plist = [p for p in allp if str(p.get("category","")).upper()=="RWA"]
    pdf = pd.DataFrame([{"name": p.get("name"),
                         "slug": p.get("slug") or p.get("name","").lower().replace(" ","-"),
                         "tvl": p.get("tvl", 0) or 0} for p in plist if p.get("name")])
    top = pdf.sort_values("tvl", ascending=False).head(top_k)
    saved = []
    for _, r in top.iterrows():
        hist = http_get_json(f"https://api.llama.fi/protocol/{r['slug']}")
        rows = []
        for pt in hist.get("tvl", []):
            rows.append({"date": pd.to_datetime(pt.get("date"), unit="s", utc=True).date(),
                         "tvl": pt.get("totalLiquidityUSD", 0)})
        hdf = pd.DataFrame(rows)
        if hdf.empty: continue
        hdf["date"] = pd.to_datetime(hdf["date"]).dt.date
        path = DATA_PROC / f"rwa_top_{r['slug']}.csv"
        old = read_csv(path, ["date","tvl"])
        hdf = merge_on_date(old, hdf)
        write_csv(hdf, path)
        saved.append((r["name"], r["slug"]))
    return out, saved

# ---------- Chains (TVL) ----------
def chain_series(chain_name):
    try:
        j = http_get_json(f"https://api.llama.fi/v2/historicalChainTvl/{chain_name}")
        df = pd.DataFrame(j)
        if not df.empty:
            if "date" in df.columns and "tvl" in df.columns:
                df["date"] = pd.to_datetime(df["date"], unit="s", utc=True).dt.date
                return df[["date","tvl"]]
            if "timestamp" in df.columns and "tvl" in df.columns:
                df["date"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.date
                return df[["date","tvl"]]
            if df.shape[1] >= 2:
                df.columns = ["ts","tvl"] + list(df.columns[2:])
                df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
                return df[["date","tvl"]]
    except Exception:
        pass
    try:
        j2 = http_get_json(f"https://api.llama.fi/charts/{chain_name}")
        d2 = pd.DataFrame(j2)
        if not d2.empty:
            if "date" in d2.columns and "totalLiquidityUSD" in d2.columns:
                d2["date"] = pd.to_datetime(d2["date"], unit="s", utc=True).dt.date
                d2 = d2.rename(columns={"totalLiquidityUSD":"tvl"})
                return d2[["date","tvl"]]
            if d2.shape[1] >= 2:
                d2.columns = ["ts","tvl"] + list(d2.columns[2:])
                d2["date"] = pd.to_datetime(d2["ts"], unit="s", utc=True).dt.date
                return d2[["date","tvl"]]
    except Exception:
        pass
    return pd.DataFrame(columns=["date","tvl"])

def fetch_chains_total_and_top(whitelist=None, top_k=5):
    ov = http_get_json("https://api.llama.fi/overview/chains",
                       params={"excludeTotalChart":"false","excludeChains":"true","dataType":"daily"})
    chart = (ov.get("totalDataChart") or ov.get("totalChart")) or []
    if chart:
        df = pd.DataFrame(chart, columns=["ts","tvl"])
        df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
        total = df[["date","tvl"]].rename(columns={"tvl":"chain_total_tvl"})
    else:
        total = pd.DataFrame(columns=["date","chain_total_tvl"])
    total_path = DATA_PROC / "chains_total.csv"
    old = read_csv(total_path, ["date","chain_total_tvl"])
    if not total.empty:
        total["date"] = pd.to_datetime(total["date"]).dt.date
        total = merge_on_date(old, total)
        write_csv(total, total_path)
    else:
        total = old
    assert_non_empty(total, "Chains total TVL")

    ov2 = http_get_json("https://api.llama.fi/overview/chains",
                        params={"excludeTotalChart":"true","excludeChains":"false","dataType":"daily"})
    chains = ov2.get("chains") or []
    cdf = pd.DataFrame([{"name": c.get("name"), "tvl": c.get("tvl",0)} for c in chains if c.get("name")])
    if whitelist:
        cdf = cdf[cdf["name"].isin(whitelist)]
    top = cdf.sort_values("tvl", ascending=False).head(top_k)
    saved = []
    for _, r in top.iterrows():
        name = r["name"]
        h = chain_series(name)
        if h.empty: 
            continue
        path = DATA_PROC / f"chain_{name}.csv"
        old = read_csv(path, ["date","tvl"])
        h["date"] = pd.to_datetime(h["date"]).dt.date
        h = merge_on_date(old, h)
        write_csv(h, path)
        saved.append(name)
    return total, saved

# ---------- Plot ----------
def plot_track_page(title, charts, footer):
    html = make_track_page(title, charts, footer_note=footer)
    write_page(DOCS_TRACKS / f"{title}.html", html)

def run_all(cfg):
    ensure_dirs()
    lookback = cfg["global"].get("lookback_days", 1095)
    top_k = cfg["global"].get("top_k", 5)

    # Stablecoins
    st_total, st_syms = fetch_stablecoins()
    st_total = window_or_full(st_total, lookback)
    charts = [("Total (3Y)", fig_to_div(px.line(st_total, x="date", y="stable_total", title="Stablecoins Total")))]
    for sym in st_syms[:top_k]:
        sdf = read_csv(DATA_PROC / f"stablecoins_top_{sym}.csv", ["date","amount"])
        if sdf.empty: continue
        sdf = window_or_full(sdf, lookback)
        charts.append((f"{sym} - Supply/Cap", fig_to_div(px.line(sdf, x="date", y="amount", title=f"{sym}"))))
    plot_track_page("Stablecoins", charts, "DeFiLlama Stablecoins")

    # DeFi
    defi_total, defi_top = fetch_defi_total_and_top(top_k=top_k)
    ddf = window_or_full(defi_total, lookback)
    charts = [("Total TVL (3Y)", fig_to_div(px.line(ddf, x="date", y="defi_total_tvl", title="DeFi Total TVL")))]
    for name, slug in defi_top:
        h = read_csv(DATA_PROC / f"defi_top_{slug}.csv", ["date","tvl"])
        if h.empty: continue
        h = window_or_full(h, lookback)
        charts.append((f"{name} - TVL", fig_to_div(px.line(h, x="date", y="tvl", title=f"{name}"))))
    plot_track_page("DeFi - Total", charts, "DeFiLlama Overview + Protocols")

    # RWA
    rwa_total, rwa_top = fetch_rwa_and_top(top_k=top_k)
    rdf = window_or_full(rwa_total, lookback)
    charts = [("Total (3Y)", fig_to_div(px.line(rdf, x="date", y="rwa_total", title="RWA Total")))]
    for name, slug in rwa_top:
        h = read_csv(DATA_PROC / f"rwa_top_{slug}.csv", ["date","tvl"])
        if h.empty: continue
        h = window_or_full(h, lookback)
        charts.append((f"{name} - TVL", fig_to_div(px.line(h, x="date", y="tvl", title=f"{name}"))))
    plot_track_page("RWA", charts, "DeFiLlama RWA/Protocols")

    # Chains
    chains_conf = next((t for t in cfg["tracks"] if t["source"].lower()=="defillama_chains"), None)
    wl = (chains_conf or {}).get("selection", {}).get("whitelist", [])
    ch_total, ch_top = fetch_chains_total_and_top(whitelist=wl, top_k=top_k)
    cdf = window_or_full(ch_total, lookback)
    charts = [("Total Chain TVL (3Y)", fig_to_div(px.line(cdf, x="date", y="chain_total_tvl", title="Chains Total TVL")))]
    for name in ch_top:
        h = read_csv(DATA_PROC / f"chain_{name}.csv", ["date","tvl"])
        if h.empty: continue
        h = window_or_full(h, lookback)
        charts.append((f"{name} - TVL", fig_to_div(px.line(h, x="date", y="tvl", title=f"{name}"))))
    plot_track_page("Chains (TVL)", charts, "DeFiLlama Chains")

    print("[DONE] Saved CSV to data/processed, pages to docs/tracks")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, default="config/tracks.yml")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    run_all(cfg)

if __name__ == "__main__":
    main()

