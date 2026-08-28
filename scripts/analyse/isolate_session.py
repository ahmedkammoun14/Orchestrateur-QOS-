"""
Isole, dans latences.csv (append-only, pas de colonne de date, plusieurs
jours melanges), la SESSION CONTIGUE qui se termine a la derniere ligne du
fichier -- c'est forcement le run le plus recent, donc UC1 ou UC2 selon le
fichier. Marche en PARTANT DE LA FIN, s'arrete des qu'un ecart > 5 min
apparait entre deux lignes consecutives (meme methode que la detection de
tours utilisee plus tot dans la session).
"""
import csv, datetime, sys

path = sys.argv[1]
GAP_S = 300

with open(path, newline="", encoding="utf-8") as f:
    rows = list(csv.reader(f))

data = [r for r in rows[1:] if len(r) == 19]
print(f"{path}: {len(data)} lignes a 19 colonnes sur {len(rows)-1} lignes totales")

def to_dt(t):
    h, m, s = map(int, t.split(":"))
    return datetime.timedelta(hours=h, minutes=m, seconds=s)

session = [data[-1]]
prev_t = to_dt(data[-1][0])
for row in reversed(data[:-1]):
    t = to_dt(row[0])
    delta = (prev_t - t).total_seconds()
    if delta < 0:
        delta += 24 * 3600  # passage minuit
    if delta > GAP_S:
        break
    session.append(row)
    prev_t = t
session.reverse()

print(f"session isolee : {len(session)} lignes | {session[0][0]} -> {session[-1][0]}")

out = path.replace(".csv", "_session.csv")
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(rows[0])
    w.writerows(session)
print("ecrit ->", out)
