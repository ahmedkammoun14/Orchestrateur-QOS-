"""
DEUX analyses, sur des donnees deja validees :

  (A) UC3 DERIVE : violation d'un placement STATIQUE (poser le service une
      fois, ne jamais migrer) -- ce que fait kube-scheduler. Calcule pour
      CHAQUE VM possible, sur la MEME trajectoire que la condition comparee.

  (B) DIAGNOSTIC de l'ecart a l'oracle en federe : quand notre systeme viole
      alors qu'une meilleure VM existait, de combien est-il en retard ?

Verification integree : le total d'instants et le taux systeme doivent
retomber EXACTEMENT sur les valeurs deja calculees par violation_rate.py
(UC1 315/1587 = 19.8%, UC2 654/1217 = 53.7%). Si ce n'est pas le cas, le
script s'arrete : cela signifie que l'alignement temporel a change.
"""
import csv, datetime, openpyxl, sys

VMS = ["edge1", "edge1b", "edge1c", "edge2", "edge2b", "edge2c", "cloud1", "cloud2"]
P1 = ["edge1", "edge1b", "edge1c", "cloud1"]
THRESHOLD = 28.0
TZ_OFFSET_H = 2


def load_timeline(*paths):
    events = []
    for path in paths:
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(min_row=1, max_row=3, values_only=True))
        header, cur = [], [None, None, None]
        for c in range(ws.max_column):
            for r in range(3):
                v = rows[r][c] if c < len(rows[r]) else None
                if v is not None:
                    cur[r] = v
                    for k in range(r + 1, 3):
                        cur[k] = None
            header.append(" > ".join(str(p) for p in cur if p))
        idx = {n: i for i, n in enumerate(header)}
        ts_i, vm_i = idx["Horodatage (UTC)"], idx["VM active (source)"]
        for r in ws.iter_rows(min_row=4, values_only=True):
            if r[0] is None or r[ts_i] is None or r[vm_i] is None:
                continue
            try:
                t = datetime.datetime.fromisoformat(str(r[ts_i])).replace(tzinfo=None)
            except ValueError:
                continue
            events.append((t, r[vm_i]))
        wb.close()
    events.sort(key=lambda e: e[0])
    return events


def host_at(timeline, t):
    best = None
    for et, vm in timeline:
        if et <= t:
            best = vm
        else:
            break
    return best


