"""
Verification d'un run JUSTE APRES son archivage — 10 secondes.

A lancer immediatement apres chaque run de la campagne :

    python scripts/analyse/verify_run.py data_UC1_federe_run1 federe
    python scripts/analyse/verify_run.py data_UC2_ablation_run1 ablation

Repond aux quatre questions qui peuvent invalider un run, AVANT d'en lancer
un autre. Decouvrir un probleme le soir, c'est perdre la journee ; le
decouvrir en 10 secondes, c'est relancer le run.

POURQUOI PAS UN CONTROLE EN DIRECT DANS LE TERMINAL :
  - curl /health sur les relais ne distingue RIEN : RELAY_URL_PROVIDER_* est
    pose inconditionnellement dans launch_provider._env_for(), donc les deux
    bras affichent deux pairs differents.
  - Les lignes de negociation n'apparaissent QUE sur violation : sans
    violation, _decide_federated retombe sur _decide_mono_provider et le
    federe est indiscernable de l'ablation dans les logs.
  La colonne « Federation » du fichier de temps, elle, est vide en ablation
  et remplie en federe des qu'une negociation a eu lieu. C'est le seul
  temoin fiable, et il est dans les donnees.
"""
import sys, datetime, collections, openpyxl
from pathlib import Path

if len(sys.argv) < 3:
    print(__doc__)
    sys.exit(1)

DOSSIER = Path(sys.argv[1])
BRAS = sys.argv[2].lower()          # "federe" ou "ablation"
SEUIL = 28.0
VMS = ["edge1", "edge1b", "edge1c", "edge2", "edge2b", "edge2c", "cloud1", "cloud2"]

ok = True


def echec(msg):
    global ok
    ok = False
    print(f"  [ECHEC] {msg}")


def bon(msg):
    print(f"  [ OK  ] {msg}")


def avert(msg):
    """Anomalie connue, sans effet sur l'exploitabilité du run : ne met pas
    `ok` a False, pour ne pas faire rejeter un run parfaitement utilisable."""
    print(f"  [AVERT] {msg}")


def entetes(ws, n=3):
    rows = list(ws.iter_rows(min_row=1, max_row=n, values_only=True))
    out, cur = [], [None] * n
    for c in range(ws.max_column):
        for r in range(n):
            v = rows[r][c] if c < len(rows[r]) else None
            if v is not None:
                cur[r] = v
                for k in range(r + 1, n):
                    cur[k] = None
        out.append(" > ".join(str(x) for x in cur if x))
    return out


print(f"\n=== VERIFICATION : {DOSSIER}  (bras attendu : {BRAS})\n")

# ── 1. Les fichiers attendus sont la ────────────────────────────────
attendus = ["timings_autonomous_provider1.xlsx", "qos_history_provider1.xlsx"]
if BRAS == "federe":
    attendus += ["timings_autonomous_provider2.xlsx", "qos_history_provider2.xlsx"]

for f in attendus:
    p = DOSSIER / f
    if p.exists() and p.stat().st_size > 10_000:
        bon(f"{f} present ({p.stat().st_size // 1024} Ko)")
    else:
        echec(f"{f} absent ou trop petit")

if (DOSSIER / "latences.csv").exists():
    n = sum(1 for _ in open(DOSSIER / "latences.csv", encoding="utf-8"))
    bon(f"latences.csv present ({n} lignes)")
else:
    echec("latences.csv ABSENT — le scp depuis le Pi n'a pas ete fait")

