"""
Открытый интерес на MOEX по парам USD/RUB и CNY/RUB — календарный и вечный фьючерс.
Карточка с переключателем ₽/$ ↔ ₽/¥. Для каждой пары показывает 2 последних
торговых дня и изменение ОИ от конца предыдущего дня к концу следующего.

Календарный и вечный показаны РАЗДЕЛЬНО (ОИ в контрактах, у них разный размер
контракта/ГО — суммировать нельзя). В скобках — изменение в деньгах (Δконтрактов × ГО),
а в итоге деньги обеих веток складываются (рубли — общий знаменатель).

Реалтайм через Algopack (apim.moex.com).

Запуск вручную:   python3 fetch_oi.py
Запуск по cron:   python3 fetch_oi.py --no-open
"""

import requests
import os
import sys
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CARD_FILE = os.path.join(BASE_DIR, "si_openinterest.html")

# Пары: код в futoi (Algopack) у юаня (CR) отличается от FORTS assetcode (CNY).
PAIRS = [
    {
        "key": "usd", "title": "USD/RUB", "tab": "$ USD/RUB",
        "cal":  {"futoi": "Si",      "name": "Si · календарные",      "hint": "квартальные контракты", "forts_asset": "Si"},
        "perp": {"futoi": "USDRUBF", "name": "USDRUBF · вечный",      "hint": "перпетуал",             "forts_secid": "USDRUBF"},
    },
    {
        "key": "cny", "title": "CNY/RUB", "tab": "¥ CNY/RUB",
        "cal":  {"futoi": "CR",      "name": "CNY · календарные",     "hint": "квартальные контракты", "forts_asset": "CNY"},
        "perp": {"futoi": "CNYRUBF", "name": "CNYRUBF · вечный",      "hint": "перпетуал",             "forts_secid": "CNYRUBF"},
    },
]

FUTOI_CODES = [PAIRS[i][leg]["futoi"] for i in range(len(PAIRS)) for leg in ("cal", "perp")]
CAL_ASSETS = {p["cal"]["forts_asset"] for p in PAIRS}
PERP_SECIDS = {p["perp"]["forts_secid"] for p in PAIRS}

def load_token() -> str:
    """Читает ключ из token.txt рядом с программой. Если файла нет — просит вставить и сохраняет.
    Понимает и ошибочное имя token.txt.txt, и BOM от Блокнота."""
    for name in ("token.txt", "token.txt.txt"):
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            t = open(path, encoding="utf-8-sig").read().strip()
            if t:
                return t
    if sys.stdin and sys.stdin.isatty():
        print("Файл token.txt рядом с программой не найден.")
        print("Вставь свой ключ Algopack (data.moex.com) и нажми Enter:")
        t = input("> ").strip()
        if t:
            with open(os.path.join(BASE_DIR, "token.txt"), "w", encoding="utf-8") as f:
                f.write(t)
            print("Ключ сохранён в token.txt — в следующий раз вводить не нужно.\n")
            return t
    raise SystemExit("Нет ключа. Положи файл token.txt с ключом Algopack рядом с программой.")


TOKEN = load_token()
SESSION = requests.Session()
SESSION.headers["Authorization"] = f"Bearer {TOKEN}"

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def fetch_asset(asset: str, frm: date, till: date) -> list:
    """Снимки открытого интереса одного актива за период."""
    url = (
        f"https://apim.moex.com/iss/analyticalproducts/futoi/securities/{asset}.json"
        f"?from={frm}&till={till}&limit=1000&iss.meta=off"
    )
    rows = SESSION.get(url, timeout=20).json()["futoi"]["data"]
    return [r for r in rows if len(r) >= 13]  # отсекаем служебные строки-ошибки


def daily_oi() -> tuple:
    """ОИ (сумма лонгов) на последнем срезе каждого дня по всем активам.
    Возвращает (by_day: {date: {ticker: oi}}, times: {date: 'HH:MM:SS'})."""
    till = date.today()
    frm = till - timedelta(days=10)

    rows = []
    for code in FUTOI_CODES:
        rows.extend(fetch_asset(code, frm, till))

    # seqnum общий для всех активов — последний срез дня один на всех
    last_seq = {}
    for r in rows:
        d, seq = r[12], r[1]
        if d not in last_seq or seq > last_seq[d][0]:
            last_seq[d] = (seq, r[3])

    by_day = {}
    for r in rows:
        d, seq, ticker, pos_long = r[12], r[1], r[4], r[7]
        if seq == last_seq[d][0]:
            day = by_day.setdefault(d, {})
            day[ticker] = day.get(ticker, 0) + pos_long

    return by_day, {d: last_seq[d][1] for d in last_seq}


