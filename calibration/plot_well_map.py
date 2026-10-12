"""Top-down reference map of the well labels and the calibrated nozzle X/Y for each (Z50)."""
import json
from collections import defaultdict
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrow

HERE = Path(__file__).parent
# fitted 12 mm grid per island: the same values as the tables in Model_Hardware_API_spec.md
wells = {k: {"xy": [v["x"], v["y"]]} for k, v in
         json.load(open(HERE / "well_positions_fit.json"))["wells"].items()}
ISLAND_NAME = {"C": "C  (centre)", "P1": "P1  (back-left)", "P2": "P2  (back-right)",
               "P3": "P3  (front-right)", "P4": "P4  (front-left)"}

fig, ax = plt.subplots(figsize=(11, 11.5), facecolor="white")
ax.set_facecolor("#1d2430")  # dark bed

islands = defaultdict(list)
for k, v in wells.items():
    islands[k.split("-")[0]].append(v["xy"])
for isl, pts in islands.items():
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs) - 7, max(xs) + 7, min(ys) - 7, max(ys) + 7
    ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fc="#e9edf2", ec="#9aa5b1", lw=1.2, zorder=1))
    above = isl in ("P1", "P2", "C")
    ax.text((x0 + x1) / 2, y1 + 2.5 if above else y0 - 2.5, ISLAND_NAME[isl], ha="center",
            va="bottom" if above else "top", color="white", fontsize=13, fontweight="bold")

for k, v in wells.items():
    x, y = v["xy"]
    ax.add_patch(Rectangle((x - 5, y - 5), 10, 10, fc="#2b3545", ec="none", zorder=2))
    well = k.split("-")[1]
    ax.text(x, y + 1.6, well, ha="center", va="center", color="#ffd166", fontsize=12.5,
            fontweight="bold", zorder=3)
    ax.text(x, y - 2.4, f"{x:.2f}\n{y:.2f}", ha="center", va="center", color="#cfd8e3",
            fontsize=7.2, linespacing=1.05, zorder=3)

ax.set_xlim(55, 205); ax.set_ylim(88, 240); ax.set_aspect("equal")
ax.set_xlabel("printer X (mm) — nozzle command", fontsize=11)
ax.set_ylabel("printer Y (mm) — nozzle command", fontsize=11)
ax.grid(color="white", alpha=0.08)
ax.text(130, 90.5, "FRONT of printer", ha="center", va="bottom", color="#8fa3b8", fontsize=11)
ax.text(130, 237.5, "BACK of printer", ha="center", va="top", color="#8fa3b8", fontsize=11)
ax.add_patch(FancyArrow(178, 132, 12, 0, width=0.6, head_width=3, color="#8fa3b8"))
ax.add_patch(FancyArrow(178, 132, 0, 12, width=0.6, head_width=3, color="#8fa3b8"))
ax.text(195.5, 132, "+X", color="#8fa3b8", va="center", fontsize=10)
ax.text(178, 149.5, "+Y", color="#8fa3b8", ha="center", fontsize=10)
# legend for the numbers inside a well
ax.add_patch(Rectangle((60, 128), 14, 14, fc="#2b3545", ec="#9aa5b1", lw=0.8))
ax.text(67, 137.5, "label", ha="center", va="center", color="#ffd166", fontsize=9, fontweight="bold")
ax.text(67, 132, "X\nY", ha="center", va="center", color="#cfd8e3", fontsize=7.2, linespacing=1.05)
ax.text(76, 135, "nozzle X, Y (mm)\nfor camera over\nthe well centre", va="center",
        color="#cfd8e3", fontsize=8.5)
fig.suptitle("Well labels and camera positions (top view)", fontsize=18, fontweight="bold", y=0.99)
ax.set_title("Each well shows its label and the nozzle X / Y that puts the camera straight above "
             "its centre.\nAll at Z = 50 mm (G90, after homing). Rows A→D run back→front, columns "
             "1→4 run left→right.", fontsize=10.5, color="#444", pad=10)
fig.tight_layout(rect=(0, 0, 1, 0.965))
fig.savefig(HERE.parent / "well_map.png", dpi=150)
print("saved", HERE.parent / "well_map.png")
