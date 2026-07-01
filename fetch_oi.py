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
import json
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

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


def fetch_day(d: date):
    """ОИ (сумма лонгов) на последнем срезе одного дня по всем активам.
    apim игнорирует start на диапазоне, поэтому качаем строго по одному дню
    (в дне ~340 строк на актив — влезает в лимит без пагинации).
    Возвращает (dateISO, {ticker: oi}, 'HH:MM:SS') или None, если данных нет."""
    rows = []
    for code in FUTOI_CODES:
        url = (
            f"https://apim.moex.com/iss/analyticalproducts/futoi/securities/{code}.json"
            f"?from={d}&till={d}&limit=1000&iss.meta=off"
        )
        page = SESSION.get(url, timeout=20).json()["futoi"]["data"]
        rows.extend(r for r in page if len(r) >= 13)  # отсекаем служебные строки-ошибки
    if not rows:
        return None

    last = max(r[1] for r in rows)  # последний seqnum (конец дня), общий для всех активов
    ltime = next(r[3] for r in rows if r[1] == last)
    oi = {}
    for r in rows:
        if r[1] == last:
            oi[r[4]] = oi.get(r[4], 0) + r[7]  # r[4]=ticker, r[7]=pos_long
    return str(d), oi, ltime


def daily_oi() -> tuple:
    """ОИ на последнем срезе каждого торгового дня за последние ~2 недели.
    Возвращает (by_day: {date: {ticker: oi}}, times: {date: 'HH:MM:SS'})."""
    till = date.today()
    frm = till - timedelta(days=14)
    days = [frm + timedelta(days=i) for i in range((till - frm).days + 1)
            if (frm + timedelta(days=i)).weekday() < 5]

    by_day, times = {}, {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(fetch_day, days):
            if res:
                d, oi, ltime = res
                by_day[d], times[d] = oi, ltime
    return by_day, times


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


def build_panel(pair: dict, p_date: str, l_date: str, by_day: dict, times: dict,
                cal_im: dict, perp_im: dict, day_index: int, visible: bool) -> str:
    cal, perp = pair["cal"], pair["perp"]
    p_cal, l_cal = by_day[p_date].get(cal["futoi"], 0), by_day[l_date].get(cal["futoi"], 0)
    p_perp, l_perp = by_day[p_date].get(perp["futoi"], 0), by_day[l_date].get(perp["futoi"], 0)
    c_margin, p_margin = cal_im.get(cal["forts_asset"]), perp_im.get(perp["forts_secid"])

    legs = (
        leg_block(cal["name"], cal["hint"], p_date, l_date, p_cal, l_cal, c_margin)
        + leg_block(perp["name"], perp["hint"], p_date, l_date, p_perp, l_perp, p_margin)
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

    hidden = "" if visible else " hidden"
    return f"""
    <section class="card" data-pair="{pair['key']}" data-day="{day_index}"{hidden}>
      <div class="title">Открытый интерес · {pair['title']}</div>
      <div class="subtitle">Контракты, по последнему срезу дня. Календарный и вечный — раздельно (разный ГО,
        в один итог не суммируются). В скобках — изменение в деньгах: Δконтрактов × ГО на контракт.</div>
      {legs}
      {total_html}
      <div class="footer">Срез {times[l_date][:5]} · обновлено {datetime.now().strftime('%d.%m.%Y %H:%M')}</div>
    </section>"""


def build_card(by_day: dict, times: dict):
    cal_im, perp_im = get_margins()
    tabs = "".join(
        f'<button class="tab{" active" if p["key"]=="usd" else ""}" data-pair="{p["key"]}">{p["tab"]}</button>'
        for p in PAIRS
    )

    dates = sorted(by_day)
    # Виды: для каждого v сравниваем dates[v] (предыдущий) → dates[v+1] (показываемый день).
    views = list(range(len(dates) - 1))
    latest = views[-1]
    panels = ""
    for v in views:
        p_date, l_date = dates[v], dates[v + 1]
        for pair in PAIRS:
            visible = (v == latest and pair["key"] == "usd")
            panels += build_panel(pair, p_date, l_date, by_day, times,
                                   cal_im, perp_im, v, visible)
    day_labels = json.dumps([fmt_date(dates[v + 1]) for v in views], ensure_ascii=False)

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
  .daynav {{ display: inline-flex; align-items: center; gap: 12px; }}
  .navbtn {{
    border: 1px solid #232a33; cursor: pointer; padding: 7px 14px; border-radius: 9px;
    font-size: 13px; font-weight: 700; color: #c9d1d9; background: #161b22;
    font-family: inherit; transition: .15s;
  }}
  .navbtn:hover:not(:disabled) {{ border-color: #1f6feb; color: #fff; }}
  .navbtn:disabled {{ opacity: .35; cursor: default; }}
  .daylabel {{ font-size: 13px; font-weight: 700; color: #8b949e; min-width: 150px; text-align: center; }}
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
  <div class="daynav">
    <button id="prevDay" class="navbtn">◀ Пред. день</button>
    <span id="dayLabel" class="daylabel"></span>
    <button id="nextDay" class="navbtn">След. день ▶</button>
  </div>
  {panels}
<script>
  var DAYS = {day_labels};
  var MAXDAY = DAYS.length - 1;
  var pair = 'usd', day = MAXDAY;

  // восстановить выбор после авто-обновления страницы (offset от последнего дня)
  try {{
    var s = JSON.parse(sessionStorage.getItem('oiState') || '{{}}');
    if (s.pair) pair = s.pair;
    if (typeof s.offset === 'number') day = Math.max(0, MAXDAY - s.offset);
  }} catch (e) {{}}

  function render() {{
    document.querySelectorAll('.tab').forEach(function (t) {{
      t.classList.toggle('active', t.dataset.pair === pair);
    }});
    document.querySelectorAll('.card').forEach(function (c) {{
      c.hidden = !(c.dataset.pair === pair && +c.dataset.day === day);
    }});
    document.getElementById('dayLabel').textContent = 'Показан день: ' + DAYS[day];
    document.getElementById('prevDay').disabled = (day <= 0);
    document.getElementById('nextDay').disabled = (day >= MAXDAY);
    try {{ sessionStorage.setItem('oiState', JSON.stringify({{ pair: pair, offset: MAXDAY - day }})); }} catch (e) {{}}
  }}

  document.querySelectorAll('.tab').forEach(function (t) {{
    t.addEventListener('click', function () {{ pair = t.dataset.pair; render(); }});
  }});
  document.getElementById('prevDay').addEventListener('click', function () {{ if (day > 0) {{ day--; render(); }} }});
  document.getElementById('nextDay').addEventListener('click', function () {{ if (day < MAXDAY) {{ day++; render(); }} }});
  render();
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
