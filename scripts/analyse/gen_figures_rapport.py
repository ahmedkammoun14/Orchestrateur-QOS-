"""
Genere les figures PNG du chapitre 5 du rapport, dans rapport/images/.

Format PNG (pas TikZ) parce que main.tex charge deja graphicx mais ni tikz
ni pgfplots -- ajouter un package a main.tex demanderait l'accord du binome.

TOUTES les valeurs viennent des tableaux DEJA VALIDES du chapitre 5. Aucune
n'est recalculee ici : ce script ne fait que tracer, jamais mesurer. Si un
chiffre du rapport change, il faut le changer ICI AUSSI, a la main. Ce choix
est volontaire -- recalculer depuis les donnees brutes ferait de ce script
une seconde source de verite, qui pourrait diverger du texte en silence.

Sortie :
  fig_concordance.png  -- Table 5.9  : concordance vs Delta, 8 executions
  fig_violation.png    -- Table 5.13 : taux de violation par execution
  fig_bornes.png       -- Sec. 5.5.2 : oracle / systeme / meilleur statique
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "rapport" / "images"
OUT.mkdir(parents=True, exist_ok=True)

# Palette sobre, lisible en niveaux de gris a l'impression.
C_FED = "#1f4e79"   # bleu fonce  -- federe
C_ABL = "#c55a11"   # orange      -- ablation
C_ORA = "#7f7f7f"   # gris        -- oracle
C_STA = "#bfbfbf"   # gris clair  -- statique

plt.rcParams.update({
    "font.size": 10,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "grid.linestyle": "--",
    "figure.dpi": 200,
})


# ── Figure 1 — concordance vs Delta (Table 5.9) ──────────────────────
def fig_concordance():
    deltas = [0, 6, 12, 18, 30, 60]
    federe = {
        "UC1":  [83.8, 86.7, 88.6, 87.2, 80.8, 62.4],
        "FED1": [76.8, 79.8, 81.2, 80.2, 74.3, 57.7],
        "FED2": [73.4, 75.9, 77.7, 76.3, 70.1, 55.2],
        "FED3": [83.2, 86.3, 88.0, 87.4, 81.3, 64.0],
    }
    ablation = {
        "UC2":  [96.9, 95.5, 94.0, 92.5, 89.3, 80.1],
        "ABL1": [94.2, 93.8, 93.3, 92.2, 89.5, 80.5],
        "ABL2": [90.6, 90.0, 88.4, 86.6, 83.3, 74.7],
        "ABL3": [94.1, 92.9, 91.3, 89.5, 86.1, 78.4],
    }

    fig, ax = plt.subplots(figsize=(6.5, 3.8))

    for i, (lab, vals) in enumerate(federe.items()):
        ax.plot(deltas, vals, "-o", color=C_FED, markersize=4, linewidth=1.4,
                alpha=0.85, label="Fédéré" if i == 0 else None)
        imax = int(np.argmax(vals))
        ax.plot(deltas[imax], vals[imax], "o", color=C_FED, markersize=9,
                markerfacecolor="white", markeredgewidth=1.8, zorder=5)

    for i, (lab, vals) in enumerate(ablation.items()):
        ax.plot(deltas, vals, "--s", color=C_ABL, markersize=4, linewidth=1.4,
                alpha=0.85, label="Ablation" if i == 0 else None)
        imax = int(np.argmax(vals))
        ax.plot(deltas[imax], vals[imax], "s", color=C_ABL, markersize=9,
                markerfacecolor="white", markeredgewidth=1.8, zorder=5)

    ax.axvline(12, color=C_FED, linewidth=0.9, linestyle=":", alpha=0.6)
    ax.text(13.0, 68, "pic fédéré\nΔ = 12 s", fontsize=8, color=C_FED, va="center")
    ax.axvline(0, color=C_ABL, linewidth=0.9, linestyle=":", alpha=0.6)
    ax.annotate("pic ablation\nΔ = 0 s", xy=(0, 96.9), xytext=(4.5, 101),
                fontsize=8, color=C_ABL, va="center", ha="left",
                arrowprops=dict(arrowstyle="-", color=C_ABL, lw=0.8, alpha=0.6))

    ax.set_xlabel("Décalage temporel Δ (secondes)")
    ax.set_ylabel("Concordance avec l'optimum (%)")
    ax.set_xticks(deltas)
    ax.set_ylim(50, 106)
    ax.legend(loc="lower left", frameon=True, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(OUT / "fig_concordance.png", bbox_inches="tight")
    plt.close(fig)
    print("  fig_concordance.png")


# ── Figure 2 — taux de violation par execution (Table 5.13) ──────────
def fig_violation():
    federe   = {"UC1": 19.9, "FED1": 25.9, "FED2": 29.3, "FED3": 20.2}
    ablation = {"UC2": 53.7, "ABL1": 52.7, "ABL2": 56.6, "ABL3": 56.3}

    fig, ax = plt.subplots(figsize=(6.5, 3.6))

    # Ecartement large : ABL2 (56,6) et ABL3 (56,3) sont a 0,3 pt l'un de
    # l'autre, leurs etiquettes se chevaucheraient a un ecartement plus serre.
    xf = np.full(len(federe), 1.0) + np.linspace(-0.22, 0.22, len(federe))
    xa = np.full(len(ablation), 2.0) + np.linspace(-0.22, 0.22, len(ablation))

    ax.scatter(xf, list(federe.values()), s=70, color=C_FED, zorder=3,
               edgecolors="white", linewidth=1.2)
    ax.scatter(xa, list(ablation.values()), s=70, color=C_ABL, zorder=3,
               edgecolors="white", linewidth=1.2)

    mf, ma = np.mean(list(federe.values())), np.mean(list(ablation.values()))

    # Etiquette placee VERS L'EXTERIEUR (au-dessus si le point est au-dessus de
    # la moyenne, en dessous sinon) : une alternance fixe ferait tomber certaines
    # etiquettes pile sur la ligne de moyenne, qu'elles masqueraient.
    for x, (lab, v), moy in [(x, kv, mf) for x, kv in zip(xf, federe.items())] + \
                             [(x, kv, ma) for x, kv in zip(xa, ablation.items())]:
        col = C_FED if moy == mf else C_ABL
        dy = 11 if v >= moy else -17
        ax.annotate(lab, (x, v), textcoords="offset points", xytext=(0, dy),
                    ha="center", fontsize=7.5, color=col)

    ax.hlines(mf, 0.70, 1.30, color=C_FED, linewidth=2.2, zorder=2)
    ax.hlines(ma, 1.70, 2.30, color=C_ABL, linewidth=2.2, zorder=2)
    ax.text(1.34, mf, f"{mf:.1f} %", va="center", fontsize=9,
            color=C_FED, fontweight="bold")
    ax.text(2.34, ma, f"{ma:.1f} %", va="center", fontsize=9,
            color=C_ABL, fontweight="bold")

    # Bande separant strictement les deux groupes : pire federe < meilleur ablation.
    hi_f, lo_a = max(federe.values()), min(ablation.values())
    ax.axhspan(hi_f, lo_a, color="black", alpha=0.05, zorder=1)
    ax.text(2.62, (hi_f + lo_a) / 2,
            f"aucun\nrecouvrement\n({lo_a - hi_f:.1f} pt)",
            fontsize=8, va="center", ha="center", color="#555555")

    ax.set_xticks([1, 2])
    ax.set_xticklabels(["Fédéré\n(8 cibles)", "Ablation\n(4 cibles)"])
    ax.set_xlim(0.6, 2.95)
    ax.set_ylabel("Temps passé en violation (%)")
    ax.set_ylim(0, 70)
    fig.tight_layout()
    fig.savefig(OUT / "fig_violation.png", bbox_inches="tight")
    plt.close(fig)
    print("  fig_violation.png")


# ── Figure 3 — bornes de comparaison (section 5.5.2) ─────────────────
def fig_bornes():
    # (oracle, systeme, meilleur statique) -- moyennes par condition
    fed = (7.5, 23.8, 81.9)
    abl = (53.1, 54.8, 83.2)

    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    y_f, y_a = 1.0, 0.35
    h = 0.16

    for y, (ora, sysv, sta), col, lab in [
        (y_f, fed, C_FED, "Fédéré"),
        (y_a, abl, C_ABL, "Ablation"),
    ]:
        ax.barh(y, sta, height=h, color=C_STA, zorder=1)
        ax.barh(y, sysv, height=h, color=col, zorder=2)
        ax.barh(y, ora, height=h, color=C_ORA, zorder=3)

        # Une bande trop etroite ne peut pas contenir son etiquette : dans ce
        # cas on la deporte au-dessus, reliee par un trait. Cas rencontres :
        # l'oracle en federe (7,5 pt) et la bande systeme en ablation (1,7 pt).
        def etiquette(x_fin, largeur, texte, couleur_texte, dy):
            if largeur >= 16:
                ax.text(x_fin - largeur / 2, y, texte, ha="center", va="center",
                        fontsize=8, color=couleur_texte, fontweight="bold",
                        zorder=4)
            else:
                ax.annotate(texte, xy=(x_fin, y + h / 2),
                            xytext=(x_fin + 5, y + h / 2 + dy), fontsize=8,
                            color=col, fontweight="bold", va="center", zorder=5,
                            arrowprops=dict(arrowstyle="-", color=col, lw=0.8))

        etiquette(ora, ora, f"oracle {ora:.1f} %", "white", 0.30)
        etiquette(sysv, sysv - ora, f"système {sysv:.1f} %", "white", 0.16)
        ax.text(sta + 1.5, y, f"statique {sta:.1f} %", va="center", fontsize=8,
                color="#555555")

        # Part du gain atteignable effectivement capturee.
        capt = 100 * (sta - sysv) / (sta - ora)
        ax.annotate(f"{capt:.0f} % du gain atteignable capturé",
                    xy=(sysv, y - h / 2 - 0.03), fontsize=8, color=col,
                    va="top", ha="left")

    ax.set_yticks([y_a, y_f])
    ax.set_yticklabels(["Ablation", "Fédéré"])
    ax.set_xlabel("Temps passé en violation (%)")
    ax.set_xlim(0, 100)
    ax.set_ylim(0.05, 1.55)
    ax.grid(axis="y", visible=False)

    handles = [
        plt.Rectangle((0, 0), 1, 1, color=C_ORA),
        plt.Rectangle((0, 0), 1, 1, color=C_FED),
        plt.Rectangle((0, 0), 1, 1, color=C_STA),
    ]
    ax.legend(handles, ["Oracle (plancher)", "Système mesuré",
                        "Meilleur placement statique"],
              loc="upper right", fontsize=8, frameon=True, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(OUT / "fig_bornes.png", bbox_inches="tight")
    plt.close(fig)
    print("  fig_bornes.png")


if __name__ == "__main__":
    print(f"Ecriture dans {OUT} :")
    fig_concordance()
    fig_violation()
    fig_bornes()
