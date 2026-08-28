"""
predictions[0] est-il le pas le PLUS PROCHE (t+1) ou le PLUS LOINTAIN (t+7) ?

Le code suppose implicitement l'ordre proche->lointain (les poids
[7,6,...,1] de calculate_weighted_mean donnent le maximum a l'indice 0),
mais rien ne le VERIFIE. Si l'API renvoyait l'ordre inverse, la ponderation
privilegierait le futur lointain et toute l'analyse du retard s'inverserait.

Test empirique : la feuille Predictions stocke predictions[0]. On compare
cette valeur a la mesure du cycle N+1, N+2, ... N+7 de la MEME VM. Le
decalage qui MINIMISE l'erreur indique ce que predictions[0] represente
reellement.

  minimum a N+1  ->  predictions[0] = t+1  (ordre proche->lointain, suppose)
  minimum a N+7  ->  predictions[0] = t+7  (ordre inverse)
"""
import openpyxl, collections, math

SRC = [
    "data_UC1_federe/qos_history_provider1.xlsx",
    "data_UC1_federe/qos_history_provider2.xlsx",
    "data_UC2_ablation/qos_history_provider1.xlsx",
]
MAX_LAG = 8

meas = collections.defaultdict(dict)          # (metric, vm) -> {cycle: measured}
pred = collections.defaultdict(list)          # (metric, vm) -> [(cycle, pred)]

for path in SRC:
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
    except FileNotFoundError:
        continue
    for ts, cyc, vm, metric, m, p, model in \
            wb["Prédictions"].iter_rows(min_row=2, values_only=True):
        if cyc is None or vm is None:
            continue
        if m is not None:
            meas[(metric, vm)][cyc] = float(m)
        # Niveau 1 uniquement : les replis renvoient la derniere valeur
        # repetee, ce qui biaiserait le test vers le decalage 0.
        if p is not None and model not in ("last_value_fallback", "point_model"):
            pred[(metric, vm)].append((cyc, float(p)))
    wb.close()

print("MAE de predictions[0] compare a la mesure du cycle N+k\n")
header = "metrique     " + "".join(f"{'N+'+str(k):>9s}" for k in range(1, MAX_LAG))
print(header)
print("-" * len(header))

for metric in ("latency", "cpu_usage", "ram_usage"):
    row = {k: [] for k in range(1, MAX_LAG)}
    for (met, vm), plist in pred.items():
        if met != metric:
            continue
        mv = meas[(met, vm)]
        cycles = sorted(mv)
        for cyc, pv in plist:
            later = [c for c in cycles if c > cyc][:MAX_LAG - 1]
            for k, c in enumerate(later, start=1):
                row[k].append(abs(pv - mv[c]))
    cells = ""
    best_k, best_v = None, float("inf")
    for k in range(1, MAX_LAG):
        if row[k]:
            v = sum(row[k]) / len(row[k])
            cells += f"{v:9.3f}"
            if v < best_v:
                best_v, best_k = v, k
        else:
            cells += f"{'--':>9s}"
    print(f"{metric:12s}{cells}   -> min a N+{best_k}")

print("\nLecture : si le minimum tombe a N+1, predictions[0] est bien le pas")
print("le plus proche et la ponderation [7,6,...,1] privilegie le present.")
