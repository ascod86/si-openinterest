"""
Открытый интерес Si (MOEX) — дневной и 4H суммарный график.
Данные: MOEX ISS API (бесплатно, задержка 14 дней).
Запуск: python3 fetch_oi.py
"""

import requests
import pandas as pd
import plotly.graph_objects as go
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "futoi_si.csv")
CHART_FILE = os.path.join(os.path.dirname(__file__), "si_openinterest.html")

TILL = date.today()
FROM = date(2024, 1, 1)

# Целевые точки за день для 4H (берём ближайший снимок к этому времени)
TARGET_TIMES_4H = ["12:00", "16:00", "20:00", "23:50"]

TOKEN = open(os.path.join(os.path.dirname(__file__), "token.txt")).read().strip()

SESSION = requests.Session()
SESSION.headers["Authorization"] = f"Bearer {TOKEN}"


def minutes(t: str) -> int:
    """'HH:MM' → минуты от полуночи."""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def fetch_day(d: date) -> list:
    """Скачивает все строки за день, возвращает строки в 4 временных точках."""
    url = (
        f"https://apim.moex.com/iss/analyticalproducts/futoi/securities/Si.json"
        f"?from={d}&till={d}&limit=1000"
    )
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        rows = resp.json()["futoi"]["data"]
        if not rows:
            return []
        if len(rows) == 1000:
            more = SESSION.get(url + "&start=1000", timeout=15).json()["futoi"]["data"]
            rows.extend(more)
    except Exception as e:
        print(f"  Ошибка {d}: {e}")
        return []

    # Строим словарь seqnum → время
    seq_to_time = {}
    for r in rows:
        seq = r[1]
        t = r[3][:5]  # "HH:MM"
        if seq not in seq_to_time:
            seq_to_time[seq] = t

    # Для каждой целевой точки — ближайший seqnum
    target_seqs = set()
    for target in TARGET_TIMES_4H:
        tgt_min = minutes(target)
        best = min(seq_to_time.keys(),
                   key=lambda s: abs(minutes(seq_to_time[s]) - tgt_min))
        target_seqs.add(best)

    return [r for r in rows if r[1] in target_seqs]


def trading_days(from_date: date, till_date: date) -> list:
    days = []
    cur = from_date
    while cur <= till_date:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def load_all_data() -> pd.DataFrame:
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    COLS = ["sess_id", "seqnum", "tradedate", "tradetime", "ticker",
            "clgroup", "pos", "pos_long", "pos_short",
            "pos_long_num", "pos_short_num", "systime", "trade_session_date"]

    existing = pd.DataFrame()
    last_date = None

    if os.path.exists(CACHE_FILE):
        existing = pd.read_csv(CACHE_FILE)
        last_date = date.fromisoformat(existing["trade_session_date"].dropna().max())
        print(f"Кэш: {len(existing)} строк, последняя дата {last_date}")

    fetch_from = FROM if last_date is None else last_date + timedelta(days=1)

    if fetch_from > TILL:
        print("Кэш актуален.")
        return existing

    days = trading_days(fetch_from, TILL)
    print(f"Скачиваю {len(days)} торговых дней ({fetch_from} – {TILL})...")

    all_rows = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_day, d): d for d in days}
        done = 0
        for future in as_completed(futures):
            all_rows.extend(future.result())
            done += 1
            if done % 100 == 0 or done == len(days):
                print(f"  {done}/{len(days)}...")

    if not all_rows:
        print("Нет новых данных.")
        return existing

    new_df = pd.DataFrame(all_rows, columns=COLS)
    combined = pd.concat([existing, new_df], ignore_index=True).drop_duplicates()
    combined.to_csv(CACHE_FILE, index=False)
    print(f"Сохранено {len(combined)} строк.")
    return combined


def compute_oi(df: pd.DataFrame) -> pd.DataFrame:
    """Суммирует ОИ (физлица + юрлица) для каждого снимка."""
    df = df.copy()
    df["oi"] = df["pos_long"] + df["pos_short"].abs()
    # Суммируем FIZ + YUR по каждому снимку (seqnum + дата)
    snap = df.groupby(["trade_session_date", "seqnum", "tradetime"])["oi"].sum().reset_index()
    snap["datetime"] = pd.to_datetime(snap["trade_session_date"] + " " + snap["tradetime"])
    return snap.sort_values("datetime").reset_index(drop=True)


def build_1d(snap: pd.DataFrame) -> pd.DataFrame:
    """Последний снимок за каждый день."""
    idx = snap.groupby("trade_session_date")["seqnum"].idxmax()
    daily = snap.loc[idx].copy()
    daily["change"] = daily["oi"].diff()
    return daily.reset_index(drop=True)


