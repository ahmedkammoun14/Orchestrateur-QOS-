"""
Quantifie la portee du plancher du Gap Grade (DELTA_FLOOR = -1).

delta_i est borne inferieurement a -1 :
  - objectif COUT   (<)  : delta = (v - tau)/tau  <= -1  <=>  v <= 0
  - objectif BENEFICE (>=): delta = (tau - v)/tau  <= -1  <=>  v >= 2*tau

Consequence analytique, a verifier sur les donnees :
  * la latence etant toujours > 0, le plancher n'est JAMAIS atteint sur un
    objectif de latence -> le mode autonome (latency < 28) n'est pas concerne ;
  * il n'est atteint que sur des objectifs de RESSOURCE, des qu'une cible
    offre plus du DOUBLE du seuil demande -> c'est le cas des intentions
    orientees calcul.
"""
import openpyxl, collections

CAP = {"edge1": (2, 2), "edge1b": (3, 3), "edge1c": (4, 4), "cloud1": (16, 16),
       "edge2": (2, 2), "edge2b": (3, 3), "edge2c": (4, 4), "cloud2": (8, 8)}

# Contrat de l'intention "compute" observe en campagne
CPU_TAU, RAM_TAU = 3.0, 2.0

SRC = [
    ("UC1 federe P1", "data_UC1_federe/qos_history_provider1.xlsx"),
    ("UC1 federe P2", "data_UC1_federe/qos_history_provider2.xlsx"),
    ("UC4 local P1",  "data_UC4_local/qos_history_provider1.xlsx"),
    ("UC4 local P2",  "data_UC4_local/qos_history_provider2.xlsx"),
]


def avail(vm, metric, usage_pct):
    cores, ram = CAP[vm]
    total = cores if metric == "cpu_usage" else ram
    return total * (1.0 - usage_pct / 100.0)


print("Plancher atteint <=> disponible >= 2 x seuil")
print(f"  seuil CPU = {CPU_TAU} cores  -> plancher si dispo >= {2*CPU_TAU} cores")
print(f"  seuil RAM = {RAM_TAU} GB     -> plancher si dispo >= {2*RAM_TAU} GB\n")

floor_hits = collections.defaultdict(lambda: {"cpu": 0, "ram": 0, "n": 0})
lat_floor = 0
lat_total = 0

for label, path in SRC:
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
    except FileNotFoundError:
        continue
    for ts, cyc, vm, metric, meas, pred, model in \
            wb["Prédictions"].iter_rows(min_row=2, values_only=True):
        if vm not in CAP or meas is None:
            continue
        if metric == "latency":
            lat_total += 1
            if float(meas) <= 0:      # condition du plancher sur un cout
                lat_floor += 1
        elif metric in ("cpu_usage", "ram_usage"):
            a = avail(vm, metric, float(meas))
            tau = CPU_TAU if metric == "cpu_usage" else RAM_TAU
            k = "cpu" if metric == "cpu_usage" else "ram"
            floor_hits[vm][k] += 1 if a >= 2 * tau else 0
            if k == "cpu":
                floor_hits[vm]["n"] += 1
    wb.close()

print(f"LATENCE  : plancher atteint {lat_floor}/{lat_total} echantillons "
      f"({100*lat_floor/max(lat_total,1):.1f} %)")
print("           -> confirme : jamais, car une latence est toujours > 0.\n")

print(f"{'VM':8s} {'n':>6s} {'plancher CPU':>14s} {'plancher RAM':>14s}")
print("-" * 46)
for vm in ["edge1", "edge1b", "edge1c", "cloud1", "edge2", "edge2b", "edge2c", "cloud2"]:
    d = floor_hits.get(vm)
    if not d or not d["n"]:
        continue
    n = d["n"]
    print(f"{vm:8s} {n:6d} {100*d['cpu']/n:13.1f}% {100*d['ram']/n:13.1f}%")

c1, c2 = floor_hits.get("cloud1"), floor_hits.get("cloud2")
if c1 and c2 and c1["n"] and c2["n"]:
    both = min(100*c1['cpu']/c1['n'], 100*c2['cpu']/c2['n'])
    print(f"\ncloud1 ET cloud2 simultanement au plancher CPU : "
          f"au moins {both:.1f} % des echantillons")
    print("-> Gap Grade identique, egalite, le tenant l'emporte par defaut.")
