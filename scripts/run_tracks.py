# scripts/run_tracks.py
# usage: python -m scripts.run_tracks --config config/tracks.yml

import sys, argparse, json, time, random
from pathlib import Path
import requests, pandas as pd
import yaml, plotly.express as px
from scripts.report_utils import fig_to_div, make_track_page, write_page

UA = "crypto-tracks-bot/1.5 (+github-actions)"
DATA_RAW = Path("data/raw")
DATA_PROC = Path("data/processed")
DOCS_TRACKS = Path("docs/tracks")

# 全局限频（秒）
GLOBAL_RATE_SEC = 0.8

# 是否抓 DeFi Top 协议（为降请求，默认 False；稳定后可改 True）
DEF_TOP_ENABLED = False
DEF_TOP_K = 3  # 开启时最多3个，减少 API 压力

_last_req_ts = 0.0

def ensure_dirs():
    for p in [DATA_RAW, DATA_PROC, DOCS_TRACKS, Path("docs")]:
        p.mkdir(parents=True, exist_ok=True)
    (Path("docs")/".nojekyll").write_text("", encoding="utf-8")

def _rate_limit():
    global _last_req_ts
    now = time.time()
    dt = now - _last_req_ts
    if dt < GLOBAL_RATE_SEC:
        time.sleep(GLOBAL_RATE_SEC - dt)
    _last_req_ts = time.time()

def http_get_json(url, params=None, max_retry=8):
    """
    稳健 GET：全局节流+指数退避+抖动；对 429/403/5xx 自动重试
    """
    backoff = 1.2
    for i in range(max_retry):
        try:
            _rate_limit()
            r = requests.get(
                url,
                headers={"User-Agent": UA, "Accept": "application/json"},
                params=params,
                timeout=60,
            )
            if r.status_code in (429, 403, 500, 502, 503, 504):
                # 指数退避 + 抖动
                sleep_s = (backoff ** i) + random.uniform(0, 0.5)
                print(f"[WARN] {r.status_code} on {url}, retry in {sleep_s:.2f}s")
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == max_retry - 1:
                print(f"[ERROR] GET fail {url}: {e}")
                raise
            sleep_s = (backoff ** i) + random.uniform(0, 0.5)
            print(f"[WARN] exception on {url}, retry in {sleep_s:.2f}s: {e}")
            time.sleep(sleep_s)
    raise RuntimeError("unreachable")

def save_raw(obj, name):
    ts = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d%H%M%S")
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

def ts_to_date(ts):
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return pd.to_datetime(int(ts), unit='s', utc=True).date()
        s = str(ts).strip()
        if s.isdigit():
            return pd.to_datetime(int(s), unit='s', utc=True).date()
        return pd.to_datetime(s, utc=True).date()
    except Exception:
        return None

# ---------- Stablecoins: /stablecoincharts/all + /stablecoins + /stablecoin/{id} ----------
# 参考文档: [1] /stablecoincharts/all, /stablecoins, /stablecoin/{asset}
def fetch_stablecoins():
    # 总量历史
    all_series = http_get_json("https://stablecoins.llama.fi/stablecoincharts/all")  # [1]
    rows = []
    for pt in (all_series or []):
        d = ts_to_date(pt.get("date"))
        tot = pt.get("totalCirculating") or {}
        usd = tot.get("peggedUSD") if isinstance(tot, dict) else None
        if d and usd is not None:
            rows.append({"date": d, "stable_total": float(usd)})
    total = pd.DataFrame(rows)

    # Top5 当前
    listing = http_get_json("https://stablecoins.llama.fi/stablecoins?includePrices=true")  # [1]
    assets = listing.get("peggedAssets", []) if isinstance(listing, dict) else []
    latest_rows, id_map = [], {}
    for a in assets:
        sid = str(a.get("id"))
        sym = a.get("symbol")
        id_map[sid] = sym or sid
        circ = a.get("circulating") or {}
        cur_usd = circ.get("peggedUSD") if isinstance(circ, dict) else None
        if cur_usd is not None:
            latest_rows.append({"id": sid, "symbol": sym or sid, "cur_usd": float(cur_usd)})

    top_syms = []
    if latest_rows:
        latest_df = pd.DataFrame(latest_rows)
        top_ids = latest_df.sort_values("cur_usd", ascending=False).head(5)["id"].tolist()
        for asset_id in top_ids:
            coin = http_get_json(f"https://stablecoins.llama.fi/stablecoin/{asset_id}")  # [1]
            hist = coin.get("totalCirculating") or []
            hrows = []
            for pt in hist:
                d = ts_to_date(pt.get("date"))
                val = pt.get("totalCirculating")
                if d and val is not None:
                    hrows.append({"date": d, "amount": float(val)})
            hdf = pd.DataFrame(hrows)
            sym = id_map.get(str(asset_id), str(asset_id))
            if not hdf.empty:
                path = DATA_PROC / f"stablecoins_top_{sym}.csv"
                old = read_csv(path, ["date","amount"])
                hdf = merge_on_date(old, hdf)
                write_csv(hdf, path)
                top_syms.append(sym)

    total_path = DATA_PROC / "stablecoins_total.csv"
    old_total = read_csv(total_path, ["date","stable_total"])
    if not total.empty:
        total["date"] = pd.to_datetime(total["date"]).dt.date
        total = merge_on_date(old_total, total)
        write_csv(total, total_path)
    else:
        total = old_total

    assert_non_empty(total, "Stablecoins total")
    return total, top_syms

