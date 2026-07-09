"""
scripts/export_timings_to_pdf.py — Export des fichiers de mesures en PDF imprimable.

Convertit timings_autonomous.xlsx, timings_enhanced.xlsx et
timings_comparison.xlsx en PDF : paysage, une seule page par fichier,
en-têtes de colonnes conservés + UNIQUEMENT les 10 dernières lignes de
données (les lignes intermédiaires sont masquées avant l'export, pas
supprimées du fichier source). Tout est mis à l'échelle pour tenir sur
une page entière (lisible, pas de coupure).

Automatisation COM d'Excel (fidèle aux couleurs/fusions de cellules
d'origine — nécessite Microsoft Excel installé).

Usage :
    ./venv/Scripts/python.exe scripts/export_timings_to_pdf.py
"""
import sys
from pathlib import Path

import win32com.client as win32

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import config  # noqa: E402
from shared.timing_writer import (  # noqa: E402
    _AUTONOMOUS_COLS,
    _ENHANCED_COLS,
    _S_DB,
    _S_HL,
)

_ROOT = Path(__file__).resolve().parent.parent

_XL_LANDSCAPE = 2
_N_LAST_ROWS  = 10

# Groupes de colonnes retirés du PDF imprimable (moins de colonnes → moins
# de réduction d'échelle nécessaire → texte plus grand et plus lisible).
_HIDE_SERVICES = {_S_DB, _S_HL}

# (chemin xlsx, dernière ligne d'en-tête, définition des colonnes — None si
# le retrait de colonnes par microservice ne s'applique pas à ce fichier)
_FILES = [
    (Path(config.TIMING_EXCEL_AUTONOMOUS_PATH), 3, _AUTONOMOUS_COLS),
    (Path(config.TIMING_EXCEL_ENHANCED_PATH),   3, _ENHANCED_COLS),
    (_ROOT / "data" / "timings_comparison.xlsx", 4, None),
]


def _hide_columns(ws, cols_def) -> None:
    """Masque les colonnes des microservices Database et History Loader —
    demandé pour libérer de la place et agrandir le texte restant à l'impression."""
    if not cols_def:
        return
    for idx, (_key, svc, _sg, _label, _kind) in enumerate(cols_def, start=1):
        if svc in _HIDE_SERVICES:
            ws.Columns(idx).EntireColumn.Hidden = True


def _hide_all_but_last_rows(ws, header_end: int, n_last: int) -> None:
    """Masque les lignes de données intermédiaires — ne garde visibles que
    l'en-tête et les n_last dernières lignes (le fichier source n'est pas modifié,
    on ne sauvegarde jamais ces changements)."""
    last_row  = ws.UsedRange.Rows.Count
    data_start = header_end + 1
    keep_from  = max(data_start, last_row - n_last + 1)
    if keep_from > data_start:
        ws.Rows(f"{data_start}:{keep_from - 1}").EntireRow.Hidden = True


def _configure_sheet(ws) -> None:
    ps = ws.PageSetup
    # Paysage : avec 37-40 colonnes, une page portrait (21cm de large) oblige
    # Excel à réduire le texte bien plus qu'une page paysage (29,7cm) pour
    # tout faire tenir en largeur — même facteur de réduction appliqué aux
    # lignes, d'où le texte écrasé constaté en portrait.
    ps.Orientation        = _XL_LANDSCAPE
    ps.Zoom               = False   # doit être False pour que FitToPages s'applique
    ps.FitToPagesWide     = 1
    ps.FitToPagesTall     = 1       # tout tient sur UNE seule page (largeur ET hauteur)
    ps.LeftMargin         = ws.Application.CentimetersToPoints(0.5)
    ps.RightMargin        = ws.Application.CentimetersToPoints(0.5)
    ps.TopMargin          = ws.Application.CentimetersToPoints(0.8)
    ps.BottomMargin       = ws.Application.CentimetersToPoints(0.8)
    ps.CenterHorizontally = True


def main() -> None:
    missing = [str(p) for p, _, _ in _FILES if not p.exists()]
    if missing:
        print(f"⚠️  Fichier(s) introuvable(s), ignoré(s) : {missing}")

    excel = win32.gencache.EnsureDispatch("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False

    try:
        for path, header_end, cols_def in _FILES:
            if not path.exists():
                continue
            print(f"📂 Ouverture : {path}")
            # ReadOnly=False : nécessaire pour que la suppression de feuille et
            # l'export fonctionnent sans erreur COM "Document non enregistré".
            # Sans danger — Close(SaveChanges=False) ci-dessous ne réécrit
            # jamais le fichier source.
            wb = excel.Workbooks.Open(str(path.resolve()), ReadOnly=False)
            try:
                ws = wb.Worksheets(1)  # feuille de données (créée avant la Légende)
                _hide_all_but_last_rows(ws, header_end, _N_LAST_ROWS)
                _hide_columns(ws, cols_def)
                _configure_sheet(ws)

                # Supprime les autres feuilles (ex: Légende) de cette copie en
                # mémoire — jamais sauvegardé sur le fichier source (Close
                # SaveChanges=False) — pour que l'export ne contienne QUE les
                # données. Worksheet.ExportAsFixedFormat lève une erreur COM
                # ("Document non enregistré") sur un classeur ouvert en lecture
                # seule ; exporter au niveau du classeur (une seule feuille
                # restante) contourne le problème.
                for other in list(wb.Worksheets):
                    if other.Name != ws.Name:
                        other.Delete()

                out_path = path.with_suffix(".pdf")
                # Contourne l'erreur COM "Document non enregistré" que déclenche
                # ExportAsFixedFormat après des modifications en mémoire (lignes
                # masquées, feuille supprimée) — ne sauvegarde rien sur disque,
                # indique juste à Excel qu'il n'y a "rien à enregistrer".
                wb.Saved = True
                wb.ExportAsFixedFormat(0, str(out_path.resolve()))
                print(f"✅ PDF généré (dernières {_N_LAST_ROWS} lignes) : {out_path}")
            finally:
                wb.Close(SaveChanges=False)
    finally:
        excel.Quit()


if __name__ == "__main__":
    main()