def load_traj(path, debut, fin):
    """
    Trajectoire restreinte a la fenetre couverte par l'orchestrateur.

    Meme regle que violation_rate.py depuis le 17/08/2026 : latences.csv ne
    porte qu'une heure sans date, on essaie les trois decalages de jour et on
    ne garde que celui qui tombe DANS [debut, fin]. Sans cette borne, les
    echantillons anterieurs au demarrage (21 min pour le run ABL du 17/08) ou
    posterieurs a l'arret seraient attribues a une VM que le systeme n'avait
    plus la main pour choisir.
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))[1:]
    out = []
    for r in rows:
        if len(r) != 19:
            continue
        h, m, s = map(int, r[0].split(":"))
        utc = None
        for delta_j in (0, 1, -1):
            cand = (datetime.datetime.combine(debut.date(), datetime.time(h, m, s))
                    - datetime.timedelta(hours=TZ_OFFSET_H)
                    + datetime.timedelta(days=delta_j))
            if debut <= cand <= fin:
                utc = cand
                break
        if utc is None:
            continue
        vals = {vm: float(r[3 + i]) for i, vm in enumerate(VMS) if r[3 + i]}
        if vals:
            out.append((utc, vals))
    return out


def run(label, traj_path, timing_paths, pool, expect_viol, expect_total):
    timeline = load_timeline(*timing_paths)
    traj = load_traj(traj_path, timeline[0][0], timeline[-1][0])

    # --- controle : reproduire le taux systeme deja valide ---
    sys_viol = sys_total = 0
    samples = []
    for utc, vals in traj:
        vm = host_at(timeline, utc)
        if vm is None or vm not in pool or vm not in vals:
            continue
        sys_total += 1
        if vals[vm] >= THRESHOLD:
            sys_viol += 1
        samples.append((utc, vals, vm))

    print(f"\n{'='*66}\n=== {label}\n{'='*66}")
    print(f"  CONTROLE : systeme {sys_viol}/{sys_total} = {100*sys_viol/sys_total:.1f} %"
          f"  (attendu {expect_viol}/{expect_total} = {100*expect_viol/expect_total:.1f} %)")
    if (sys_viol, sys_total) != (expect_viol, expect_total):
        print("  !! DIVERGENCE avec le calcul de reference -- ANALYSE ABANDONNEE")
        return
    print("  controle OK, les deux calculs concordent exactement.\n")

    # --- (A) UC3 derive : placement statique ---
    print("  (A) PLACEMENT STATIQUE (kube-scheduler : pose une fois, ne migre jamais)")
    static = {}
    for vm in pool:
        v = sum(1 for _, vals, _ in samples if vm in vals and vals[vm] >= THRESHOLD)
        n = sum(1 for _, vals, _ in samples if vm in vals)
        static[vm] = 100 * v / n if n else float("nan")
    for vm in sorted(static, key=static.get):
        print(f"      {vm:8s} : {static[vm]:5.1f} %")
    best_vm = min(static, key=static.get)
    worst_vm = max(static, key=static.get)
    mean_static = sum(static.values()) / len(static)
    print(f"      -> meilleur cas {best_vm} = {static[best_vm]:.1f} % | "
          f"pire cas {worst_vm} = {static[worst_vm]:.1f} % | "
          f"moyenne = {mean_static:.1f} %")

    # --- (B) diagnostic du retard ---
    print("\n  (B) DIAGNOSTIC : quand le systeme viole, une meilleure VM existait-elle ?")
    viol_avoidable = 0   # systeme viole ET une VM conforme existait
    viol_inevitable = 0  # systeme viole ET aucune VM conforme
    for _, vals, vm in samples:
        if vals[vm] < THRESHOLD:
            continue
        avail = {k: v for k, v in vals.items() if k in pool}
        if min(avail.values()) < THRESHOLD:
            viol_avoidable += 1
        else:
            viol_inevitable += 1
    print(f"      violations EVITABLES  (une VM conforme existait) : {viol_avoidable}"
          f"  = {100*viol_avoidable/sys_total:.1f} % du temps total")
    print(f"      violations INEVITABLES (aucune VM conforme)      : {viol_inevitable}"
          f"  = {100*viol_inevitable/sys_total:.1f} % du temps total")

    # retard : la VM choisie etait-elle la meilleure il y a N secondes ?
    print("\n      Le systeme suit-il l'optimum avec du retard ?")
    for lag_s in (0, 6, 12, 18, 30, 60):
        match = 0
        checked = 0
        for i, (utc, vals, vm) in enumerate(samples):
            past = utc - datetime.timedelta(seconds=lag_s)
            ref = None
            for u2, v2 in traj:
                if u2 <= past:
                    ref = v2
                else:
                    break
            if not ref:
                continue
            avail = {k: v for k, v in ref.items() if k in pool}
            if not avail:
                continue
            best_then = min(avail, key=avail.get)
            checked += 1
            if vm == best_then:
                match += 1
        if checked:
            print(f"        VM choisie = meilleure VM d'il y a {lag_s:2d} s : "
                  f"{100*match/checked:5.1f} %")


run("UC1  (13 aout) -- FEDERE (8 VMs)",
    "data_UC1_federe/latences_session.csv",
    ["data_UC1_federe/timings_autonomous_provider1.xlsx",
     "data_UC1_federe/timings_autonomous_provider2.xlsx"],
    VMS, 315, 1586)

run("FED1 (17 aout) -- FEDERE (8 VMs)",
    "data_FED_run1/latences_session.csv",
    ["data_FED_run1/timings_autonomous_provider1.xlsx",
     "data_FED_run1/timings_autonomous_provider2.xlsx"],
    VMS, 422, 1627)

run("FED2 (18 aout) -- FEDERE (8 VMs)",
    "data_FED_run2/latences_session.csv",
    ["data_FED_run2/timings_autonomous_provider1.xlsx",
     "data_FED_run2/timings_autonomous_provider2.xlsx"],
    VMS, 502, 1712)

run("FED3 (18 aout) -- FEDERE (8 VMs)",
    "data_FED_run3/latences_session.csv",
    ["data_FED_run3/timings_autonomous_provider1.xlsx",
     "data_FED_run3/timings_autonomous_provider2.xlsx"],
    VMS, 656, 3252)

run("RUN9 (21 aout, horizon corrige) -- FEDERE (8 VMs)",
    "data_RUN9_horizon/latences_session.csv",
    ["data_RUN9_horizon/timings_autonomous_provider1.xlsx",
     "data_RUN9_horizon/timings_autonomous_provider2.xlsx"],
    VMS, 395, 1488)

run("UC2  (14 aout) -- ABLATION (4 VMs)",
    "data_UC2_ablation/latences_session.csv",
    ["data_UC2_ablation/timings_autonomous_provider1.xlsx"],
    P1, 652, 1215)

run("ABL1 (17 aout) -- ABLATION (4 VMs)",
    "data_ABL_run1/latences_session.csv",
    ["data_ABL_run1/timings_autonomous_provider1.xlsx"],
    P1, 701, 1331)

run("ABL2 (18 aout) -- ABLATION (4 VMs)",
    "data_ABL_run2/latences_session.csv",
    ["data_ABL_run2/timings_autonomous_provider1.xlsx"],
    P1, 973, 1720)

run("ABL3 (18 aout) -- ABLATION (4 VMs)",
    "data_ABL_run3/latences_session.csv",
    ["data_ABL_run3/timings_autonomous_provider1.xlsx"],
    P1, 1067, 1894)
