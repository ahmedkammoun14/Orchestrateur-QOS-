"""
Genere deux figures TikZ a partir des donnees reelles :

  fig4_trajectory.tex : trajectoire du vehicule + position des 8 cibles,
                        avec les zones de couverture (rayon ou la latence
                        passe sous 28 ms).

  fig5_latency.tex    : latence subie au cours du tour, federe vs ablation,
                        avec la ligne du seuil. Sous-echantillonne pour
                        rester lisible et compilable.

Aucune valeur n'est inventee : tout vient de latences_session.csv et des
fichiers de temps deja valides.
"""
import csv, datetime, openpyxl, math

VMS = ["edge1", "edge1b", "edge1c", "edge2", "edge2b", "edge2c", "cloud1", "cloud2"]
POS = {"edge1": (3, -9), "edge1b": (34, 19), "edge1c": (-6, 51),
       "edge2": (31, -8), "edge2b": (4, 23), "edge2c": (-23, 30),
       "cloud1": (-4, 34), "cloud2": (18, 4)}
P1 = {"edge1", "edge1b", "edge1c", "cloud1"}
THRESHOLD = 28.0
TZ = 2

# Rayon de conformite edge : L(d) = (d-3)/77*(150-5)+5 = 28  ->  d
R_EDGE = 3 + (THRESHOLD - 5) * 77 / 145


def load_traj(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))[1:]
    out = []
    for r in rows:
        if len(r) != 19:
            continue
        h, m, s = map(int, r[0].split(":"))
        vals = {vm: float(r[3 + i]) for i, vm in enumerate(VMS) if r[3 + i]}
        out.append((h * 3600 + m * 60 + s, float(r[1]), float(r[2]), vals))
    return out


def load_hosts(*paths):
    ev = []
    for p in paths:
        wb = openpyxl.load_workbook(p, read_only=True)
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(min_row=1, max_row=3, values_only=True))
        hdr, cur = [], [None] * 3
        for c in range(ws.max_column):
            for r in range(3):
                v = rows[r][c] if c < len(rows[r]) else None
                if v is not None:
                    cur[r] = v
                    for k in range(r + 1, 3):
                        cur[k] = None
            hdr.append(" > ".join(str(x) for x in cur if x))
        i = {n: k for k, n in enumerate(hdr)}
        for r in ws.iter_rows(min_row=4, values_only=True):
            if r[0] is None or r[i["Horodatage (UTC)"]] is None:
                continue
            try:
                t = datetime.datetime.fromisoformat(str(r[i["Horodatage (UTC)"]]))
            except ValueError:
                continue
            ev.append((t.hour * 3600 + t.minute * 60 + t.second,
                       r[i["VM active (source)"]]))
        wb.close()
    ev.sort()
    return ev


def host_at(ev, t):
    best = None
    for et, vm in ev:
        if et <= t:
            best = vm
        else:
            break
    return best


