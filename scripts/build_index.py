# scripts/build_index.py
# usage: python -m scripts.build_index --docs docs/tracks
import argparse
from pathlib import Path
from datetime import datetime

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=str, default="docs/tracks")
    args = ap.parse_args()
    d = Path(args.docs)
    links = []
    for p in sorted(d.glob("*.html")):
        name = p.name
        links.append(f"<li><a href='tracks/{name}'>{name.replace('.html','')}</a></li>")
    body = f"""<!doctype html><html><head><meta charset="utf-8"><title>Tracks Index</title></head>
<body>
<h1>Daily Tracks Reports</h1>
<ul>
{''.join(links)}
</ul>
<p>Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
</body></html>"""
    Path("docs").mkdir(parents=True, exist_ok=True)
    Path("docs/index.html").write_text(body, encoding="utf-8")
    # 禁用 Jekyll
    (Path("docs") / ".nojekyll").write_text("", encoding="utf-8")

if __name__ == "__main__":
    main()
