"""Screen one row of A's supplied data for ranging and wall echo stability."""

import argparse
import csv
import json
import re
import struct
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
from matplotlib import pyplot as plt


SETTINGS = json.loads(Path("problem/pilot.json").read_text(encoding="utf-8"))
ROOT = Path(__file__).resolve().parents[5]
RX = SETTINGS["row_rx_indices_zero_based"]
REF = SETTINGS["reference_rx_index_in_selected_row"]
FS = 4e9
C = 299792458
N = 4000
RX_X = 0.035 * (8 - 2 * (np.array(RX) + 1))
DIRECT = np.abs(RX_X)
T = np.arange(N) / FS
REFERENCE = np.hamming(N) * np.cos(2 * np.pi * (0.3e9 * T + 0.5 * 1.4e15 * T**2))
BAND = (np.fft.fftfreq(N, d=1 / FS) >= 0.3e9) & (np.fft.fftfreq(N, d=1 / FS) <= 1.7e9)
MATCH = np.where(BAND, np.fft.fft(REFERENCE).conj(), 0)


def compress(path):
    """Use the first full packet in the selected row and fold four periods."""
    with path.open("rb") as file:
        magic, _, kind, _, counter, samples, width = struct.unpack("<IIIfIII", file.read(28))
        if (magic, kind, samples, width) != (963072, 102, 16000, 16):
            raise ValueError(f"unexpected packet at {path}")
        values = np.frombuffer(file.read(samples * width), dtype="<i2").reshape(4, N, 8)
    waveform = values.mean(axis=0)[:, RX].T
    return np.fft.ifft(MATCH * np.fft.fft(waveform, axis=-1), axis=-1), counter


def nearby_direct(response, offsets):
    reference = int(np.argmax(np.abs(response[REF])))
    expected = np.round(reference + offsets).astype(int)
    candidates = (expected[:, None] + np.arange(-3, 4)) % N
    maxima = np.argmax(np.abs(np.take_along_axis(response, candidates, axis=1)), axis=1)
    return candidates[np.arange(len(RX)), maxima]


def align(response, offsets):
    peaks = nearby_direct(response, offsets)
    shifted = np.array([np.roll(a, -int(i)) for a, i in zip(response, peaks)])
    return shifted / shifted[:, 0, None]


