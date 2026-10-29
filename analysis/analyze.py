#!/usr/bin/env python3
"""
Turn results/*.json (and logs/port_stats.csv if present) into plots + a summary table.

    python3 analysis/analyze.py                 # reads results/, writes results/plots/
    python3 analysis/analyze.py --results some/dir

Plots
    goodput_vs_offered.png   the capacity knee
    loss_vs_offered.png      loss rises once offered load passes capacity
    jitter_vs_offered.png    queueing shows up as jitter near/after the knee
    competing_flows.png      E2: two flows sharing the bottleneck over time
    port_rates.png           controller-side view of the bottleneck port (if logs exist)
"""

import argparse
import csv
import glob
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"


def style(ax, title, xlabel, ylabel):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=12, fontweight="bold", pad=10)
    ax.set_xlabel(xlabel, color=INK2, fontsize=10)
    ax.set_ylabel(ylabel, color=INK2, fontsize=10)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9)


def save(fig, out_dir, name):
    fig.patch.set_facecolor(SURFACE)
    fig.tight_layout()
    path = os.path.join(out_dir, name)
    fig.savefig(path, dpi=180)
    plt.close(fig)
    print(f"  wrote {path}")


def load(path):
    with open(path) as fh:
        return json.load(fh)


def sweep_plots(sweep, capacity, out_dir):
    pts = sorted(((r["sender"]["offered_mbps"], r["receiver"]) for r in sweep),
                 key=lambda p: p[0])
    off = [p[0] for p in pts]
    good = [p[1]["goodput_mbps"] for p in pts]
    loss = [p[1]["loss_pct"] for p in pts]
    jit = [p[1]["jitter_ms"] for p in pts]
    top = max(off) * 1.08

    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.plot([0, top], [0, top], color=MUTED, lw=1, ls="--")
    ax.text(top * 0.97, top * 0.93, "ideal (no loss)", color=MUTED, fontsize=8, ha="right")
    if capacity:
        ax.axhline(capacity, color=MUTED, lw=1, ls=":")
        ax.text(top * 0.02, capacity * 1.02, f"bottleneck {capacity:g} Mbps",
                color=INK2, fontsize=8, va="bottom")
    ax.plot(off, good, color=BLUE, lw=2, marker="o", ms=6,
            markeredgecolor=SURFACE, markeredgewidth=2)
    style(ax, "Measured goodput vs offered load", "offered load (Mbps)", "goodput (Mbps)")
    ax.set_xlim(0, top)
    ax.set_ylim(0, top)
    save(fig, out_dir, "goodput_vs_offered.png")

    fig, ax = plt.subplots(figsize=(7, 4.2))
    if capacity:
        ax.axvline(capacity, color=MUTED, lw=1, ls=":")
    ax.plot(off, loss, color=BLUE, lw=2, marker="o", ms=6,
            markeredgecolor=SURFACE, markeredgewidth=2)
    style(ax, "Packet loss vs offered load", "offered load (Mbps)", "loss (%)")
    ax.set_xlim(0, top)
    ax.set_ylim(bottom=0)
    save(fig, out_dir, "loss_vs_offered.png")

    fig, ax = plt.subplots(figsize=(7, 4.2))
    if capacity:
        ax.axvline(capacity, color=MUTED, lw=1, ls=":")
    ax.plot(off, jit, color=BLUE, lw=2, marker="o", ms=6,
            markeredgecolor=SURFACE, markeredgewidth=2)
    style(ax, "Interarrival jitter (RFC 3550) vs offered load",
          "offered load (Mbps)", "jitter (ms)")
    ax.set_xlim(0, top)
    ax.set_ylim(bottom=0)
    save(fig, out_dir, "jitter_vs_offered.png")

    return list(zip(off, good, loss, jit))


def competing_plot(a, b, capacity, out_dir):
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for res, color, name in ((a, BLUE, "h1 → h3"), (b, ORANGE, "h2 → h4")):
        s = res["receiver"]["throughput_series_mbps"]
        t = list(range(1, len(s) + 1))
        ax.plot(t, s, color=color, lw=2, label=name)
        if s:
            ax.text(t[-1] + 0.15, s[-1], name, color=INK2, fontsize=8, va="center")
    if capacity:
        ax.axhline(capacity, color=MUTED, lw=1, ls=":")
        ax.text(0.6, capacity * 1.02, f"bottleneck {capacity:g} Mbps",
                color=INK2, fontsize=8, va="bottom")
    style(ax, "Two flows competing for the bottleneck", "time (s)", "goodput (Mbps)")
    ax.set_ylim(bottom=0)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK2, loc="lower right")
    save(fig, out_dir, "competing_flows.png")


def port_plot(csv_path, out_dir):
    rows = list(csv.DictReader(open(csv_path)))
    if not rows:
        return
    # pick the busiest (dpid, port) by total tx: that's the bottleneck egress
    totals = {}
    for r in rows:
        k = (r["dpid"], r["port"])
        totals[k] = totals.get(k, 0.0) + float(r["tx_mbps"])
    dpid, port = max(totals, key=totals.get)
    sel = [r for r in rows if r["dpid"] == dpid and r["port"] == port]
    t0 = float(sel[0]["time"])
    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.plot([float(r["time"]) - t0 for r in sel], [float(r["tx_mbps"]) for r in sel],
            color=BLUE, lw=2)
    style(ax, f"Controller view: s{dpid} port {port} transmit rate",
          "time (s)", "tx rate (Mbps)")
    ax.set_ylim(bottom=0)
    save(fig, out_dir, "port_rates.png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(ROOT, "results"))
    ap.add_argument("--logs", default=os.path.join(ROOT, "logs"))
    a = ap.parse_args()
    out_dir = os.path.join(a.results, "plots")
    os.makedirs(out_dir, exist_ok=True)

    meta_path = os.path.join(a.results, "meta.json")
    capacity = load(meta_path).get("bottleneck_mbps") if os.path.exists(meta_path) else None

    lines = ["| offered (Mbps) | goodput (Mbps) | loss (%) | jitter (ms) |",
             "|---:|---:|---:|---:|"]
    sweep_path = os.path.join(a.results, "e1_sweep.json")
    if os.path.exists(sweep_path):
        for o, g, l, j in sweep_plots(load(sweep_path), capacity, out_dir):
            lines.append(f"| {o:.2f} | {g:.2f} | {l:.2f} | {j:.3f} |")

    fa, fb = (os.path.join(a.results, f"e2_flow_{x}.json") for x in "ab")
    if os.path.exists(fa) and os.path.exists(fb):
        competing_plot(load(fa)[0], load(fb)[0], capacity, out_dir)

    port_csv = os.path.join(a.logs, "port_stats.csv")
    if os.path.exists(port_csv):
        port_plot(port_csv, out_dir)

    for extra in sorted(glob.glob(os.path.join(a.results, "e3_*.json"))):
        for r in load(extra):
            rr = r["receiver"]
            lines.append(f"| {r['sender']['offered_mbps']:.2f} (same switch) | "
                         f"{rr['goodput_mbps']:.2f} | {rr['loss_pct']:.2f} | "
                         f"{rr['jitter_ms']:.3f} |")

    summary = os.path.join(a.results, "summary.md")
    with open(summary, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"  wrote {summary}")


if __name__ == "__main__":
    main()