def plot(snap: pd.DataFrame):
    daily = build_1d(snap)

    # Цвета для баров изменений
    def bar_colors(series):
        return ["#4CAF50" if v >= 0 else "#F44336" for v in series.fillna(0)]

    fig = go.Figure()

    # === 1D: линия ОИ ===
    fig.add_trace(go.Scatter(
        x=daily["datetime"],
        y=daily["oi"],
        name="ОИ (1D)",
        visible=True,
        line=dict(color="#2196F3", width=2),
        hovertemplate="<b>%{x|%d.%m.%Y}</b><br>ОИ: %{y:,.0f}<br>Изм: %{customdata:+,.0f}<extra></extra>",
        customdata=daily["change"],
    ))
    # 1D: бары изменений
    fig.add_trace(go.Bar(
        x=daily["datetime"],
        y=daily["change"],
        name="Изменение (1D)",
        visible=True,
        marker_color=bar_colors(daily["change"]),
        opacity=0.7,
        yaxis="y2",
        hovertemplate="<b>%{x|%d.%m.%Y}</b><br>%{y:+,.0f}<extra></extra>",
    ))

    # === 4H: снимки 12:00, 16:00, 20:00, 23:50 ===
    snap_4h = snap.copy()
    snap_4h["change"] = snap_4h["oi"].diff()

    fig.add_trace(go.Scatter(
        x=snap_4h["datetime"],
        y=snap_4h["oi"],
        name="ОИ (4H)",
        visible=False,
        line=dict(color="#FF9800", width=1.5),
        hovertemplate="<b>%{x|%d.%m %H:%M}</b><br>ОИ: %{y:,.0f}<br>Изм: %{customdata:+,.0f}<extra></extra>",
        customdata=snap_4h["change"],
    ))
    fig.add_trace(go.Bar(
        x=snap_4h["datetime"],
        y=snap_4h["change"],
        name="Изменение (4H)",
        visible=False,
        marker_color=bar_colors(snap_4h["change"]),
        opacity=0.7,
        yaxis="y2",
        hovertemplate="<b>%{x|%d.%m %H:%M}</b><br>%{y:+,.0f}<extra></extra>",
    ))

    fig.update_layout(
        title=dict(text="Si — Суммарный открытый интерес", x=0.5, xanchor="center"),
        xaxis=dict(
            rangeslider=dict(visible=True),
            rangeselector=dict(
                bgcolor="#263238",
                activecolor="#1565C0",
                bordercolor="#546E7A",
                borderwidth=1,
                font=dict(color="white", size=12),
                buttons=[
                    dict(count=1, label="1М", step="month", stepmode="backward"),
                    dict(count=3, label="3М", step="month", stepmode="backward"),
                    dict(count=6, label="6М", step="month", stepmode="backward"),
                    dict(count=1, label="1Y",  step="year",  stepmode="backward"),
                    dict(step="all", label="Всё"),
                ],
            ),
        ),
        yaxis=dict(title="Открытые позиции", tickformat=","),
        yaxis2=dict(title="Изменение", overlaying="y", side="right", showgrid=False),
        legend=dict(x=0.01, y=0.99),
        hovermode="x unified",
        height=620,
        template="plotly_dark",
    )

    # Кастомные кнопки поверх графика через HTML
    plot_html = fig.to_html(include_plotlyjs="cdn", full_html=False, div_id="oi_chart")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Si — Открытый интерес</title>
<style>
  body {{ margin: 0; background: #111; font-family: Arial, sans-serif; }}
  #controls {{
    display: flex; justify-content: center; align-items: center;
    gap: 8px; padding: 12px 0 4px;
  }}
  .tf-btn {{
    padding: 6px 20px; font-size: 14px; font-weight: bold;
    border: 2px solid #546E7A; border-radius: 6px;
    background: #263238; color: #90A4AE;
    cursor: pointer; transition: all 0.15s;
  }}
  .tf-btn:hover {{ border-color: #90CAF9; color: white; }}
  .tf-btn.active {{ background: #1565C0; border-color: #2196F3; color: white; }}
</style>
</head>
<body>
<div id="controls">
  <button class="tf-btn active" onclick="setTF('1D', this)">1D</button>
  <button class="tf-btn"        onclick="setTF('4H', this)">4H</button>
</div>
{plot_html}
<script>
function setTF(tf, btn) {{
  document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  var visible = tf === '1D'
    ? [true, true, false, false]
    : [false, false, true, true];
  Plotly.restyle('oi_chart', {{ visible: visible }});
}}
</script>
</body>
</html>"""

    with open(CHART_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"График: {CHART_FILE}")


if __name__ == "__main__":
    try:
        df = load_all_data()
        snap = compute_oi(df)
        daily = build_1d(snap)
        print(f"\n{len(daily)} торговых дней")
        print(f"Последняя точка: {daily.iloc[-1]['trade_session_date']}  ОИ = {daily.iloc[-1]['oi']:,.0f}")
        plot(snap)
        import webbrowser
        webbrowser.open(f"file://{CHART_FILE}")
        print("\nГотово! График открыт в браузере.")
    except Exception as e:
        print(f"\nОШИБКА: {e}")
        import traceback
        traceback.print_exc()
    finally:
        input("\nНажмите Enter для выхода...")
