def dl_stablecoins_total_and_hist():
    # 统一的请求（带 UA 和重试）
    j = dl_json("https://stablecoins.llama.fi/stablecoins?includePrices=true")
    if not j or "peggedAssets" not in j:
        # 返回空 DF，调用方做 empty 判断
        return pd.DataFrame(columns=["date","stable_total"]), pd.DataFrame(columns=["date","symbol","amount"])

    rows_total, rows_coins = [], []
    for a in j["peggedAssets"]:
        sym = a.get("symbol")
        if not sym:
            continue
        # 兼容两种历史结构：
        # 1) historicalCirculating 为 dict: {"YYYY-MM-DD": amount, ...}
        hist = a.get("historicalCirculating")
        if isinstance(hist, dict):
            it = hist.items()
            for k, v in it:
                try:
                    d = pd.to_datetime(k).date()
                except Exception:
                    # 有时 key 可能是 int 时间戳
                    d = pd.to_datetime(int(k), unit="s", utc=True).date()
                amt = float(v) if v is not None else 0.0
                rows_total.append({"date": d, "amount": amt})
                rows_coins.append({"date": d, "symbol": sym, "amount": amt})
        # 2) circulating 为 list: [{"date": 1705814400, "circulating": 123...}, ...]
        circ_list = a.get("circulating")
        if isinstance(circ_list, list) and circ_list:
            for pt in circ_list:
                if pt is None: 
                    continue
                ts = pt.get("date")
                val = pt.get("circulating") or pt.get("amount") or pt.get("value")
                if ts is None or val is None:
                    continue
                d = pd.to_datetime(ts, unit="s", utc=True).date() if isinstance(ts, (int,float)) else pd.to_datetime(ts).date()
                amt = float(val)
                rows_total.append({"date": d, "amount": amt})
                rows_coins.append({"date": d, "symbol": sym, "amount": amt})

    df_total = pd.DataFrame(rows_total)
    if df_total.empty:
        return pd.DataFrame(columns=["date","stable_total"]), pd.DataFrame(columns=["date","symbol","amount"])

    total = (df_total.groupby("date", as_index=False)["amount"]
                    .sum()
                    .rename(columns={"amount":"stable_total"}))
    coins = pd.DataFrame(rows_coins)
    return total, coins

