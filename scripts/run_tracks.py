# scripts/run_tracks.py
# usage: python -m scripts.run_tracks --config config/tracks.yml

import sys, argparse, json, time, random, re
from io import StringIO
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
    
def http_get_text(url, params=None, max_retry=8):
    """
    稳健 GET 文本：用于 CSV 等非 JSON 响应。
    """
    backoff = 1.2
    for i in range(max_retry):
        try:
            _rate_limit()
            r = requests.get(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/csv,application/json;q=0.9,*/*;q=0.8",
                },
                params=params,
                timeout=60,
            )
            if r.status_code in (429, 403, 500, 502, 503, 504):
                sleep_s = (backoff ** i) + random.uniform(0, 0.5)
                print(f"[WARN] {r.status_code} on {url}, retry in {sleep_s:.2f}s")
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.text
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

RWA_EXCLUDED_SYMBOLS = {"cbbtc"}

def safe_slug(value):
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "unknown"

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
    # DeFi total：overview/defi 最近经常 500，直接使用稳定的全链历史 TVL。
    out = pd.DataFrame(columns=["date", "defi_total_tvl"])
    try:
        tjson = http_get_json("https://api.llama.fi/v2/historicalChainTvl")
        trows = []
        for pt in (tjson or []):
            d = ts_to_date(pt.get("date"))
            tvl = pt.get("tvl")
            if d and tvl is not None:
                trows.append({"date": d, "defi_total_tvl": float(tvl)})
        out = pd.DataFrame(trows)
    except Exception as e:
        print(f"[WARN] DeFi total failed: {e}")

    path = DATA_PROC / "defi_total.csv"
    old = read_csv(path, ["date", "defi_total_tvl"])
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
            prots = http_get_json("https://api.llama.fi/protocols")
            pdf = pd.DataFrame([{
                "name": p.get("name"),
                "slug": p.get("slug"),
                "tvl": p.get("tvl", 0),
                "category": p.get("category", "")
            } for p in prots if p.get("name")])
            pdf = pdf[pdf["tvl"].notna()]
            top = pdf.sort_values("tvl", ascending=False).head(min(top_k, DEF_TOP_K))
            for _, r in top.iterrows():
                hist = http_get_json(f"https://api.llama.fi/protocol/{r['slug']}")
                rows = []
                for pt in hist.get("tvl", []):
                    rows.append({
                        "date": pd.to_datetime(pt.get("date"), unit="s", utc=True).date(),
                        "tvl": pt.get("totalLiquidityUSD", 0),
                    })
                hdf = pd.DataFrame(rows)
                if hdf.empty:
                    continue
                hdf["date"] = pd.to_datetime(hdf["date"]).dt.date
                pth = DATA_PROC / f"defi_top_{r['slug']}.csv"
                oldh = read_csv(pth, ["date", "tvl"])
                hdf = merge_on_date(oldh, hdf)
                write_csv(hdf, pth)
                saved.append((r["name"], r["slug"]))
        except Exception as e:
            print(f"[WARN] DeFi top protocols skipped: {e}")

    return out, saved

# ---------- RWA: RWA Pipe /market + /export/tokens ----------
# 口径：RWA Pipe tokenized RWA TVL，排除 stablecoin category。
# 注意：免费 CSV 历史导出请求上限为 365 天；实际返回日期可能更少，本地 CSV 会每天 merge，之后可逐步累积更长历史。
def fetch_rwa_and_top(top_k=5):
    market = http_get_json(
        "https://rwapipe.com/api/market",
        params={"excludeStablecoins": "true"},
    )

    csv_text = http_get_text(
        "https://rwapipe.com/api/export/tokens",
        params={"days": 365},
    )
    hist = pd.read_csv(StringIO(csv_text))

    if not hist.empty:
        hist["date"] = pd.to_datetime(hist["date"], utc=True).dt.date
        hist["category"] = hist["category"].fillna("").astype(str).str.lower()
        hist["symbol"] = hist["symbol"].fillna("").astype(str)
        hist["tvl_usd"] = pd.to_numeric(hist["tvl_usd"], errors="coerce").fillna(0)
        hist = hist[hist["category"] != "stablecoin"]
        hist = hist[~hist["symbol"].str.lower().isin(RWA_EXCLUDED_SYMBOLS)]

    if hist.empty:
        out = pd.DataFrame(columns=["date", "rwa_total"])
    else:
        out = (
            hist.groupby("date", as_index=False)["tvl_usd"]
            .sum()
            .rename(columns={"tvl_usd": "rwa_total"})
        )

    # 如果 CSV 临时为空，至少用 /market 的当前总量补今天，避免整条 RWA track 失败。
    if out.empty:
        summary = market.get("summary") or {}
        current = summary.get("rwaTVL") or summary.get("totalTVL")
        if current is not None:
            out = pd.DataFrame([{
                "date": pd.Timestamp.now(tz="UTC").date(),
                "rwa_total": float(current),
            }])

    path = DATA_PROC / "rwa_total.csv"
    old = read_csv(path, ["date", "rwa_total"])
    if not out.empty:
        out["date"] = pd.to_datetime(out["date"]).dt.date
        out = merge_on_date(old, out)
        write_csv(out, path)
    else:
        out = old
    assert_non_empty(out, "RWA total")

    tokens = market.get("data") or []
    latest = pd.DataFrame([{
        "name": t.get("name") or t.get("symbol"),
        "symbol": t.get("symbol") or "",
        "chain": t.get("chain") or "",
        "address": t.get("address") or "",
        "category": str(t.get("category") or "").lower(),
        "tvl": float(t.get("tvlUsd") or 0),
    } for t in tokens if t.get("name") or t.get("symbol")])

    if latest.empty:
        return out, []

    latest = latest[latest["category"] != "stablecoin"]
    latest = latest[~latest["symbol"].str.lower().isin(RWA_EXCLUDED_SYMBOLS)]
    top = latest.sort_values("tvl", ascending=False).head(top_k)

    saved = []
    for _, r in top.iterrows():
        slug = safe_slug(f"{r['chain']}-{r['symbol']}-{r['address'][:8]}")
        label = f"{r['symbol']} ({r['chain']})" if r["symbol"] else r["name"]

        hdf = pd.DataFrame(columns=["date", "tvl"])
        if not hist.empty and r["address"]:
            mask = (
                hist["chain"].fillna("").astype(str).str.lower().eq(str(r["chain"]).lower())
                & hist["token_address"].fillna("").astype(str).str.lower().eq(str(r["address"]).lower())
            )
            hdf = hist.loc[mask, ["date", "tvl_usd"]].rename(columns={"tvl_usd": "tvl"})

        if hdf.empty and r["tvl"]:
            hdf = pd.DataFrame([{
                "date": pd.Timestamp.now(tz="UTC").date(),
                "tvl": r["tvl"],
            }])

        if hdf.empty:
            continue

        hdf["date"] = pd.to_datetime(hdf["date"]).dt.date
        pth = DATA_PROC / f"rwa_top_{slug}.csv"
        oldh = read_csv(pth, ["date", "tvl"])
        hdf = merge_on_date(oldh, hdf)
        write_csv(hdf, pth)
        saved.append((label, slug))

    return out, saved

# ---------- Chains (TVL): /v2/historicalChainTvl (all) + /v2/historicalChainTvl/{chain} ----------
# 参考文档: [2] /v2/historicalChainTvl, /v2/historicalChainTvl/{chain}
def fetch_chains_total_and_top(whitelist=None, top_k=5):
    # 全链合计
    total_json = http_get_json("https://api.llama.fi/v2/historicalChainTvl")
    trows = []
    for pt in (total_json or []):
        d = ts_to_date(pt.get("date"))
        tvl = pt.get("tvl")
        if d and tvl is not None:
            trows.append({"date": d, "chain_total_tvl": float(tvl)})
    total = pd.DataFrame(trows)

    total_path = DATA_PROC / "chains_total.csv"
    old = read_csv(total_path, ["date", "chain_total_tvl"])
    if not total.empty:
        total["date"] = pd.to_datetime(total["date"]).dt.date
        total = merge_on_date(old, total)
        write_csv(total, total_path)
    else:
        total = old
    assert_non_empty(total, "Chains total TVL")

    # overview/chains 最近经常 500；优先按 config whitelist 抓单链历史。
    chain_names = list(whitelist or [])

    # 如果没有配置 whitelist，再尝试 v2/chains；失败时使用保守默认列表。
    if not chain_names:
        try:
            chains = http_get_json("https://api.llama.fi/v2/chains", max_retry=3)
            cdf = pd.DataFrame([{
                "name": c.get("name"),
                "tvl": c.get("tvl", 0),
            } for c in chains if c.get("name")])
            chain_names = cdf.sort_values("tvl", ascending=False).head(top_k)["name"].tolist()
        except Exception as e:
            print(f"[WARN] v2/chains failed, use default chain list: {e}")
            chain_names = ["Ethereum", "Tron", "BSC", "Arbitrum", "Solana", "Base", "Optimism"]

    saved = []
    for name in chain_names[:top_k]:
        h = chain_series(name)
        if h.empty:
            print(f"[WARN] Chain skipped, no data: {name}")
            continue
        pth = DATA_PROC / f"chain_{name}.csv"
        oldh = read_csv(pth, ["date", "tvl"])
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
    plot_track_page("RWA", charts, "RWA: RWA Pipe /market and /export/tokens; excludes stablecoin category")

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
    plot_track_page("Chains (TVL)", charts, "Chains: /v2/historicalChainTvl for total and configured whitelist chains")

    print("[DONE] Saved CSV to data/processed, pages to docs/tracks")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, default="config/tracks.yml")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    run_all(cfg)

if __name__ == "__main__":
    main()

