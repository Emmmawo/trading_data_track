# scripts/report_utils.py
from pathlib import Path
import plotly.io as pio

CSS = """
body { max-width: 1100px; margin: 20px auto; font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial; }
h1 { font-size: 22px; }
h2 { font-size: 18px; margin-top: 28px; }
.footer { color: #666; font-size: 12px; margin-top: 24px; }
.chart { border: 1px solid #eee; padding: 6px; border-radius: 6px; margin: 8px 0; }
"""

def fig_to_div(fig):
    return pio.to_html(fig, full_html=False, include_plotlyjs="cdn", default_height="480px")

def make_track_page(title, charts, footer_note=""):
    parts = [f"<h1>{title}</h1>"]
    for sub, div in charts:
        parts.append(f"<h2>{sub}</h2>")
        parts.append(f"<div class='chart'>{div}</div>")
    parts.append(f"<div class='footer'>Data sources: {footer_note}. Updated UTC.</div>")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>{CSS}</style></head><body>
{''.join(parts)}
</body></html>"""
    return html

def write_page(path: Path, html: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