# ---------- DeFi total: /overview/defi?dataType=daily, fallback to /v2/historicalChainTvl ----------
# 参考文档: [2] /overview/defi?dataType=daily, /v2/historicalChainTvl
def fetch_defi_total_and_top(top_k=3):
    # 总量优先 /overview/defi
    out = pd.DataFrame(columns=["date","defi_total_tvl"])
    try:
        j = http_get_json("https://api.llama.fi/overview/defi",
                          params={"excludeTotalChart":"false","excludeProtocolChart":"true","dataType":"daily"})  # [2]
        chart = (j.get("totalDataChart") or j.get("totalChart")) or []
        if chart:
            df = pd.DataFrame(chart, columns=["ts","tvl"])
            df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.date
            out = df[["date","tvl"]].rename(columns={"tvl":"defi_total_tvl"})
    except Exception as e:
        print(f"[WARN] overview/defi failed, fallback to chain total: {e}")

    # 兜底：直接用全链合计 TVL 作为 DeFi Total（两者口径一致）
    if out.empty:
        try:
            tjson = http_get_json("https://api.llama.fi/v2/historicalChainTvl")  # [2]
            trows = []
            for pt in (tjson or []):
                d = ts_to_date(pt.get("date"))
                tvl = pt.get("tvl")
                if d and tvl is not None:
                    trows.append({"date": d, "defi_total_tvl": float(tvl)})
            out = pd.DataFrame(trows)
        except Exception as e:
            print(f"[WARN] chain tvl fallback failed: {e}")

    path = DATA_PROC / "defi_total.csv"
    old = read_csv(path, ["date","defi_total_tvl"])
    if not out.empty:
        out["date"] = pd.to_datetime(out["date"]).dt.date
        out = merge_on_date(old, out)
        write_csv(out, path)
    else:
        out = old
    assert_non_empty(out, "DeFi total")

    # Top 协议（降请求，默认关闭）
    saved = []
    if DEF_TOP_ENABLED:
        try:
            prots = http_get_json("https://api.llama.fi/protocols")  # [2]
            pdf = pd.DataFrame([{
                "name": p.get("name"),
                "slug": p.get("slug"),
                "tvl": p.get("tvl", 0),
                "category": p.get("category", "")
            } for p in prots if p.get("name")])
            pdf = pdf[pdf["tvl"].notna()]
            # 高频抓取时可加白名单/类别过滤，这里直接按 TVL 取前3
            top = pdf.sort_values("tvl", ascending=False).head(min(top_k, DEF_TOP_K))
            for _, r in top.iterrows():
                hist = http_get_json(f"https://api.llama.fi/protocol/{r['slug']}")  # [2]
                rows = []
                for pt in hist.get("tvl", []):
                    rows.append({"date": pd.to_datetime(pt.get("date"), unit="s", utc=True).date(),
                                 "tvl": pt.get("totalLiquidityUSD", 0)})
                hdf = pd.DataFrame(rows)
                if hdf.empty: continue
                hdf["date"] = pd.to_datetime(hdf["date"]).dt.date
                pth = DATA_PROC / f"defi_top_{r['slug']}.csv"
                oldh = read_csv(pth, ["date","tvl"])
                hdf = merge_on_date(oldh, hdf)
                write_csv(hdf, pth)
                saved.append((r["name"], r["slug"]))
        except Exception as e:
            print(f"[WARN] DeFi top protocols skipped: {e}")

    return out, saved

# ---------- RWA: /charts/rwa + /rwa (list) ----------
# 参考文档: [2] /charts/rwa, /rwa
def fetch_rwa_and_top(top_k=5):
    j = http_get_json("https://api.llama.fi/charts/rwa")  # [2]
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
        projs = http_get_json("https://api.llama.fi/rwa")  # [2]
        if isinstance(projs, dict) and "projects" in projs:
            plist = projs["projects"]
    except Exception:
        pass
    if not plist:
        allp = http_get_json("https://api.llama.fi/protocols")  # [2]
        plist = [p for p in allp if str(p.get("category","")).upper()=="RWA"]
    pdf = pd.DataFrame([{"name": p.get("name"),
                         "slug": p.get("slug") or p.get("name","").lower().replace(" ","-"),
                         "tvl": p.get("tvl", 0) or 0} for p in plist if p.get("name")])
    top = pdf.sort_values("tvl", ascending=False).head(top_k)
    saved = []
    for _, r in top.iterrows():
        hist = http_get_json(f"https://api.llama.fi/protocol/{r['slug']}")  # [2]
        rows = []
        for pt in hist.get("tvl", []):
            rows.append({"date": pd.to_datetime(pt.get("date"), unit="s", utc=True).date(),
                         "tvl": pt.get("totalLiquidityUSD", 0)})
        hdf = pd.DataFrame(rows)
        if hdf.empty: continue
        hdf["date"] = pd.to_datetime(hdf["date"]).dt.date
        pth = DATA_PROC / f"rwa_top_{r['slug']}.csv"
        oldh = read_csv(pth, ["date","tvl"])
        hdf = merge_on_date(oldh, hdf)
        write_csv(hdf, pth)
        saved.append((r["name"], r["slug"]))
    return out, saved

