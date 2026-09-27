#!/usr/bin/env python3
"""
rtty_decode.py — Décodeur RTTY (45,45 bauds, shift 170 Hz, Baudot ITA2) sur fichier WAV.

Étape 3 du projet RTTY TCI : mise au point du démodulateur sur des enregistrements réels
(produits par tci_record.py) avant de le brancher en temps réel sur le flux TCI.

Usage :
    python3 rtty_decode.py fichier.wav              # fréquences et polarité automatiques
    python3 rtty_decode.py fichier.wav --mark 2125  # mark imposé
    python3 rtty_decode.py fichier.wav --mark 2125 --reverse

Dépendance : numpy uniquement.

Fait partie d'Emile-RTTY — (C) 2026 Albert Müller, ON5AM — licence GNU GPL v3.
"""

import argparse
import sys
import wave

import numpy as np

BAUD = 45.45
SHIFT = 170.0
DECIM = 16  # 48 kHz / 16 = 3 kHz, soit ~66 échantillons par bit

# Table Baudot ITA2 (code 5 bits, bit de poids faible transmis en premier)
LTRS = ["\0", "E", "\n", "A", " ", "S", "I", "U", "\r", "D", "R", "J", "N", "F", "C", "K",
        "T", "Z", "L", "W", "H", "Y", "P", "Q", "O", "B", "G", "#", "M", "X", "V", "*"]
FIGS = ["\0", "3", "\n", "-", " ", "'", "8", "7", "\r", "$", "4", "#", ",", "!", ":", "(",
        "5", "+", ")", "2", "#", "6", "0", "1", "9", "?", "&", "#", ".", "/", "=", "*"]
CODE_FIGS, CODE_LTRS, CODE_SPACE = 27, 31, 4


def lire_wav(nom):
    with wave.open(nom, "rb") as w:
        if w.getsampwidth() != 2:
            sys.exit("Seuls les WAV 16 bits sont gérés.")
        sr, nc = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    if nc > 1:
        x = x[::nc]
    return x, sr


def filtre_adapte(sr, longueur_bits=1.4):
    """
    Filtre de démodulation adapté au débit RTTY : fenêtre en cosinus surélevé (Hann)
    couvrant 1,4 durée de bit (~3 dB de gain par rapport à un passe-bas classique).
    """
    h = np.hanning(int(round(longueur_bits * sr / BAUD)))
    return h / h.sum()