# ─────────────────── FIGURE 4 : trajectoire ───────────────────
traj = load_traj("data_UC1_federe/latences_session.csv")
step = max(1, len(traj) // 400)
pts = [(x, y) for _, x, y, _ in traj[::step]]

lines = [
    "% Figure 4 --- trajectoire du consommateur et couverture des cibles.",
    "% Genere depuis data_UC1_federe/latences_session.csv (donnees reelles).",
    "\\begin{tikzpicture}[font=\\scriptsize, scale=0.075]",
    "  \\draw[->,black!55] (-32,0) -- (50,0) node[right,font=\\tiny]{$x$ (cm)};",
    "  \\draw[->,black!55] (0,-24) -- (0,60) node[above,font=\\tiny]{$y$ (cm)};",
]
# zones de couverture edge
for vm, (x, y) in POS.items():
    if vm.startswith("cloud"):
        continue
    col = "blue!12" if vm in P1 else "orange!14"
    lines.append(f"  \\fill[{col}] ({x},{y}) circle ({R_EDGE:.1f});")
# trajectoire
path = " -- ".join(f"({x:.1f},{y:.1f})" for x, y in pts)
lines.append(f"  \\draw[black!70, line width=0.35pt] {path};")
# cibles
for vm, (x, y) in POS.items():
    # Formes TikZ pures (shapes.geometric) : 'diamond*' / 'mark size' sont des
    # cles pgfplots et ne compilent pas dans un tikzpicture simple.
    shape = "diamond" if vm.startswith("cloud") else "circle"
    col = ("black!55" if vm.startswith("cloud")
           else ("blue!70!black" if vm in P1 else "orange!80!black"))
    lines.append(f"  \\node[{shape}, fill={col}, draw={col}, inner sep=1.1pt] "
                 f"at ({x},{y}) {{}};")
    lines.append(f"  \\node[font=\\tiny, text={col}, anchor=south] "
                 f"at ({x},{y+2}) {{{vm}}};")
lines.append("  \\node[font=\\tiny,anchor=north west,text=black!60] at (-31,59)")
lines.append(f"    {{shaded: latency $<{THRESHOLD:.0f}$\\,ms (radius {R_EDGE:.0f}\\,cm)}};")
lines.append("\\end{tikzpicture}")

with open("paper/fig4_trajectory.tex", "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(f"fig4_trajectory.tex ecrit ({len(pts)} points, rayon {R_EDGE:.1f} cm)")


# ─────────────────── FIGURE 5 : latence subie ───────────────────
def served_series(dossier, timing_files, pool):
    """
    Latence subie, en fonction du temps ecoule DEPUIS LE DEMARRAGE DE
    L'ORCHESTRATEUR.

    L'origine des abscisses est le premier evenement de la chronologie, pas le
    premier echantillon de trajectoire : le PiCar peut tourner avant le
    lancement (21 min d'avance sur data_ABL_run1). Prendre l'origine dans la
    trajectoire decalerait toute la courbe et la rendrait incomparable aux
    autres runs. Les echantillons hors fenetre sont ecartes, comme dans
    violation_rate.py.
    """
    traj = load_traj(f"{dossier}/latences_session.csv")
    ev = load_hosts(*[f"{dossier}/{f}" for f in timing_files])
    debut, fin = ev[0][0], ev[-1][0]
    out = []
    for t, x, y, vals in traj:
        utc = t - TZ * 3600
        if not (debut <= utc <= fin):
            continue
        vm = host_at(ev, utc)
        if vm and vm in vals and vm in pool:
            out.append(((utc - debut) / 60.0, vals[vm]))
    return out


TIMINGS_FED = ["timings_autonomous_provider1.xlsx",
               "timings_autonomous_provider2.xlsx"]
TIMINGS_ABL = ["timings_autonomous_provider1.xlsx"]

# 4+4 : campagne complete au 18/08/2026. Un style uniforme par bras (plutot
# que "premier run en gras, second en clair" comme au stade 2+2) -- avec
# quatre traces superposees, c'est la DENSITE de recouvrement elle-meme qui
# montre la reproductibilite, sans avoir a designer un run "principal".
FED_RUNS = ["data_UC1_federe", "data_FED_run1", "data_FED_run2", "data_FED_run3"]
ABL_RUNS = ["data_UC2_ablation", "data_ABL_run1", "data_ABL_run2", "data_ABL_run3"]

fed_series = [served_series(d, TIMINGS_FED, set(VMS)) for d in FED_RUNS]
abl_series = [served_series(d, TIMINGS_ABL, P1) for d in ABL_RUNS]

WIN = 40.0          # fenetre commune aux 8 runs (le plus court, UC2, fait 40,5 min)
YMAX = 160.0


def emit(series, n=180):
    s = [(t, v) for t, v in series if t <= WIN]
    k = max(1, len(s) // n)
    return " -- ".join(f"({t:.2f},{min(v, YMAX):.1f})" for t, v in s[::k])


lines = [
    "% Figure 5 --- latence subie par le consommateur au cours du tour.",
    "% Huit runs reels : 4 federes (bleu) et 4 ablation (orange), meme style",
    "% par bras -- la densite de recouvrement montre la reproductibilite.",
    "\\begin{tikzpicture}[font=\\scriptsize, xscale=0.188, yscale=0.032]",
    f"  \\fill[red!7] (0,{THRESHOLD}) rectangle ({WIN},{YMAX});",
    f"  \\draw[->,black!55] (0,0) -- ({WIN+1.8},0)"
    "    node[right,font=\\tiny]{time (min)};",
    f"  \\draw[->,black!55] (0,0) -- (0,{YMAX+12})"
    "    node[above,font=\\tiny]{latency (ms)};",
]
for yv in (0, 28, 60, 100, 140):
    lines.append(f"  \\draw[black!45] (-0.5,{yv}) -- (0,{yv}) "
                 f"node[left,font=\\tiny,xshift=-1pt]{{{yv}}};")
for xv in (0, 10, 20, 30, 40):
    lines.append(f"  \\draw[black!45] ({xv},-3) -- ({xv},0) "
                 f"node[below,font=\\tiny,yshift=-1pt]{{{xv}}};")
lines.append(f"  \\draw[red!65,dashed,line width=0.5pt] (0,{THRESHOLD}) -- ({WIN},{THRESHOLD})"
             f"    node[right,font=\\tiny,red!70]{{SLO}};")
for s in abl_series:
    lines.append(f"  \\draw[orange!75!black, opacity=0.55, line width=0.28pt] {emit(s)};")
for s in fed_series:
    lines.append(f"  \\draw[blue!65!black, opacity=0.55, line width=0.32pt] {emit(s)};")
lines.append("  \\node[font=\\tiny,blue!70!black,anchor=west] at (0.6,152) "
             "{federated (4 runs)};")
lines.append("  \\node[font=\\tiny,orange!80!black,anchor=west] at (14.0,152) "
             "{ablated (4 runs)};")
lines.append("\\end{tikzpicture}")

with open("paper/fig5_latency.tex", "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(f"fig5_latency.tex ecrit — federe {[len(s) for s in fed_series]} pts, "
      f"ablation {[len(s) for s in abl_series]} pts")
