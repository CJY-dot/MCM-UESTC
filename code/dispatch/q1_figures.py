"""Data-driven Q1 plots. Only aggregate result fields enter public figures."""

from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator


ROOT = Path(__file__).resolve().parents[2]
CRANE_COLORS = ("#0072B2", "#E69F00", "#009E73", "#CC79A7", "#565656")
POLICY_COLORS = ("#686868", "#0072B2")
WAITING = (
    ("pickup", "Ready to pickup", "#0072B2"),
    ("path", "In transit", "#E69F00"),
    ("drop", "Arrival to drop", "#009E73"),
    ("seat", "Seat to service", "#CC79A7"),
)
STYLE = {
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": "#E7EAF0",
    "grid.linewidth": 0.6,
    "pdf.fonttype": 42,
    "savefig.dpi": 300,
}


def _save(fig, output, name, title, note):
    fig.suptitle(title, fontsize=12, weight="bold", x=0, ha="left")
    fig.supxlabel(note, fontsize=8, ha="left", x=0, color="#444444")
    paths = [output / f"{name}.{extension}" for extension in ("png", "pdf")]
    for path in paths:
        metadata = {"CreationDate": None} if path.suffix == ".pdf" else None
        fig.savefig(path, bbox_inches="tight", metadata=metadata)
    plt.close(fig)
    return paths


def _policy_label(policy):
    key = str(policy).lower()
    if key == "fcfs":
        return "FCFS"
    if key in {"margin", "slack", "tightest", "least_slack", "tightest_margin"}:
        return "Tightest margin"
    return str(policy).replace("_", " ").capitalize()


def _minute(value):
    return value / 60


def _private_directory(path):
    output = Path(path).resolve()
    allowed = (ROOT / "local", ROOT / "problem" / "data" / "processed")
    if not any(output.is_relative_to(directory.resolve()) for directory in allowed):
        raise ValueError("Detailed Q1 figures must stay under local/ or problem/data/processed/")
    output.mkdir(parents=True, exist_ok=True)
    return output


def private_figures(run: dict, output: Path) -> list[Path]:
    """Render detailed Gantt, trajectories and continuous pairwise gaps locally."""
    output = _private_directory(output)
    tracks = defaultdict(list)
    for segment in run["trajectories"]:
        tracks[segment["crane_id"]].append(segment)
    for track in tracks.values():
        track.sort(key=lambda t: t["t0_s"])
    order = sorted(tracks, key=lambda crane: tracks[crane][0]["x0"])
    colors = {crane: CRANE_COLORS[k] for k, crane in enumerate(order)}
    tasks = run["tasks"]
    unit = run["unit"]
    threshold = run["gap_threshold"]
    time_end = max((segment["t1_s"] for segment in run["trajectories"]), default=0)
    time_start = min((segment["t0_s"] for segment in run["trajectories"]), default=0)
    source = run.get("source", "Q1W06 frozen historical replay")
    note = (
        f"{source} | {run['scenario']} parameters | {_policy_label(run['policy'])}\n"
        "Conditional batch plan. Time is relative to the planning origin."
    )

    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(8.0, max(3, 1.7 + 0.23 * len(tasks))), layout="constrained")
        for index, task in enumerate(tasks):
            start, finish = task["pickup_start_s"], task["drop_complete_s"]
            if start is not None and finish is not None:
                color = colors.get(task["crane_id"], POLICY_COLORS[0])
                if finish == start:
                    ax.scatter(_minute(start), index, color=color, marker="D", s=20, zorder=3)
                else:
                    ax.barh(index, _minute(finish - start), left=_minute(start), color=color, height=0.62)
            else:
                status = str(task["status"]).replace("_", " ")
                ax.text(0.015, index, status, transform=ax.get_yaxis_transform(),
                        color="#4B5563", va="center", fontsize=8)
        ax.set_yticks(range(len(tasks)), [f"T{i + 1:02d}" for i in range(len(tasks))])
        ax.invert_yaxis()
        ax.yaxis.grid(False)
        ax.set(xlabel="Time (min)", ylabel="Task order in result")
        if time_end > time_start:
            ax.set_xlim(_minute(time_start), _minute(time_end))
        ax.legend(handles=[Patch(color=colors[c], label=c) for c in order],
                  loc="lower left", bbox_to_anchor=(0, 1.01), ncols=len(order) or 1,
                  frameon=False, fontsize=8)
        paths = _save(fig, output, "q1_task_gantt", "Q1 task schedule", note)

        fig, ax = plt.subplots(figsize=(8.0, 3.4), layout="constrained")
        for crane in order:
            points = tracks[crane]
            times = [points[0]["t0_s"], *(p["t1_s"] for p in points)]
            positions = [points[0]["x0"], *(p["x1"] for p in points)]
            ax.plot([_minute(t) for t in times], positions, color=colors[crane], linewidth=1.4, label=crane)
        ax.set(xlabel="Time (min)", ylabel=f"EF longitudinal position ({unit})")
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.11), ncols=len(order) or 1, frameon=False)
        paths += _save(fig, output, "q1_crane_trajectories", "Five EF crane trajectories", note)

        fig, ax = plt.subplots(figsize=(8.0, 3.4), layout="constrained")
        for index, (left, right) in enumerate(zip(order, order[1:])):
            left_track, right_track = tracks[left], tracks[right]
            times = sorted({s[k] for s in [*left_track, *right_track] for k in ("t0_s", "t1_s")})
            gaps = [_position(right_track, t) - _position(left_track, t) for t in times]
            ax.plot([_minute(t) for t in times], gaps, color=CRANE_COLORS[index],
                    linewidth=1.35, label=f"{left} / {right}")
        ax.axhline(threshold, color="#A02A35", linestyle="--", linewidth=1,
                   label=f"Assumed threshold ({threshold:g})")
        ax.set(xlabel="Time (min)", ylabel=f"Adjacent reference-position gap ({unit})")
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.17), ncols=3, frameon=False, fontsize=8)
        paths += _save(fig, output, "q1_continuous_gap", "Continuous gaps on the EF track", note)
    return paths