def trouver_tons(x, sr):
    """Cherche la paire de raies la plus forte espacée de ~170 Hz, avec interpolation fine."""
    n = 32768
    nb = max(1, len(x) // n)
    spec = np.zeros(n // 2 + 1)
    for i in range(nb):
        seg = x[i * n:(i + 1) * n]
        if len(seg) < n:
            seg = np.pad(seg, (0, n - len(seg)))
        spec += np.abs(np.fft.rfft(seg * np.hanning(n))) ** 2
    df = sr / n
    db = 10 * np.log10(spec + 1e-20)

    def pic_affine(k):
        # Interpolation parabolique autour du maximum pour dépasser la résolution FFT
        a, b, c = db[k - 1], db[k], db[k + 1]
        d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0
        return (k + d) * df

    kmin, kmax = int(300 / df), int(3300 / df)
    meilleur, score_max = None, -np.inf
    # Pour chaque raie candidate, on cherche sa jumelle 170 Hz plus haut (±15 Hz)
    for k in np.argsort(db[kmin:kmax])[::-1][:40] + kmin:
        lo, hi = int((SHIFT - 15) / df) + k, int((SHIFT + 15) / df) + k
        if hi >= len(db) - 1:
            continue
        k2 = lo + int(np.argmax(db[lo:hi]))
        score = min(db[k], db[k2])  # la paire vaut ce que vaut sa raie la plus faible
        if score > score_max:
            score_max, meilleur = score, (pic_affine(k), pic_affine(k2))
    if meilleur is None:
        return None
    # Les bandes latérales de manipulation faussent un peu chaque raie ; le centre de la
    # paire est plus fiable, et le shift RTTY amateur est normalisé à 170 Hz
    centre = (meilleur[0] + meilleur[1]) / 2
    return centre - SHIFT / 2, centre + SHIFT / 2


def enveloppes(x, sr, f_bas, f_haut):
    """
    Démodulation non cohérente : chaque ton est ramené à 0 Hz, passé dans le filtre
    adapté puis décimé. Retourne les enveloppes (ton bas, ton haut).
    """
    t = np.arange(len(x)) / sr
    h = filtre_adapte(sr)
    env = []
    for f in (f_bas, f_haut):
        bb = x * np.exp(-2j * np.pi * f * t)
        env.append(np.abs(np.convolve(bb, h, mode="same"))[::DECIM])
    return env


def decision_simple(a, b):
    """Comparaison directe des deux tons : d dans [-1, 1], >0 = ton bas plus fort."""
    return (a - b) / (a + b + 1e-9)


class ATC:
    """
    Correction automatique de seuil (ATC « optimale », formule de Kok Chen reprise par fldigi).

    Chaque ton a son propre suivi de crête (attaque rapide, retour lent) ; le plancher de
    bruit est suivi sur le plus faible des deux. Chaque ton est jugé par rapport à son
    niveau récent : un mark affaibli par le fading sélectif reste reconnu comme mark.
    Classe à état : utilisable bloc par bloc en temps réel.
    """

    def __init__(self, spb):
        # Poids des moyennes glissantes exprimés en échantillons (comme fldigi)
        self.w_att, self.w_crete, self.w_bruit = spb / 4, spb * 16, spb * 48
        self.ca = self.cb = self.bruit = None

    def traiter(self, a, b):
        if self.ca is None:
            self.ca, self.cb = float(a[:50].max()), float(b[:50].max())
            self.bruit = float(np.minimum(a[:50], b[:50]).min())
        ca, cb, nf = self.ca, self.cb, self.bruit
        wa, wc, wn = self.w_att, self.w_crete, self.w_bruit
        d = np.empty(len(a), np.float32)
        for i in range(len(a)):          # boucle simple : ~3000 échantillons/s seulement
            va, vb = a[i], b[i]
            ca += (va - ca) / (wa if va > ca else wc)
            cb += (vb - cb) / (wa if vb > cb else wc)
            m = va if va < vb else vb
            nf += (m - nf) / (wa if m < nf else wn)
            # Écrêtage de chaque ton entre le plancher et sa crête
            xa = min(max(va, nf), ca) - nf
            xb = min(max(vb, nf), cb) - nf
            pa, pb = ca - nf, cb - nf
            v = xa * pa - xb * pb - 0.25 * (pa * pa - pb * pb)
            # Normalisation pour retrouver une échelle [-1, 1] (utile au squelch)
            d[i] = v / (0.5 * (pa * pa + pb * pb) + 1e-12)
        self.ca, self.cb, self.bruit = ca, cb, nf
        return np.clip(d, -1, 1)


def decoder_bits(d, sr_d, uos=True, sql=0.45, d_sql=None):
    """
    Réception asynchrone type UART : start (space), 5 bits de données, stop (mark ≥1 bit).
    d > 0 = mark. Chaque bit est jugé sur la moyenne de la moitié centrale du bit.
    Squelch : un caractère n'est affiché que si ses 7 bits sont nets en moyenne
    (|d| proche de 1 = un ton domine franchement ; sur du bruit |d| reste faible).
    d_sql : signal servant à mesurer cette netteté, s'il diffère de celui qui décide
    les bits (avec l'ATC, la netteté reste mesurée sur la comparaison directe).
    """
    if d_sql is None:
        d_sql = d
    spb = sr_d / BAUD
    q = spb / 4
    texte, figs = [], False
    ok = rejets = muets = 0
    i = 1
    n = len(d)

    def moyenne(pos, s=d):  # moyenne de s sur la moitié centrale du bit centré en pos
        return s[int(pos - q):int(pos + q)].mean()

    while i < n - int(8 * spb):
        # Recherche d'un front mark -> space (début du bit de start)
        if not (d[i - 1] > 0 >= d[i]):
            i += 1
            continue
        if moyenne(i + 0.5 * spb) >= 0:  # le start doit rester en space
            i += 1
            continue
        valeurs = [moyenne(i + (0.5 + k) * spb) for k in range(7)]  # start, 5 bits, stop
        bits = [v > 0 for v in valeurs[1:6]]
        if valeurs[6] <= 0:  # stop absent : faux départ
            rejets += 1
            i += int(0.5 * spb)
            continue
        nettete = np.mean([abs(moyenne(i + (0.5 + k) * spb, d_sql)) for k in range(7)])
        if nettete < sql:  # caractère trop flou : bruit, on se tait
            muets += 1
            i += int(0.5 * spb)
            continue
        ok += 1
        code = sum(b << k for k, b in enumerate(bits))
        if code == CODE_FIGS:
            figs = True
        elif code == CODE_LTRS:
            figs = False
        else:
            if code == CODE_SPACE and uos:
                figs = False  # UOS : retour en lettres après un espace
            c = (FIGS if figs else LTRS)[code]
            if c not in ("\0", "\r"):
                texte.append(c)
        # On repart juste avant la fin du stop pour attraper le start suivant
        i += int(6.9 * spb)
    return "".join(texte), ok, rejets, muets


def main():
    p = argparse.ArgumentParser(description="Décodeur RTTY sur fichier WAV")
    p.add_argument("wav")
    p.add_argument("--mark", type=float, help="fréquence audio du mark (Hz)")
    p.add_argument("--reverse", action="store_true", help="inverser mark/space")
    p.add_argument("--no-uos", action="store_true", help="désactiver l'unshift-on-space")
    p.add_argument("--no-atc", action="store_true", help="désactiver l'ATC (comparaison directe)")
    p.add_argument("--sql", type=float, default=0.45,
                   help="squelch de 0 (ouvert) à 1 (très strict), défaut 0.45")
    a = p.parse_args()

    x, sr = lire_wav(a.wav)
    print(f"{a.wav} : {len(x) / sr:.1f} s à {sr} Hz")

    if a.mark:
        tons = (a.mark, a.mark + SHIFT)
        polarites = [a.reverse]
    else:
        tons = trouver_tons(x, sr)
        if not tons:
            sys.exit("Aucune paire de tons RTTY trouvée.")
        polarites = [False, True]  # on essaie les deux et on garde la meilleure
    print(f"Tons : {tons[0]:.1f} Hz / {tons[1]:.1f} Hz (écart {tons[1] - tons[0]:.1f} Hz)")

    a_env, b_env = enveloppes(x, sr, *tons)
    d_simple = decision_simple(a_env, b_env)
    # L'ATC décide les bits ; la netteté (squelch) reste mesurée sur la comparaison directe
    d = d_simple if a.no_atc else ATC(sr / DECIM / BAUD).traiter(a_env, b_env)
    resultats = []
    for rev in polarites:
        # Sans inversion : ton bas = mark (convention Flex RTTY / DIGL)
        s = -1 if rev else 1
        texte, ok, rejets, muets = decoder_bits(s * d, sr / DECIM, not a.no_uos, a.sql,
                                                s * d_simple)
        resultats.append((ok, rev, texte, rejets, muets))
    # La bonne polarité est celle qui produit le plus de caractères nets
    ok, rev, texte, rejets, muets = max(resultats)

    mark = tons[1] if rev else tons[0]
    print(f"Mark = {mark:.1f} Hz ({'ton haut' if rev else 'ton bas'}), "
          f"{ok} caractères, {muets} écartés par le squelch, {rejets} faux départs")
    print("-" * 60)
    print(texte)


if __name__ == "__main__":
    main()
