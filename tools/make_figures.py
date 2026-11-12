"""Figures for README / REPORT / notebook, drawn from the Azure SQL views (the same numbers as Power BI).

    python tools/make_figures.py        → docs/img/*.png
Colors: the validated reference palette (blue #2a78d6, orange #eb6834), text in ink colors, recessive grid,
one axis per chart, direct labels on the few marks that matter.
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from txfr.common import connect_sql  # noqa: E402

BLUE, ORANGE, INK, INK2, GRID, SURFACE = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
OUT = ROOT / "docs" / "img"
plt.rcParams.update({"font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2,
                     "ytick.color": INK2, "axes.titlecolor": INK, "axes.titleweight": "bold", "axes.titlesize": 12,
                     "axes.titlelocation": "left", "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
                     "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID,
                     "grid.linewidth": 0.8})


def frame(conn, q):
    cur = conn.cursor()
    cur.execute(q)
    cols = [d[0] for d in cur.description]
    df = pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)
    for c in df.columns:
        if df[c].dtype == object and df[c].dropna().map(lambda v: hasattr(v, "as_integer_ratio")).all():
            df[c] = df[c].astype(float)
    return df


def money(v):
    return f"${v / 1e9:,.1f}B" if abs(v) >= 1e9 else f"${v / 1e6:,.0f}M" if abs(v) >= 1e6 else f"${v / 1e3:,.0f}K"


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote", (OUT / name).relative_to(ROOT))


def exposure(conn):
    d = frame(conn, """SELECT TOP 12 agency_name, direct_cost_2yr, ai_cost_2yr, attributed_cost_2yr
                       FROM dw.v_agency_exposure WHERE attributed_cost_2yr > 0 ORDER BY attributed_cost_2yr DESC""")
    d = d.iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 5.2))
    y = range(len(d))
    ax.barh(y, d.direct_cost_2yr, color=BLUE, height=0.62, label="Direct (bill names only this agency)")
    ax.barh(y, d.ai_cost_2yr, left=d.direct_cost_2yr, color=ORANGE, height=0.62,
            edgecolor=SURFACE, linewidth=2, label="AI-attributed (verified quote)")
    ax.set_yticks(list(y), [n[:42] for n in d.agency_name])
    for i, v in enumerate(d.attributed_cost_2yr):
        ax.text(v, i, "  " + money(v), va="center", color=INK, fontsize=9)
    ax.xaxis.set_major_formatter(lambda v, _: money(v) if v else "0")
    ax.grid(axis="y", visible=False)
    ax.set_title("New 2026-27 cost from enacted 2025 bills, by agency (top 12)")
    ax.legend(loc="lower right", frameon=False)
    ax.margins(x=0.18)
    save(fig, "agency_exposure.png")


def readiness(conn):
    d = frame(conn, """SELECT TOP 12 agency_name, load_pct_of_spend FROM dw.v_agency_exposure
                       WHERE fy2024_spend > 5000000 AND load_pct_of_spend > 0 ORDER BY load_pct_of_spend DESC""").iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 5.2))
    ax.barh(range(len(d)), d.load_pct_of_spend, color=BLUE, height=0.62)
    ax.set_yticks(range(len(d)), [n[:42] for n in d.agency_name])
    for i, v in enumerate(d.load_pct_of_spend):
        ax.text(v, i, f"  {v:.1f}%", va="center", color=INK, fontsize=9)
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.grid(axis="y", visible=False)
    ax.set_title("Readiness: new yearly cost as % of FY2024 spending (agencies spending > $5M)")
    ax.margins(x=0.15)
    save(fig, "readiness.png")


def pace(conn):
    d = frame(conn, "SELECT session_code, day_of_prefiling, filed_cumulative FROM dw.v_filing_pace WHERE day_of_prefiling BETWEEN 0 AND 70")
    fig, ax = plt.subplots(figsize=(9, 4.2))
    for s, c, label in (("89R", BLUE, "2025 session (89R)"), ("90R", ORANGE, "2027 session (90R)")):
        x = d[d.session_code == s].sort_values("day_of_prefiling")
        if len(x):
            ax.plot(x.day_of_prefiling, x.filed_cumulative, color=c, linewidth=2, label=label,
                    marker="o" if len(x) < 3 else None, markersize=8)   # 2027 has only days 0-1 so far
            ax.annotate(f"{int(x.filed_cumulative.iloc[-1]):,}", (x.day_of_prefiling.iloc[-1], x.filed_cumulative.iloc[-1]),
                        textcoords="offset points", xytext=(4, 0), va="center", color=INK, fontsize=9)
    ax.set_xlabel("Days since prefiling opened")
    ax.set_ylabel("Bills filed (cumulative)")
    ax.set_title("Filing pace: 2027 vs 2025 at the same point")
    ax.legend(frameon=False, loc="upper left")
    save(fig, "filing_pace.png")


def funnel(conn):
    d = frame(conn, """SELECT SUM(filed) filed, SUM(referred) referred, SUM(passed_one_chamber) one_chamber,
                              SUM(passed_both) both_chambers, SUM(enacted) enacted, SUM(vetoed) vetoed
                       FROM dw.v_session_funnel WHERE session_code = '89R' AND bill_type IN ('HB', 'SB')""").iloc[0]
    labels = ["Filed", "Referred to committee", "Passed one chamber", "Passed both chambers", "Enacted", "Vetoed"]
    vals = [d.filed, d.referred, d.one_chamber, d.both_chambers, d.enacted, d.vetoed]
    fig, ax = plt.subplots(figsize=(9, 3.8))
    ax.barh(range(len(vals))[::-1], vals, color=BLUE, height=0.62)
    ax.set_yticks(range(len(vals))[::-1], labels)
    for i, v in zip(range(len(vals))[::-1], vals):
        ax.text(v, i, f"  {int(v):,} ({v / vals[0]:.1%})" if v / vals[0] < 0.01 else f"  {int(v):,} ({v / vals[0]:.0%})", va="center", color=INK, fontsize=9)
    ax.grid(axis="y", visible=False)
    ax.set_title("2025 regular session: House and Senate bills, how far they got")
    ax.margins(x=0.2)
    save(fig, "funnel_89r.png")


def drift(conn):
    d = frame(conn, """SELECT bill_id, first_cost_2yr, latest_cost_2yr, status FROM dw.v_estimate_drift
                       WHERE session_code = '89R' AND first_cost_2yr > 0 AND latest_cost_2yr > 0""")
    if d.empty:
        return
    fig, ax = plt.subplots(figsize=(6.5, 6))
    en = d.status == "enacted"
    ax.scatter(d.first_cost_2yr[~en], d.latest_cost_2yr[~en], s=18, color=BLUE, alpha=0.5, edgecolor=SURFACE, linewidth=0.5, label="Not enacted")
    ax.scatter(d.first_cost_2yr[en], d.latest_cost_2yr[en], s=22, color=ORANGE, alpha=0.8, edgecolor=SURFACE, linewidth=0.5, label="Enacted")
    lo, hi = 1e4, max(d.first_cost_2yr.max(), d.latest_cost_2yr.max()) * 1.5
    ax.plot([lo, hi], [lo, hi], color=INK2, linewidth=1, linestyle="--")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.xaxis.set_major_formatter(lambda v, _: money(v))
    ax.yaxis.set_major_formatter(lambda v, _: money(v))
    ax.set_xlabel("First fiscal-note estimate (2026-27 GR cost)")
    ax.set_ylabel("Latest estimate")
    ax.set_title("Estimate drift, 2025 bills with costs (dashed = unchanged)")
    ax.legend(frameon=False, loc="upper left")
    save(fig, "estimate_drift.png")


def main():
    from txfr.common import load_env
    load_env()
    conn = connect_sql()
    for f in (funnel, pace, exposure, readiness, drift):
        f(conn)


if __name__ == "__main__":
    main()