def get_margins() -> tuple:
    """ГО (₽/контракт) из FORTS: по календарным (фронт с макс. OI) и по вечным.
    Возвращает (cal_im: {assetcode: ГО}, perp_im: {secid: ГО})."""
    url = (
        "https://iss.moex.com/iss/engines/futures/markets/forts/securities.json"
        "?iss.meta=off&iss.only=securities,marketdata"
        "&securities.columns=SECID,ASSETCODE,INITIALMARGIN"
        "&marketdata.columns=SECID,OPENPOSITION"
    )
    try:
        d = SESSION.get(url, timeout=20).json()
        oi = {r[0]: (r[1] or 0) for r in d["marketdata"]["data"]}
        cal_im, cal_best, perp_im = {}, {}, {}
        for secid, asset, im in d["securities"]["data"]:
            if secid in PERP_SECIDS:
                perp_im[secid] = im
            if asset in CAL_ASSETS and oi.get(secid, 0) > cal_best.get(asset, -1):
                cal_best[asset], cal_im[asset] = oi.get(secid, 0), im
        return cal_im, perp_im
    except Exception:
        return {}, {}


def fmt_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month]}"


def nf(v) -> str:
    return f"{v:,.0f}".replace(",", " ")


def fmt_money(v: float) -> str:
    """Рубли в компактном виде: млрд / млн / тыс."""
    a = abs(v)
    if a >= 1e9:
        s = f"{v / 1e9:.2f} млрд"
    elif a >= 1e6:
        s = f"{v / 1e6:.1f} млн"
    else:
        s = f"{v / 1e3:.0f} тыс"
    return s.replace(".", ",") + " ₽"


def leg_block(name: str, hint: str, p_date: str, l_date: str, p_val: int, l_val: int, margin) -> str:
    """Одна ветка (календарный ИЛИ вечный) — самостоятельный блок, без суммирования."""
    delta = l_val - p_val
    pct = (delta / p_val * 100) if p_val else 0
    up = delta >= 0
    color = "#16c784" if up else "#ea3943"
    arrow = "▲" if up else "▼"
    sign = "+" if up else "−"
    money = f' <span class="money">(≈ {sign}{fmt_money(abs(delta) * margin)} ГО)</span>' if margin else ""
    return f"""
      <div class="leg">
        <div class="leg-head"><span class="leg-name">{name}</span><span class="leg-hint">{hint}</span></div>
        <div class="row">
          <div class="cell">
            <div class="cl">Предыдущий · {fmt_date(p_date)}</div>
            <div class="cv">{nf(p_val)}</div>
          </div>
          <div class="arr">→</div>
          <div class="cell latest" style="border-color:{color}55">
            <div class="cl">Последний · {fmt_date(l_date)}</div>
            <div class="cv">{nf(l_val)}</div>
          </div>
        </div>
        <div class="leg-delta" style="color:{color}">{arrow} {sign}{nf(abs(delta))} <span class="pct">· {sign}{abs(pct):.2f}%</span>{money}</div>
      </div>"""


def build_panel(pair: dict, by_day: dict, times: dict, cal_im: dict, perp_im: dict) -> str:
    cal, perp = pair["cal"], pair["perp"]
    p, l = sorted(by_day)[-2:]
    p_cal, l_cal = by_day[p].get(cal["futoi"], 0), by_day[l].get(cal["futoi"], 0)
    p_perp, l_perp = by_day[p].get(perp["futoi"], 0), by_day[l].get(perp["futoi"], 0)
    c_margin, p_margin = cal_im.get(cal["forts_asset"]), perp_im.get(perp["forts_secid"])

    legs = (
        leg_block(cal["name"], cal["hint"], p, l, p_cal, l_cal, c_margin)
        + leg_block(perp["name"], perp["hint"], p, l, p_perp, l_perp, p_margin)
    )

    total_html = ""
    if c_margin and p_margin:
        net = (l_cal - p_cal) * c_margin + (l_perp - p_perp) * p_margin
        up = net >= 0
        t_color = "#16c784" if up else "#ea3943"
        t_sign = "+" if up else "−"
        t_word = "чистый приток на рынок" if up else "чистый отток с рынка"
        total_html = f"""
      <div class="total" style="border-color:{t_color}55">
        <div class="total-label">Итого по деньгам · ГО<div class="total-sub">{t_word}</div></div>
        <div class="total-val" style="color:{t_color}">{t_sign}{fmt_money(abs(net))}</div>
      </div>"""

    hidden = "" if pair["key"] == "usd" else " hidden"
    return f"""
    <section class="card" data-pair="{pair['key']}"{hidden}>
      <div class="title">Открытый интерес · {pair['title']}</div>
      <div class="subtitle">Контракты, по последнему срезу дня. Календарный и вечный — раздельно (разный ГО,
        в один итог не суммируются). В скобках — изменение в деньгах: Δконтрактов × ГО на контракт.</div>
      {legs}
      {total_html}
      <div class="footer">Срез {times[l][:5]} · обновлено {datetime.now().strftime('%d.%m.%Y %H:%M')}</div>
    </section>"""