# ---------- Chains (TVL): /v2/historicalChainTvl (all) + /v2/historicalChainTvl/{chain} ----------
# 参考文档: [2] /v2/historicalChainTvl, /v2/historicalChainTvl/{chain}
def chain_series(chain_name):
    try:
        j = http_get_json(f"https://api.llama.fi/v2/historicalChainTvl/{chain_name}")  # [2]
        rows = []
        for pt in (j or []):
            d = ts_to_date(pt.get("date"))
            tvl = pt.get("tvl")
            if d and tvl is not None:
                rows.append({"date": d, "tvl": float(tvl)})
        if rows:
            return pd.DataFrame(rows)
    except Exception:
        pass
    try:
        j2 = http_get_json(f"https://api.llama.fi/charts/{chain_name}")  # [2]
        rows = []
        for pt in (j2 or []):
            d = ts_to_date(pt.get("date"))
            tvl = pt.get("totalLiquidityUSD") if isinstance(pt, dict) else None
            if d and tvl is not None:
                rows.append({"date": d, "tvl": float(tvl)})
        if rows:
            return pd.DataFrame(rows)
    except Exception:
        pass
    return pd.DataFrame(columns=["date","tvl"])

def fetch_chains_total_and_top(whitelist=None, top_k=5):
    # 全链合计
    total_json = http_get_json("https://api.llama.fi/v2/historicalChainTvl")  # [2]
    trows = []
    for pt in (total_json or []):
        d = ts_to_date(pt.get("date"))
        tvl = pt.get("tvl")
        if d and tvl is not None:
            trows.append({"date": d, "chain_total_tvl": float(tvl)})
    total = pd.DataFrame(trows)

    total_path = DATA_PROC / "chains_total.csv"
    old = read_csv(total_path, ["date","chain_total_tvl"])
    if not total.empty:
        total["date"] = pd.to_datetime(total["date"]).dt.date
        total = merge_on_date(old, total)
        write_csv(total, total_path)
    else:
        total = old
    assert_non_empty(total, "Chains total TVL")

    # TopK 单链：从 overview/chains 取最新排序
    ov2 = http_get_json("https://api.llama.fi/overview/chains",
                        params={"excludeTotalChart":"true","excludeChains":"false","dataType":"daily"})  # [2]
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
        pth = DATA_PROC / f"chain_{name}.csv"
        oldh = read_csv(pth, ["date","tvl"])
        h["date"] = pd.to_datetime(h["date"]).dt.date
        h = merge_on_date(oldh, h)
        write_csv(h, pth)
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
    plot_track_page("Stablecoins", charts, "Stablecoins: /stablecoincharts/all, /stablecoins, /stablecoin/{id} [1]")

    # DeFi
    defi_total, defi_top = fetch_defi_total_and_top(top_k=min(top_k, DEF_TOP_K))
    ddf = window_or_full(defi_total, lookback)
    charts = [("Total TVL (3Y)", fig_to_div(px.line(ddf, x="date", y="defi_total_tvl", title="DeFi Total TVL")))]
    for name, slug in defi_top:
        h = read_csv(DATA_PROC / f"defi_top_{slug}.csv", ["date","tvl"])
        if h.empty: continue
        h = window_or_full(h, lookback)
        charts.append((f"{name} - TVL", fig_to_div(px.line(h, x="date", y="tvl", title=f"{name}"))))
    plot_track_page("DeFi - Total", charts, "DeFi: /overview/defi or /v2/historicalChainTvl (fallback); /protocols, /protocol/{slug} [2]")

    # RWA
    rwa_total, rwa_top = fetch_rwa_and_top(top_k=top_k)
    rdf = window_or_full(rwa_total, lookback)
    charts = [("Total (3Y)", fig_to_div(px.line(rdf, x="date", y="rwa_total", title="RWA Total")))]
    for name, slug in rwa_top:
        h = read_csv(DATA_PROC / f"rwa_top_{slug}.csv", ["date","tvl"])
        if h.empty: continue
        h = window_or_full(h, lookback)
        charts.append((f"{name} - TVL", fig_to_div(px.line(h, x="date", y="tvl", title=f"{name}"))))
    plot_track_page("RWA", charts, "RWA: /charts/rwa; /rwa; /protocol/{slug} [2]")

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
    plot_track_page("Chains (TVL)", charts, "Chains: /v2/historicalChainTvl (all + chain) [2]")

    print("[DONE] Saved CSV to data/processed, pages to docs/tracks")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, default="config/tracks.yml")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    run_all(cfg)

if __name__ == "__main__":
    main()

