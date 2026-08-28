"""
Intervalle de confiance sur le taux de violation, SANS relancer de run.

Deux estimations complementaires :

  (A) PAR TOUR — le taux recalcule separement sur chaque tour complet.
      Donne une idee de la variabilite intra-run. Faible (2 tours), mais
      c'est une vraie repetition partielle, pas une reconstruction.

  (B) BOOTSTRAP PAR BLOCS — un bootstrap classique serait INVALIDE ici :
      les echantillons successifs sont autocorreles (~0.99 sur la latence),
      donc les tirer independamment sous-estime massivement la variance.
      Le bootstrap par blocs tire des SEGMENTS CONTIGUS, ce qui preserve la
      structure temporelle a l'interieur de chaque bloc.

⚠️ Ni l'un ni l'autre ne remplace des executions independantes : ils bornent
la variabilite A L'INTERIEUR d'un run, pas entre runs. Une repetition
complete resterait la seule facon de borner la variance entre executions.
"""
import csv, datetime, math, random, statistics, openpyxl

VMS = ["edge1", "edge1b", "edge1c", "edge2", "edge2b", "edge2c", "cloud1", "cloud2"]
P1 = {"edge1", "edge1b", "edge1c", "cloud1"}
THRESHOLD = 28.0
TZ = 2
BLOCK_S = 120          # blocs de 2 min : > periode d'autocorrelation utile
N_BOOT = 2000
random.seed(0)


def load_timeline(*paths):
    ev = []
    for path in paths:
        wb = openpyxl.load_workbook(path, read_only=True)
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
        ts_i, vm_i = i["Horodatage (UTC)"], i["VM active (source)"]
        for r in ws.iter_rows(min_row=4, values_only=True):
            if r[0] is None or r[ts_i] is None or r[vm_i] is None:
                continue
            try:
                t = datetime.datetime.fromisoformat(str(r[ts_i])).replace(tzinfo=None)
            except ValueError:
                continue
            ev.append((t, r[vm_i]))
        wb.close()
    ev.sort(key=lambda e: e[0])
    return ev


def host_at(ev, t):
    best = None
    for et, vm in ev:
        if et <= t:
            best = vm
        else:
            break
    return best


def samples(traj_path, timing_paths, pool):
    ev = load_timeline(*timing_paths)
    base = ev[0][0].date()
    out = []
    with open(traj_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))[1:]
    for r in rows:
        if len(r) != 19:
            continue
        h, m, s = map(int, r[0].split(":"))
        utc = datetime.datetime.combine(base, datetime.time(h, m, s)) \
              - datetime.timedelta(hours=TZ)
        vm = host_at(ev, utc)
        if vm is None or vm not in pool:
            continue
        idx = VMS.index(vm)
        if not r[3 + idx]:
            continue
        out.append((utc, float(r[3 + idx]) >= THRESHOLD))
    return out


def per_lap(sm, passages):
    """Taux par tour, delimite par les passages au plus pres (heure locale)."""
    res = []
    for a, b in zip(passages, passages[1:]):
        seg = [v for t, v in sm if a <= t < b]
        if seg:
            res.append(100.0 * sum(seg) / len(seg))
    return res


def block_bootstrap(sm):
    if not sm:
        return (float("nan"),) * 2
    t0 = sm[0][0]
    blocks = {}
    for t, v in sm:
        k = int((t - t0).total_seconds() // BLOCK_S)
        blocks.setdefault(k, []).append(v)
    keys = list(blocks)
    n_blocks = len(keys)
    rates = []
    for _ in range(N_BOOT):
        drawn = []
        for _ in range(n_blocks):
            drawn.extend(blocks[random.choice(keys)])
        rates.append(100.0 * sum(drawn) / len(drawn))
    rates.sort()
    return rates[int(0.025 * N_BOOT)], rates[int(0.975 * N_BOOT)]


def run(label, traj, timings, pool, passages_local):
    sm = samples(traj, timings, pool)
    n = len(sm)
    rate = 100.0 * sum(v for _, v in sm) / n
    lo, hi = block_bootstrap(sm)

    base = sm[0][0].date()
    passages = [
        datetime.datetime.combine(base, datetime.time(*map(int, p.split(":"))))
        for p in passages_local
    ]
    laps = per_lap(sm, passages)

    print(f"\n=== {label}")
    print(f"  taux global        : {rate:.1f} %   (n = {n})")
    print(f"  IC 95 % (blocs {BLOCK_S}s) : [{lo:.1f} % , {hi:.1f} %]")
    if laps:
        txt = " , ".join(f"{x:.1f} %" for x in laps)
        print(f"  par tour           : {txt}")
        if len(laps) > 1:
            print(f"  etendue entre tours: {max(laps)-min(laps):.1f} points")


run("UC1 -- FEDERE",
    "data_UC1_federe/latences_session.csv",
    ["data_UC1_federe/timings_autonomous_provider1.xlsx",
     "data_UC1_federe/timings_autonomous_provider2.xlsx"],
    set(VMS),
    ["12:20:59", "12:36:47", "12:53:00"])

run("UC2 -- ABLATION",
    "data_UC2_ablation/latences_session.csv",
    ["data_UC2_ablation/timings_autonomous_provider1.xlsx"],
    P1,
    ["11:51:02", "12:06:52", "12:23:04"])