def build_card(by_day: dict, times: dict):
    cal_im, perp_im = get_margins()
    tabs = "".join(
        f'<button class="tab{" active" if p["key"]=="usd" else ""}" data-pair="{p["key"]}">{p["tab"]}</button>'
        for p in PAIRS
    )
    panels = "".join(build_panel(p, by_day, times, cal_im, perp_im) for p in PAIRS)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>Открытый интерес USD/RUB · CNY/RUB</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; padding: 24px; gap: 16px;
    background: radial-gradient(1200px 600px at 50% -10%, #1b2430 0%, #0d1117 60%);
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #e6edf3;
  }}
  .tabs {{
    display: inline-flex; gap: 4px; padding: 4px;
    background: #161b22; border: 1px solid #232a33; border-radius: 12px;
  }}
  .tab {{
    border: 0; cursor: pointer; padding: 8px 18px; border-radius: 9px;
    font-size: 14px; font-weight: 700; color: #8b949e; background: transparent;
    font-family: inherit; transition: .15s;
  }}
  .tab:hover {{ color: #e6edf3; }}
  .tab.active {{ background: #1f6feb; color: #fff; }}
  .card {{
    width: 560px; max-width: 92vw;
    background: #161b22; border: 1px solid #232a33; border-radius: 20px;
    padding: 30px 36px 24px; box-shadow: 0 24px 60px rgba(0,0,0,.45);
  }}
  .card[hidden] {{ display: none; }}
  .title {{ font-size: 22px; font-weight: 700; }}
  .subtitle {{ margin-top: 5px; font-size: 12.5px; color: #8b949e; line-height: 1.45; }}
  .leg {{
    margin-top: 18px; background: #0d1117; border: 1px solid #232a33;
    border-radius: 14px; padding: 16px 18px 14px;
  }}
  .leg-head {{ display: flex; align-items: baseline; justify-content: space-between; }}
  .leg-name {{ font-size: 15px; font-weight: 700; }}
  .leg-hint {{ font-size: 11.5px; color: #6e7681; }}
  .row {{ display: flex; align-items: stretch; gap: 12px; margin: 12px 0 10px; }}
  .cell {{ flex: 1; background: #11161d; border: 1px solid #232a33; border-radius: 10px; padding: 10px 12px; }}
  .cell .cl {{ font-size: 11px; color: #8b949e; }}
  .cell .cv {{ margin-top: 6px; font-size: 24px; font-weight: 800; font-variant-numeric: tabular-nums; }}
  .arr {{ display: flex; align-items: center; color: #6e7681; font-size: 18px; }}
  .leg-delta {{ text-align: right; font-size: 18px; font-weight: 800; font-variant-numeric: tabular-nums; }}
  .leg-delta .pct {{ font-size: 14px; font-weight: 700; }}
  .leg-delta .money {{ font-size: 13px; font-weight: 700; opacity: .92; }}
  .total {{
    margin-top: 18px; display: flex; align-items: center; justify-content: space-between;
    background: #0d1117; border: 1px solid #232a33; border-radius: 14px; padding: 16px 20px;
  }}
  .total-label {{ font-size: 14px; font-weight: 700; color: #c9d1d9; }}
  .total-sub {{ margin-top: 3px; font-size: 11.5px; font-weight: 500; color: #8b949e; }}
  .total-val {{ font-size: 26px; font-weight: 800; font-variant-numeric: tabular-nums; }}
  .footer {{ margin-top: 18px; text-align: center; font-size: 12px; color: #6e7681; }}
</style>
</head>
<body>
  <div class="tabs">{tabs}</div>
  {panels}
<script>
  document.querySelectorAll('.tab').forEach(function (t) {{
    t.addEventListener('click', function () {{
      document.querySelectorAll('.tab').forEach(function (x) {{ x.classList.remove('active'); }});
      t.classList.add('active');
      document.querySelectorAll('.card').forEach(function (c) {{
        c.hidden = (c.dataset.pair !== t.dataset.pair);
      }});
    }});
  }});
</script>
</body>
</html>"""

    with open(CARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    by_day, times = daily_oi()
    if len(by_day) < 2:
        print("Недостаточно данных: нужно минимум 2 торговых дня.")
        sys.exit(1)

    build_card(by_day, times)

    p, l = sorted(by_day)[-2:]
    for pair in PAIRS:
        cal, perp = pair["cal"]["futoi"], pair["perp"]["futoi"]
        dc = by_day[l].get(cal, 0) - by_day[p].get(cal, 0)
        dp = by_day[l].get(perp, 0) - by_day[p].get(perp, 0)
        print(f"{pair['title']}: кал {dc:+,}  вечн {dp:+,}")
    print(f"Карточка: {CARD_FILE}")

    if "--no-open" not in sys.argv:
        import webbrowser
        webbrowser.open(f"file://{CARD_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nОшибка: {e}")
        print("Проверь интернет и что ключ в token.txt действителен (data.moex.com).")
        sys.exit(1)
