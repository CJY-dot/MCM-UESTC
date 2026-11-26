"""Exercise the installed Workbench with synthetic inputs in a temporary workspace."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".agents" / "skills"
MTD = SKILLS / "mathodology-workbench" / "scripts" / "mtd.py"
PRESETS = SKILLS / "mathodology-figure-presets" / "templates"

CALCULATE = '''\
import json
import os
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import scipy
from scipy.stats import linregress

data = pd.read_csv("problem/data/raw/smoke.csv")
seed = int(os.environ["MATHODOLOGY_SEED"])
rng = np.random.default_rng(seed)
y = data.y + rng.normal(0, 0.01, size=len(data))
slope = float(linregress(data.x, y).slope)
assert abs(slope - 2) < 0.1
result = {
    "schema": "mathodology.results/1",
    "question": "Q1",
    "values": {"slope": {"value": slope, "digits": 4}},
    "tables": {"summary": {"columns": ["Metric", "Value"], "rows": [["Slope", slope]], "digits": 4}},
    "provenance": {"fixture": "synthetic", "seed": seed, "versions": {
        package.__name__: package.__version__ for package in (np, pd, scipy, matplotlib)
    }},
}
Path("results/smoke.json").write_text(json.dumps(result), encoding="utf8")
'''

PLOT = '''\
import json
from pathlib import Path

import numpy as np
from matplotlib_templates import forecast_fan

slope = json.loads(Path("results/smoke.json").read_text(encoding="utf8"))["values"]["slope"]["value"]
x = np.arange(3)
y = slope * x
fig = forecast_fan(x, y, y - 0.5, y + 0.5, interval_label="Synthetic fixture band")
fig.savefig("figures/smoke.png", dpi=240)
'''


def main():
    with TemporaryDirectory(prefix="mcm-smoke-环境-") as temp:
        ws = Path(temp)
        env = dict(os.environ, PYTHONPATH=str(PRESETS), MPLBACKEND="Agg",
                   PYTHONDONTWRITEBYTECODE="1")

        def mtd(*args, expected=0):
            proc = subprocess.run([sys.executable, str(MTD), *args], cwd=ws, env=env,
                                  capture_output=True, text=True, encoding="utf8", timeout=330)
            if proc.returncode != expected:
                raise RuntimeError(f"{args}: exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
            return proc.stdout

        mtd("init", "--paper-template", "mcm", "--no-hooks")
        (ws / "problem/smoke.md").write_text("# Q1. Check the synthetic fixture\n", encoding="utf8")
        mtd("brief")
        brief = ws / "problem/questions.json"
        questions = json.loads(brief.read_text(encoding="utf8"))
        assert [q["id"] for q in questions["questions"]] == ["Q1"]
        questions["questions"][0].update(deliverables=["Slope and chart"], metrics=["Slope"])
        brief.write_text(json.dumps(questions), encoding="utf8")
        data = ws / "problem/data/raw/smoke.csv"
        data.parent.mkdir(parents=True)
        data.write_text("x,y\n0,1\n1,3\n2,5\n", encoding="utf8")
        (ws / "code/smoke.py").write_text(CALCULATE, encoding="utf8")
        (ws / "figures/smoke.py").write_text(PLOT, encoding="utf8")
        mtd("run", "--name", "compute", "--seed", "42", "--", sys.executable, "code/smoke.py")
        mtd("run", "--name", "plot", "--seed", "42", "--", sys.executable, "figures/smoke.py")
        mtd("sync")
        manifests = {p.parent.name.rsplit("-", 1)[-1]: json.loads(p.read_text(encoding="utf8"))
                     for p in (ws / "runs").glob("*/manifest.json")}
        assert len(manifests) == 2 and all(r["seed"] == 42 and r["exit_code"] == 0 for r in manifests.values())
        assert "problem/data/raw/smoke.csv" in manifests["compute"]["inputs"]
        assert "results/smoke.json" in manifests["compute"]["outputs"]
        assert "figures/smoke.png" in manifests["plot"]["outputs"]
        for run in manifests.values():
            assert all((ws / log).is_file() for log in run["logs"].values())
        assert "smoke.slope" in (ws / "paper/results.tex").read_text(encoding="utf8")
        assert (ws / "paper/tables/smoke-summary.tex").is_file()

        main_tex = ws / "paper/main.tex"
        paper = main_tex.read_text(encoding="utf8").replace(
            "We ...", r"Synthetic slope: \res{smoke.slope}."
            "\n" r"\input{tables/smoke-summary}"
            "\n" r"\includegraphics[width=.45\textwidth]{smoke.png}")
        main_tex.write_text(paper, encoding="utf8")
        report = json.loads(mtd("check", "--compile", "--strict", "--json"))
        assert not any(i["code"].endswith(".unavailable") for i in report["issues"]), report["issues"]
        assert "1 traced to a script" in next(s for s in report["stages"] if s["id"] == "figures")["summary"]
        assert (ws / "paper/main.pdf").read_bytes().startswith(b"%PDF-")
        print("PASS: question, seeded runs, input/output manifests, plot trace, macros, table and PDF")

        main_tex.write_text(paper.replace("smoke.slope", "smoke.unknown"), encoding="utf8")
        bad_macro = json.loads(mtd("check", "--json", expected=1))
        assert any(i["severity"] == "error" and "smoke.unknown" in i["message"]
                   for i in bad_macro["issues"]), bad_macro["issues"]
        main_tex.write_text(paper, encoding="utf8")
        data.write_text("x,y\n0,1\n1,3\n2,6\n", encoding="utf8")
        figure = ws / "figures/smoke.png"
        older = (ws / "results/smoke.json").stat().st_mtime - 5
        os.utime(figure, (older, older))
        stale = json.loads(mtd("check", "--strict", "--json", expected=1))
        codes = {i["code"] for i in stale["issues"]}
        assert {"runs.stale-inputs", "figures.stale"} <= codes, codes
        print("PASS: unknown result rejected and changed input/stale figure detected")


if __name__ == "__main__":
    main()