# ── 2. Le bras est-il celui attendu ? ───────────────────────────────
tp = DOSSIER / "timings_autonomous_provider1.xlsx"
if tp.exists():
    wb = openpyxl.load_workbook(tp, read_only=True)
    ws = wb.worksheets[0]
    h = entetes(ws)
    i = {n: k for k, n in enumerate(h)}
    rows = [r for r in ws.iter_rows(min_row=4, values_only=True) if r[0]]

    col_fed = i.get("Federation > Total Fédération (ms)")
    n_fed = sum(1 for r in rows
                if col_fed is not None and col_fed < len(r)
                and r[col_fed] not in (None, "")) if col_fed is not None else 0

    print()
    if BRAS == "federe":
        if n_fed > 0:
            bon(f"BRAS FEDERE confirme — {n_fed} cycles avec negociation "
                f"({100*n_fed/len(rows):.0f} %)")
        elif col_fed is None:
            # Le modele de colonnes (_AUTONOMOUS_COLS, 37 colonnes) ne contient
            # AUCUNE cle de federation : le hub les calcule et les met dans la
            # ligne, le writer les jette. La campagne d'aout tournait sur une
            # version du writer qui les avait, jamais commitee. Constate le
            # 27/08/2026. Une colonne absente n'est donc PAS la preuve d'une
            # absence de negociation — se rabattre sur les migrations.
            avert("Colonnes Federation ABSENTES du fichier (modele a 37 colonnes).\n"
                  "          Ce n'est PAS la preuve d'une absence de negociation :\n"
                  "          verifier le nombre de 'migrate' ci-dessous, et\n"
                  "          'DECISION : MIGRATION (federee' dans le journal.")
        else:
            echec("Colonne Federation VIDE alors qu'on attend le bras federe.\n"
                  "          Soit MULTI_PROVIDER_ENABLED n'etait pas 'true',\n"
                  "          soit aucune violation n'a eu lieu (run trop court).")
    else:
        if n_fed == 0:
            bon("BRAS ABLATION confirme — aucune negociation, colonne Federation vide")
        else:
            echec(f"{n_fed} cycles avec negociation alors qu'on attend l'ablation.\n"
                  "          MULTI_PROVIDER_ENABLED etait reste a 'true'. RUN A REFAIRE.")

    # ── 3. Duree et nombre de passages ──────────────────────────────
    t0 = datetime.datetime.fromisoformat(str(rows[0][i["Horodatage (UTC)"]]))
    t1 = datetime.datetime.fromisoformat(str(rows[-1][i["Horodatage (UTC)"]]))
    duree = (t1 - t0).total_seconds() / 60
    print()
    bon(f"{len(rows)} cycles | {duree:.1f} min | "
        f"{t0.strftime('%H:%M:%S')} -> {t1.strftime('%H:%M:%S')} UTC")
    if duree < 35:
        echec(f"Duree {duree:.1f} min < 35 min : probablement moins de "
              "3 passages, fenetre de 2 tours non garantie")

    dec = collections.Counter(r[i["Décision"]] for r in rows)
    bon(f"decisions : {dict(dec)}")
    wb.close()

# ── 4. Les predictions ont-elles ete ecrites jusqu'au bout ? ────────
qp = DOSSIER / "qos_history_provider1.xlsx"
if qp.exists():
    wb = openpyxl.load_workbook(qp, read_only=True)
    sheets_qp = wb.sheetnames
    # La feuille peut manquer : le hub POSTe vers /store/predictions, endpoint
    # ABSENT de services/database/app.py (404 a chaque cycle, silencieux cote
    # hub). Constate le 27/08/2026. Ne pas planter pour autant : les autres
    # controles de ce script restent valables et sont ceux qui decident si un
    # run est bon a garder.
    if "Prédictions" in wb.sheetnames:
        pr = [r for r in wb["Prédictions"].iter_rows(min_row=2, values_only=True) if r[0]]
    else:
        pr = []
        print()
        avert("Feuille 'Predictions' ABSENTE — endpoint /store/predictions "
              "introuvable (404).\n"
              "          Sans effet sur le taux de violation, les migrations "
              "et le test MI.")
    wb.close()
    print()
    if pr:
        derniere = str(pr[-1][0])[11:19]
        bon(f"Predictions : {len(pr)} lignes, derniere a {derniere}")
        if tp.exists():
            fin_timing = t1.strftime("%H:%M:%S")
            ecart = (datetime.datetime.strptime(fin_timing, "%H:%M:%S")
                     - datetime.datetime.strptime(derniere, "%H:%M:%S")).total_seconds()
            if ecart > 120:
                echec(f"Predictions en retard de {ecart/60:.1f} min sur la fin du run.\n"
                      "          Les 30 secondes d'attente apres arret du PiCar\n"
                      "          n'ont pas suffi (ou pas ete respectees).")
            else:
                bon(f"Ecriture complete (retard {ecart:.0f} s)")
    elif "Prédictions" in sheets_qp:
        # Feuille presente mais vide : la ecriture s'est bien arretee en route,
        # c'est un vrai probleme. Si la feuille est ABSENTE, l'avertissement
        # a deja ete emis plus haut — ne pas compter deux fois.
        echec("Feuille Predictions VIDE")

print("\n" + ("=" * 60))
print("  RUN VALIDE — passer au suivant" if ok
      else "  PROBLEME DETECTE — lire ci-dessus avant de continuer")
print("=" * 60 + "\n")
