"""
CONTROLE POSITIF du mecanisme d'information mutuelle.

CE QUE CE SCRIPT PROUVE, ET CE QU'IL NE PROUVE PAS.

  Prouve   : l'estimateur MI k-NN + le test a decalage circulaire de
             mi_test2.py DETECTENT une dependance quand elle existe
             reellement. C'est un controle de validite de la METHODE.

  Ne prouve PAS : ce qu'un deploiement reel a grande echelle donnerait.
             Aucune extrapolation n'est faite vers des VMs reelles ou une
             infrastructure de production -- ce serait inventer des
             chiffres sans donnees pour les soutenir. Les series ci-dessous
             sont SYNTHETIQUES, generees pour avoir la meme autocorrelation
             que les donnees mesurees sur le testbed (~0.99 pour la
             latence, ~0.9 pour le CPU), avec une dependance construite et
             CONNUE a l'avance entre charge et violations.

POURQUOI CE CONTROLE EST NECESSAIRE. Le testbed reel ne fournit qu'un seul
verdict : "MI ne survit pas au test ici". Sans lui, ce verdict a deux
explications indiscernables : (a) le mecanisme ne detecte rien, meme quand
il le devrait, ou (b) il n'y a simplement rien a detecter sur ce testbed
(charge et latence generees independamment, par construction). Ce script
teste (a) isolement, sur des donnees ou l'on connait la reponse.

METHODE REUTILISEE A L'IDENTIQUE de scripts/analyse/mi_test2.py : meme
estimateur (mutual_info_classif, k-NN, n_neighbors=3), meme null (decalage
circulaire preservant l'autocorrelation), meme lecture du p (fraction des
decalages dont le MI depasse l'observe).

DEUX SERIES SONT TESTEES :
  - COUPLEE   : la charge influence reellement la probabilite de violation
                (dependance construite). Le test DOIT la detecter.
  - DECOUPLEE : meme autocorrelation, mais charge et violation generees
                independamment -- comme sur le vrai testbed. Le test NE
                DOIT PAS la detecter. Sert de temoin negatif : verifie que
                le test ne s'alarme pas juste a cause de l'autocorrelation.

Aucun code de production n'est modifie ni appele : ce script est autonome.
"""
import numpy as np
from sklearn.feature_selection import mutual_info_classif

RNG = np.random.default_rng(0)
N = 1600                    # ~= nombre d'echantillons d'un run reel
AC_CIBLE_CPU = 0.90         # mesure sur le testbed reel (0.875-0.947)
AC_CIBLE_LAT = 0.995        # mesure sur le testbed reel (0.993-0.997)
N_SHIFTS = 1000


def serie_ar1(n, ac, rng):
    """Marche AR(1) de correlation lag-1 = ac, variance unitaire."""
    bruit_sigma = np.sqrt(1 - ac ** 2)
    x = np.empty(n)
    x[0] = rng.normal()
    for t in range(1, n):
        x[t] = ac * x[t - 1] + rng.normal(0, bruit_sigma)
    return x


def autocorr1(v):
    v = v - v.mean()
    d = (v * v).sum()
    return float((v[:-1] * v[1:]).sum() / d) if d else 0.0


def mi(X, y):
    return mutual_info_classif(X, y, discrete_features=False,
                                n_neighbors=3, random_state=0)


def test_decalage_circulaire(X, y, n_shifts=N_SHIFTS, rng=RNG):
    n = len(y)
    obs = mi(X, y)[0]
    decalages = rng.choice(range(10, n - 10), size=min(n_shifts, n - 20),
                            replace=False)
    nul = np.array([mi(X, np.roll(y, s))[0] for s in decalages])
    p = float((nul >= obs).mean())
    return obs, nul, p


def scenario(nom, cpu, latence, seuil):
    y = (latence > seuil).astype(int)
    X = cpu.reshape(-1, 1)
    if len(set(y)) < 2:
        print(f"{nom} : une seule classe, ajuster le seuil")
        return
    obs, nul, p = test_decalage_circulaire(X, y)
    verdict = "SIGNAL DETECTE (p < 0.05)" if p < 0.05 else "indistinguable du hasard (p >= 0.05)"
    print(f"{nom}")
    print(f"  autocorr(cpu)={autocorr1(cpu):.3f}  autocorr(latence)={autocorr1(latence):.3f}"
          f"  violations={y.sum()}/{len(y)}")
    print(f"  MI observe = {obs:.4f}  |  p95 du nul = {np.percentile(nul, 95):.4f}"
          f"  |  p = {p:.3f}")
    print(f"  >> {verdict}\n")
    return p


print("=" * 74)
print("  CONTROLE POSITIF -- meme estimateur, meme test, donnees SYNTHETIQUES")
print("=" * 74)
print()

# ── Scenario A : COUPLE -- la charge influence reellement la latence ──
cpu_a = serie_ar1(N, AC_CIBLE_CPU, RNG)
# construction explicite du lien : la latence porte une composante due au
# cpu (contention reelle), plus son propre processus lent, plus du bruit.
bruit_lat = serie_ar1(N, AC_CIBLE_LAT, np.random.default_rng(1))
latence_a = 80 + 18 * cpu_a + 6 * bruit_lat
seuil_a = np.percentile(latence_a, 65)   # meme ordre de grandeur de violations que le reel
p_couple = scenario("Scenario COUPLE (dependance construite, connue)",
                     cpu_a, latence_a, seuil_a)

# ── Scenario B : DECOUPLE -- comme le vrai testbed (marches independantes) ──
cpu_b = serie_ar1(N, AC_CIBLE_CPU, np.random.default_rng(2))
latence_b = 80 + 30 * serie_ar1(N, AC_CIBLE_LAT, np.random.default_rng(3))
seuil_b = np.percentile(latence_b, 65)
p_decouple = scenario("Scenario DECOUPLE (independant, comme le testbed reel)",
                       cpu_b, latence_b, seuil_b)

print("=" * 74)
print("  LECTURE")
print("=" * 74)
if p_couple is not None and p_decouple is not None:
    if p_couple < 0.05 < p_decouple:
        print("  Le controle passe des DEUX cotes :")
        print(f"    - couple    : p = {p_couple:.3f} < 0.05 -> detecte  (le mecanisme a du pouvoir)")
        print(f"    - decouple  : p = {p_decouple:.3f} >= 0.05 -> pas de fausse alerte")
        print()
        print("  => Le mecanisme et le test ne sont pas defaillants. L'absence de")
        print("     signal sur le testbed reel reflete l'absence de dependance")
        print("     construite dans le generateur de charge, pas une limite de")
        print("     l'estimateur ou du test statistique.")
    else:
        print("  Resultat inattendu -- a examiner avant toute conclusion.")
print()
print("  Rappel : ceci ne dit RIEN sur ce qu'un deploiement reel donnerait.")
print("  C'est un controle de la methode, pas une prediction a l'echelle.")