def _position(track, time):
    for segment in track:
        if segment["t0_s"] <= time <= segment["t1_s"]:
            t0, t1 = segment["t0_s"], segment["t1_s"]
            if t1 == t0:
                return segment["x1"]
            return segment["x0"] + (segment["x1"] - segment["x0"]) * (time - t0) / (t1 - t0)
    raise ValueError(f"No EF trajectory at t={time}")


def _bar_label(ax, bar, numerator, denominator=None):
    if numerator is None:
        label = "n/a"
    elif denominator is not None:
        label = f"{numerator}/{denominator}"
    else:
        label = f"{numerator:g}"
    ax.annotate(label, (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 2.5), textcoords="offset points", ha="center", va="bottom", fontsize=8)


def public_figures(summary: dict, output: Path) -> list[Path]:
    """Render only aggregate waiting, strategy and one-factor sensitivity plots."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    runs = summary["runs"]
    scenarios = list(dict.fromkeys(run["scenario"] for run in runs))
    policies = list(dict.fromkeys(run["policy"] for run in runs))
    colors = {policy: POLICY_COLORS[index % len(POLICY_COLORS)] for index, policy in enumerate(policies)}
    note = (
        f"{summary.get('source', 'Q1W06 frozen historical replay')} | Conditional batch planning\n"
        "Completed-task totals and plan targets use the reported scenario denominators."
    )
    labels = [
        f"{run['scenario']}\n{_policy_label(run['policy'])}\n"
        f"{run['metrics']['completed_ef']}/{run['metrics']['target_ef']} EF"
        for run in runs
    ]
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(max(7.2, len(runs) * 1.08), 3.9), layout="constrained")
        bottoms = [0.0] * len(runs)
        for key, label, color in WAITING:
            values = [run["metrics"]["waiting_s"][key] for run in runs]
            for index, value in enumerate(values):
                if value is not None:
                    ax.bar(index, _minute(value), bottom=bottoms[index], color=color, width=0.68)
                    bottoms[index] += _minute(value)
        for index, run in enumerate(runs):
            reported = run["metrics"]["waiting_s"]
            if any(reported[k] is None for k, *_ in WAITING):
                ax.annotate("partial", (index, bottoms[index]), xytext=(0, 2.5),
                            textcoords="offset points", ha="center", va="bottom", fontsize=8)
        ax.set_xticks(range(len(runs)), labels, fontsize=8)
        ax.set_ylabel("Steel-ladle waiting (min)")
        ax.legend(handles=[Patch(color=c, label=label) for _, label, c in WAITING],
                  loc="upper center", bbox_to_anchor=(0.5, 1.13), ncols=4, frameon=False, fontsize=8)
        ax.margins(y=0.15)
        paths = _save(fig, output, "q1_waiting_breakdown", "Waiting by stage and policy", note)

        fig, axes = plt.subplots(2, 2, figsize=(8, 5.8), layout="constrained")
        index = {(run["scenario"], run["policy"]): run["metrics"] for run in runs}
        width = 0.72 / len(policies)
        for ax, (key, denom, title) in zip(axes.flat, (
            ("completed_ef", "target_ef", "EF tasks completed (%)"),
            ("on_time", "plan_targets", "On-time plan targets (%)"),
            ("late_total_s", None, "Completed-task delay (min)"),
            ("empty_run_s", None, "Crane movement (min)"),
        )):
            for i, scenario in enumerate(scenarios):
                for j, policy in enumerate(policies):
                    metrics = index.get((scenario, policy))
                    if metrics is None:
                        continue
                    x = i + (j - (len(policies) - 1) / 2) * width
                    value = metrics[key]
                    if denom:
                        total = metrics[denom]
                        height = 100 * value / total if total and value is not None else 0
                        bar = ax.bar(x, height, width=width, color=colors[policy])[0]
                        _bar_label(ax, bar, value if total else None, total)
                    elif key == "empty_run_s":
                        empty, loaded = _minute(value), _minute(metrics["loaded_run_s"])
                        ax.bar(x, loaded, width=width, color=colors[policy])
                        ax.bar(x, empty, bottom=loaded, width=width, color=colors[policy],
                               hatch="///", edgecolor="white", linewidth=0)
                    else:
                        bar = ax.bar(x, _minute(value), width=width, color=colors[policy])[0]
                        _bar_label(ax, bar, round(_minute(value), 1))
            ax.set_xticks(range(len(scenarios)), scenarios, fontsize=8)
            ax.set_ylabel(title)
            if denom:
                ax.set_ylim(0, 116)
            else:
                ax.margins(y=0.18)
        handles = [Patch(facecolor=colors[p], label=_policy_label(p)) for p in policies]
        handles.append(Patch(facecolor="#777777", edgecolor="white", hatch="///", label="Empty movement"))
        axes[0, 0].legend(handles=handles, frameon=False, ncols=len(handles),
                          loc="lower left", bbox_to_anchor=(0, 1.05), fontsize=8)
        paths += _save(fig, output, "q1_strategy_comparison", "Paired policy outcomes", note)

        sensitivity = defaultdict(list)
        for row in summary["sensitivity"]:
            sensitivity[row["parameter"]].append(row)
        columns = min(3, len(sensitivity))
        if columns == 0:
            return paths
        rows = (len(sensitivity) + columns - 1) // columns
        fig, axes = plt.subplots(rows, columns, figsize=(3.0 * columns, 2.35 * rows + 0.55),
                                 sharey=True, squeeze=False, layout="constrained")
        for ax, (parameter, records) in zip(axes.flat, sensitivity.items()):
            points = list(dict.fromkeys((row["level"], row["value"]) for row in records))
            if all(isinstance(v, (int, float)) for _, v in points):
                points.sort(key=lambda p: p[1])
            for j, policy in enumerate(policies):
                observations = {(r["level"], r["value"]): r["metrics"] for r in records if r["policy"] == policy}
                counts = [observations[p]["on_time"] if p in observations else None for p in points]
                ax.plot(range(len(points)), [float("nan") if value is None else value for value in counts],
                        color=colors[policy], marker=("o", "s")[j % 2], linestyle=("--", "-")[j % 2],
                        linewidth=(1.8, 1.15)[j % 2], markersize=(6, 3.5)[j % 2],
                        markerfacecolor="none")
            ax.set_title(str(parameter).replace("_", " "))
            ax.set_xticks(range(len(points)), [_level_label(level, value) for level, value in points], fontsize=8)
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        for ax in list(axes.flat)[len(sensitivity):]:
            ax.set_visible(False)
        for ax in axes[:, 0]:
            ax.set_ylabel("On-time plan targets (tasks)")
        handles = [Line2D([], [], color=colors[p], label=_policy_label(p), marker=("o", "s")[i % 2],
                          markerfacecolor="none", linestyle=("--", "-")[i % 2])
                   for i, p in enumerate(policies)]
        axes[0, 0].legend(handles=handles, frameon=False, loc="lower left",
                          bbox_to_anchor=(0, 1.22), ncols=len(policies))
        paths += _save(fig, output, "q1_parameter_sensitivity", "One-factor sensitivity under fixed task scope", note)
    return paths


def _level_label(level, value):
    if isinstance(value, (int, float)):
        return f"{level}\n{value:g}"
    if str(value).casefold() == str(level).casefold():
        return str(level).replace("_", " ")
    return f"{level}\n{value}"