def remove_direct(response, offsets, template):
    out = align(response, offsets)
    # Taper subtraction outside the direct gate to preserve scene reflections.
    delay = np.minimum(np.arange(N), N - np.arange(N))
    taper = np.interp(delay, [0, 4, 11, N // 2], [1.0, 1.0, 0.0, 0.0])
    return np.abs(out - template * taper) ** 2


def sample(data, delays):
    return np.array([np.interp(t, np.arange(N), d, left=0, right=0) for d, t in zip(data, delays)])


def ranged(powers):
    lo, hi = SETTINGS["range_domain_m"]
    distances = np.arange(lo, hi + 0.001, 0.002)
    paths = np.sqrt(4 * distances[None, :] ** 2 + DIRECT[:, None] ** 2)
    profile = np.median(sample(powers, (paths - DIRECT[:, None]) * FS / C), axis=0)
    return float(distances[np.argmax(profile)])


def line_metrics(image, xs, ys):
    lo, hi = SETTINGS["front_search_y_m"]
    near = (ys >= lo) & (ys <= hi)
    lo, hi = SETTINGS["rear_search_y_m"]
    far = (ys >= lo) & (ys <= hi)
    segment = (xs >= 0.30) & (xs <= 1.0)
    front = ys[near][np.argmax(image[near, :][:, segment], axis=0)]
    x = xs[segment]
    reference_y = 0.37 + (0.56 - 0.37) * (x - 0.25) / (1.06 - 0.25)
    far_profile = np.median(image[far, :][:, segment], axis=1)
    peak = int(np.argmax(far_profile))
    return {
        "front_median_absolute_error_m": float(np.median(np.abs(front - reference_y))),
        "front_fitted_slope": float(np.polyfit(x, front, 1)[0]),
        "rear_apparent_y_m": float(ys[far][peak]),
        "rear_band_peak_to_median_db": float(10 * np.log10(
            max(far_profile[peak], 1e-20) / max(np.median(far_profile), 1e-20))),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gain", action="store_true",
                        help="apply a bounded linear amplitude gain referenced to 0.75 m")
    gain = parser.parse_args().gain
    suffix = f"*_{SETTINGS['row_file_suffix']}.dat"
    data = next((ROOT / "docs/校赛题目").glob("A*/附件"), None)
    if data is None:
        parser.error("缺少 A 题原始附件，应放在项目根目录 docs/校赛题目/ 下的 A 题目录中。")
    ranging = next(data.glob("*测距数据"))
    empty = [compress(f)[0] for f in sorted((ranging / "空场数据").glob(f"*/{suffix}"))]
    direct_peaks = [np.argmax(np.abs(z), axis=1) for z in empty]
    shifts = [(p - p[REF] + N // 2) % N - N // 2 for p in direct_peaks]
    offsets = np.median(shifts, axis=0)
    template = np.mean([align(z, offsets) for z in empty], axis=0)
    rows, range_summary = [], {}
    for material in ("墙体数据", "金属板数据"):
        train, test = [], []
        for f in sorted((ranging / material).glob(f"*/{suffix}")):
            group, distance = map(int, re.fullmatch(r"data(\d+)-(\d+)cm", f.parent.name).groups())
            wave, packet = compress(f)
            entry = {"material": material, "group": group, "packet": packet,
                     "guess_m": ranged(remove_direct(wave, offsets, template)), "reference_m": distance / 100}
            (train if group % 2 else test).append(entry)
        assert len(train) == len(test) == 10
        bias = float(np.median([r["reference_m"] - r["guess_m"] for r in train]))
        for part, entries in (("calibration", train), ("holdout", test)):
            for e in entries:
                e.update(split=part, prediction_m=e["guess_m"] + bias)
                e["absolute_error_m"] = abs(e["prediction_m"] - e["reference_m"])
        errors = [r["absolute_error_m"] for r in test]
        range_summary[material] = {"train_bias_m": bias, "holdout_n": len(test),
                                  "median_absolute_error_m": float(np.median(errors)),
                                  "mean_absolute_error_m": float(np.mean(errors)),
                                  "max_absolute_error_m": float(max(errors))}
        rows.extend(train + test)
    if not gain:
        with Path("results/ranging_groups.csv").open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    shots = sorted((next(data.glob("*成像数据")) / "墙体").glob(suffix))
    assert len(shots) == 33
    xs = np.linspace(-0.2, 1.4, 33)
    ys = np.linspace(0, 2.5, 126)
    x, y = np.meshgrid(xs, ys)
    maps = []
    for station, f in zip(xs, shots):
        wave, _ = compress(f)
        power = remove_direct(wave, offsets, template)
        tx_length = np.hypot(x - station, y)
        rx_length = np.hypot(x[None, ...] - (station + RX_X[:, None, None]), y[None, ...])
        delays = (tx_length[None, ...] + rx_length - DIRECT[:, None, None]) * FS / C
        amp_gain = np.clip((tx_length[None, ...] + rx_length) / 1.5, 1, 3) if gain else 1
        maps.append(np.mean(sample(power, delays) * amp_gain**2, axis=0))
    maps = np.array(maps)
    products = {"all": maps.mean(axis=0), "odd": maps[::2].mean(axis=0), "even": maps[1::2].mean(axis=0)}
    wall_summary = {k: line_metrics(v, xs, ys) for k, v in products.items()}
    figure, axes = plt.subplots(1, 3, figsize=(10, 4), layout="constrained", sharex=True, sharey=True)
    peak = float(np.max(products["all"]))
    for ax, (k, v) in zip(axes, products.items()):
        im = ax.pcolormesh(xs, ys, 10 * np.log10(np.maximum(v / peak, 1e-8)), vmin=-30, vmax=0)
        ax.set(title=k, xlabel="x (m)")
        ax.plot([0.25, 1.06], [0.37, 0.56], "w--", lw=1)
        ax.axhline(2.0, color="w", linestyle=":", lw=1)
    axes[0].set_ylabel("y (m)")
    figure.colorbar(im, ax=axes, label="Relative power (dB)")
    suffix = "_gain" if gain else ""
    figure.savefig(f"figures/wall_aperture_split{suffix}.png", dpi=160)
    plt.close(figure)

    range_pass = all(v["median_absolute_error_m"] <= SETTINGS["holdout_median_error_limit_m"]
                     for v in range_summary.values())
    rear_difference = abs(wall_summary["even"]["rear_apparent_y_m"] -
                          wall_summary["odd"]["rear_apparent_y_m"])
    wall_pass = rear_difference <= SETTINGS["rear_split_difference_limit_m"] and all(
        m["front_median_absolute_error_m"] <= SETTINGS["front_median_error_limit_m"]
        and m["front_fitted_slope"] > 0
        and abs(m["rear_apparent_y_m"] - SETTINGS["expected_rear_y_m"]) <=
        SETTINGS["rear_apparent_position_tolerance_m"]
        and m["rear_band_peak_to_median_db"] >= SETTINGS["rear_contrast_minimum_db"]
        for m in wall_summary.values()
    )
    output = {"ranging": range_summary, "walls": wall_summary, "ranging_pass": range_pass,
              "wall_pass": wall_pass, "rear_split_difference_m": rear_difference,
              "empty_direct_offsets_samples": offsets.tolist(), "empty_record_count": len(empty),
              "range_processing": "relative envelope and one offset fitted per material",
              "wall_processing": "air-path envelope back-projection",
              "bounded_amplitude_gain": gain}
    Path(f"results/radar_pilot{suffix}.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"ranging_pass={range_pass}, wall_pass={wall_pass}, rear_split_difference_m={rear_difference:.3f}")


if __name__ == "__main__":
    main()
