# trading_data_track

# Crypto Tracks (Lightweight)

Daily, config-driven crypto track reports (3Y daily): Chains (mcap, CG), Stablecoins (supply, DL), DeFi Total (TVL, DL), RWA (TVL, DL). Each track -> single HTML page with Total + Top5.

## Quick start (local)

1) Python 3.10+
2) Install
3) Edit `config/tracks.yml` (adjust Chains whitelist, etc.)
4) Run
5) Open `docs/index.html` (or `docs/tracks/*.html`)

## GitHub Pages
- Repo Settings -> Pages -> Build from branch -> main /docs
- After Actions push, visit:
  `https://<owner>.github.io/<repo>/`

## Email (GitHub Actions)
Set repo Secrets:
- `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`, `RECIPIENT_EMAIL`

Enable Actions. A scheduled run sends links daily.

## Tracks / Metrics
- Chains - L1: CoinGecko market cap (whitelist IDs)
- Stablecoins: DeFiLlama stablecoins total + top coins
- DeFi - Total: DeFiLlama total TVL + top protocols
- RWA: DeFiLlama RWA total + top projects

Add tracks by appending entries in `config/tracks.yml` (source: coingecko | defillama_defi_total | defillama_stablecoins | defillama_rwa). Future sectors (DEXes/Lending etc.) can be added similarly.

Data caveats: Free APIs, best-effort; values are indicative, not for trading.
