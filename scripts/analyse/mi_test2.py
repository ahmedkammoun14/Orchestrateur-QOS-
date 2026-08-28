"""
Deuxième test : le MI mesuré survit-il à un nul qui PRÉSERVE la structure
temporelle ?

Une permutation aléatoire détruit l'autocorrélation de y. Or latence et CPU
sont deux séries temporelles lisses : chacune dérive lentement. Deux séries
autocorrélées mais indépendantes présentent une dépendance apparente que la
permutation naïve ne peut pas voir — elle est donc anti-conservatrice.

Le nul correct est le DÉCALAGE CIRCULAIRE : on décale y dans le temps, ce qui
conserve exactement son autocorrélation tout en détruisant l'appariement avec
X. Si le MI observé tombe dans cette distribution, la dépendance apparente
n'est que de la dérive partagée.
"""
import sys, collections
import numpy as np
import openpyxl
from sklearn.feature_selection import mutual_info_classif

PATH = sys.argv[1]
SEUIL = float(sys.argv[2]) if len(sys.argv) > 2 else 28.0

wb = openpyxl.load_workbook(PATH, read_only=True)
rows = [r for r in wb["Métriques"].iter_rows(min_row=2, values_only=True) if r[0]]
wb.close()

by_vm = collections.defaultdict(list)
for ts, vm, lat, cpu, ram, rel in rows:
    if None not in (lat, cpu, ram):
        by_vm[vm].append((float(lat), float(cpu), float(ram)))


def mi(X, y):
    return mutual_info_classif(X, y, discrete_features=False,
                               n_neighbors=3, random_state=0)


def autocorr1(v):
    v = v - v.mean()
    d = (v * v).sum()
    return float((v[:-1] * v[1:]).sum() / d) if d else 0.0


print(f"seuil = {SEUIL} ms\n")
print(f"{'VM':9s} {'ac(lat)':>8s} {'ac(cpu)':>8s} {'ac(ram)':>8s} | "
      f"{'MI(cpu)':>8s} {'nul p95':>8s} {'p':>6s} | "
      f"{'MI(ram)':>8s} {'nul p95':>8s} {'p':>6s}")
print("-" * 92)

for vm, series in sorted(by_vm.items()):
    a = np.array(series)
    lat, X = a[:, 0], a[:, 1:3]
    y = (lat > SEUIL).astype(int)
    n = len(y)
    if len(set(y)) < 2 or n < 60:
        print(f"{vm:9s}  — une seule classe ou série trop courte")
        continue

    obs = mi(X, y)
    shifts = range(10, n - 10)
    null = np.array([mi(X, np.roll(y, s)) for s in shifts])
    p95 = np.percentile(null, 95, axis=0)
    pval = (null >= obs).mean(axis=0)

    print(f"{vm:9s} {autocorr1(lat):8.3f} {autocorr1(a[:,1]):8.3f} "
          f"{autocorr1(a[:,2]):8.3f} | "
          f"{obs[0]:8.4f} {p95[0]:8.4f} {pval[0]:6.3f} | "
          f"{obs[1]:8.4f} {p95[1]:8.4f} {pval[1]:6.3f}")

print("\np = fraction des décalages circulaires dont le MI dépasse l'observé.")
print("p > 0.05  →  indistinguable de la dérive temporelle partagée.")
