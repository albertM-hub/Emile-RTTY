#!/usr/bin/env python3
"""
emile_rtty.py — Emile-RTTY : terminal RTTY pour Linux via un serveur TCI (AetherSDR, ExpertSDR,
Thetis...). Nom en hommage à Émile Baudot.

RTTY amateur : 45,45 bauds, shift 170 Hz, Baudot ITA2.

Utilisation :
    python3 emile_rtty.py                 # localhost:50001
    python3 emile_rtty.py --host 192.168.1.10 --port 50001

Au premier lancement, la fenêtre « Station » demande indicatif, prénom, QTH et échange.

Émission (TCI, « trx:0,true,tci; ») : macros F1-F12, texte libre, STOP / Échap.
On émet sur la fréquence décodée (centre + AFC) avec la même polarité qu'en réception.

Macros : deux jeux, « Contest » et « QSO », chacun en lignes RUN et S&P.
Clic droit sur un bouton de macro pour la modifier. Réglages et macros sont
sauvegardés dans ~/.config/emile-rtty/config.json.
Journal : fichier ADIF choisi au lancement (un par concours, un pour le trafic courant),
changeable par le bouton « Journal… ». QSO enregistré par le bouton Log, Ctrl+L ou une
macro contenant {LOG}. Les doublons (même indicatif, même bande) sont signalés.
Concours : le bouton « Cabrillo… » produit le fichier .log à envoyer à l'organisateur.
Fichier pays cty.dat (AD1C, www.country-files.com) : pays, zone CQ et continent du
correspondant, zone reçue préremplie en contest, nouveaux multiplicateurs signalés.
Bouton « CTY… » pour le choisir ; sinon recherché dans ~/.config/emile-rtty/ et chez WSJT-X.
Double-clic sur un indicatif dans le texte reçu : il passe dans « Correspondant ».

Dans la fenêtre :
  - clic sur le spectre : centre le décodeur sur le signal (milieu entre mark et space)
    et fixe la consigne de l'AFC
  - bouton Auto : recherche automatique de la paire de tons la plus forte
  - Inverser : échange mark/space (automatique selon le mode : RTTY/DIGL = normal, DIGU/USB = inversé)
  - ATC : correction du fading sélectif (conseillé, activé par défaut)
  - AFC : recalage automatique à ±60 Hz autour de la fréquence choisie (zone grisée)
  - Squelch : de 0 (ouvert, affiche le bruit) à 1 (très strict)
  - Rafale : nb de caractères à la suite exigés avant d'afficher (anti-parasites, 1 = coupé)

Dépendances : sudo apt install python3-tk python3-numpy python3-websockets

Copyright (C) 2026 Albert Müller, ON5AM — https://hamanalyst.org
Ce programme est un logiciel libre, distribué sous licence GNU GPL version 3
(voir le fichier LICENSE). Il est fourni SANS AUCUNE GARANTIE.
"""

import argparse
import asyncio
import copy
import datetime
import json
import os
import queue
import re
import struct
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import unicodedata
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
import websockets

SR = 48000
BAUD = 45.45
SHIFT = 170.0
DECIM = 16                      # 48 kHz -> 3 kHz pour le décodage des bits
SR_D = SR / DECIM
SPB = SR_D / BAUD               # échantillons décimés par bit (~66)
F_MIN, F_MAX = 300.0, 3000.0    # plage audio affichée
TX_MAX = 60.0                   # durée maximale d'une émission (s), sécurité

# Charte graphique, reprise de hamanalyst.org : fond nuit, accent ambre, touche de cyan.
# Ambre = ce qui est à moi ou actif (texte émis, mark, options allumées) ;
# cyan = informations (space, pays du correspondant, QSO enregistrés).
C_FOND, C_PANNEAU, C_FOND_TEXTE = "#0A0D12", "#11151C", "#06080B"
C_BORD, C_TEXTE, C_GRIS = "#2A2418", "#E6E1D6", "#7D8590"
C_AMBRE, C_AMBRE_SOMBRE, C_CYAN = "#FFAA00", "#3A2A08", "#66FCF1"
C_ROUGE, C_ROUGE_SOMBRE, C_ROUGE_CLAIR = "#E24B4A", "#3A1414", "#F09595"
H_CASCADE = 120                 # hauteur de la cascade (pixels = lignes d'historique)


def palette_cascade():
    """Table de 256 couleurs de la cascade : nuit -> ambre -> jaune pâle."""
    v = np.linspace(0, 1, 256)
    return np.stack([10 + 245 * v, 13 + 170 * v ** 2, 18 + 60 * v ** 3], 1).astype(np.uint8)

# Macros par défaut, deux jeux (« contest » et « qso ») ; la touche Fn suit l'ordre de la liste.
# « groupe » = ligne d'affichage (RUN ou S&P). Variables remplacées à l'émission :
#   {MOI} mon indicatif   {DX} correspondant   {ECH} échange contest (ex. « 599 14 14 »)
#   {RST} report envoyé   {NOM} mon prénom     {QTH} ma localité
#   {LOG} enregistre le QSO dans le journal (seul dans la macro : rien n'est émis)
# « \n » = retour à la ligne (CR LF). Modifiables dans l'interface (clic droit sur le bouton).
MACROS_DEFAUT = {
    "contest": [
        {"groupe": "RUN", "nom": "CQ", "texte": "\nCQ TEST {MOI} {MOI} TEST\n"},
        {"groupe": "RUN", "nom": "Échange", "texte": "\n{DX} {ECH} {DX}\n"},
        {"groupe": "RUN", "nom": "TNX", "texte": "\nTU {MOI} TEST\n"},
        {"groupe": "RUN", "nom": "Son call", "texte": "\n{DX} {DX}\n"},
        {"groupe": "RUN", "nom": "QRZ?", "texte": "\nQRZ? {MOI}\n"},
        {"groupe": "RUN", "nom": "AGN?", "texte": "\nAGN? AGN?\n"},
        {"groupe": "RUN", "nom": "Log", "texte": "{LOG}"},
        {"groupe": "S&P", "nom": "Mon call", "texte": "\n{MOI} {MOI}\n"},
        {"groupe": "S&P", "nom": "Échange", "texte": "\nTU {ECH}\n"},
        {"groupe": "S&P", "nom": "AGN?", "texte": "\nAGN? AGN?\n"},
        {"groupe": "S&P", "nom": "Log", "texte": "{LOG}"},
    ],
    "qso": [
        {"groupe": "RUN", "nom": "CQ", "texte": "\nCQ CQ {MOI} {MOI} PSE K\n"},
        {"groupe": "RUN", "nom": "QRZ?", "texte": "\nQRZ? DE {MOI} K\n"},
        {"groupe": "RUN", "nom": "Report",
         "texte": "\n{DX} DE {MOI} TNX FOR CALL, UR RST {RST} {RST} NAME {NOM} {NOM} "
                  "QTH {QTH} {QTH} HW? {DX} DE {MOI} KN\n"},
        {"groupe": "RUN", "nom": "73",
         "texte": "\n{DX} DE {MOI} 73 OM AND THANKS FOR THIS RTTY QSO, GOOD DX KN\n"},
        {"groupe": "S&P", "nom": "Appel", "texte": "\n{MOI} {MOI}\n"},
        {"groupe": "S&P", "nom": "Report",
         "texte": "\n{DX} DE {MOI} THANKS INFOS YOUR RPT {RST} NAME {NOM}, QTH {QTH} "
                  "BTU {DX} DE {MOI} KN\n"},
        {"groupe": "S&P", "nom": "73",
         "texte": "\n{DX} DE {MOI} THANKS FOR THIS QSO GOOD DX 73 KN\n"},
    ],
}

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "emile-rtty" / "config.json"
CONFIG_DEFAUT = {
    "indicatif": "N0CALL", "nom": "", "qth": "",
    "echange": "599 14 14", "rst": "599", "mode": "contest",
    "journal": "~/emile-rtty.adi",
    "cty": "",
    "macros": MACROS_DEFAUT,
    # En-tête Cabrillo, mémorisé d'un concours à l'autre
    "cabrillo": {
        "CONTEST": "CQ-WW-RTTY", "CATEGORY-OPERATOR": "SINGLE-OP", "CATEGORY-BAND": "ALL",
        "CATEGORY-POWER": "HIGH", "CATEGORY-ASSISTED": "NON-ASSISTED",
        "ECHANGE": "599 14 DX", "NAME": "", "ADDRESS": "", "EMAIL": "", "CLUB": "",
        "SOAPBOX": "",
    },
}


def charger_config():
    """Réglages par défaut, écrasés par ceux du fichier de configuration s'il existe."""
    cfg = copy.deepcopy(CONFIG_DEFAUT)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            lu = json.load(f)
        cab = {**cfg["cabrillo"], **lu.pop("cabrillo", {})}
        cfg.update(lu, cabrillo=cab)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        print(f"Configuration illisible ({CONFIG}) : {e} — valeurs par défaut utilisées")
    return cfg


def sauver_config(cfg):
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Impossible d'écrire {CONFIG} : {e}")

# Table Baudot ITA2 (bit de poids faible transmis en premier)
LTRS = ["\0", "E", "\n", "A", " ", "S", "I", "U", "\r", "D", "R", "J", "N", "F", "C", "K",
        "T", "Z", "L", "W", "H", "Y", "P", "Q", "O", "B", "G", "\0", "M", "X", "V", "\0"]
FIGS = ["\0", "3", "\n", "-", " ", "'", "8", "7", "\r", "$", "4", "#", ",", "!", ":", "(",
        "5", "+", ")", "2", "#", "6", "0", "1", "9", "?", "&", "\0", ".", "/", "=", "\0"]
CODE_FIGS, CODE_LTRS, CODE_SPACE = 27, 31, 4


# ---------------------------------------------------------------------------
# Traitement du signal (indépendant de l'interface)
# ---------------------------------------------------------------------------

def filtre_adapte(sr, longueur_bits=1.4):
    """
    Filtre de démodulation adapté au débit RTTY : fenêtre en cosinus surélevé (Hann)
    couvrant 1,4 durée de bit. Proche du filtre adapté théorique (intégration sur un bit),
    mais avec des flancs doux qui limitent l'interférence entre bits voisins.
    Mesuré sur signaux synthétiques : ~3 dB de gain par rapport au passe-bas sinc initial.
    """
    h = np.hanning(int(round(longueur_bits * sr / BAUD)))
    return h / h.sum()


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
        d = [0.0] * len(a)
        # Boucle sur des listes Python : ~4 fois plus rapide que sur des scalaires numpy
        for i, (va, vb) in enumerate(zip(a.tolist(), b.tolist())):
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
        return np.clip(np.array(d, np.float32), -1, 1)


class Demodulateur:
    """
    Démodulateur FSK non cohérent en flux continu.
    Chaque ton est ramené à 0 Hz, passé dans le filtre adapté et décimé.
    Sorties, comprises entre -1 et 1 (>0 = ton bas plus fort) :
      - d_simple = (|bas| - |haut|) / (|bas| + |haut|), sert au squelch ;
      - d_atc    = décision corrigée par l'ATC contre le fading sélectif.
    """

    def __init__(self):
        self.h = filtre_adapte(SR).astype(np.float32)   # symétrique : pas besoin de l'inverser
        self.ntaps = len(self.h)
        self.n_total = 0            # compteur global d'échantillons (continuité de phase)
        self.attente = np.zeros(0, np.float32)
        self.regler(2125 + SHIFT / 2)

    def regler(self, centre, reinit=True):
        self.centre = centre
        self.f_bas, self.f_haut = centre - SHIFT / 2, centre + SHIFT / 2
        if reinit:
            # Changement de station : filtre et ATC repartent de zéro
            self.hist = [np.zeros(self.ntaps - 1, np.complex64) for _ in range(2)]
            self.atc = ATC(SPB)
        # Sinon (petit recalage AFC de quelques dizaines de Hz) on garde l'état :
        # la perturbation ne dure que la longueur du filtre (~30 ms)

    def traiter(self, x):
        # On ne traite que des multiples de DECIM pour garder la décimation alignée
        x = np.concatenate([self.attente, x])
        n = len(x) - len(x) % DECIM
        self.attente, x = x[n:], x[:n]
        if n == 0:
            vide = np.zeros(0, np.float32)
            return vide, vide

        t = (self.n_total + np.arange(n)) / SR
        self.n_total += n
        env = []
        for k, f in enumerate((self.f_bas, self.f_haut)):
            bb = (x * np.exp(-2j * np.pi * f * t)).astype(np.complex64)
            ext = np.concatenate([self.hist[k], bb])
            self.hist[k] = ext[-(self.ntaps - 1):]
            # Filtrage calculé uniquement aux instants conservés après décimation
            fen = np.lib.stride_tricks.sliding_window_view(ext, self.ntaps)[::DECIM]
            env.append(np.abs(fen @ self.h))
        a, b = env
        d_simple = ((a - b) / (a + b + 1e-9)).astype(np.float32)
        return d_simple, self.atc.traiter(a, b)


class DecodeurUART:
    """
    Réception asynchrone en flux : start (space), 5 bits, stop (mark).
    d > 0 = mark. Un caractère n'est retenu que si ses 7 bits sont nets (squelch).
    """

    def __init__(self):
        self.buf = np.zeros(0, np.float32)      # signal qui décide les bits
        self.buf_sql = np.zeros(0, np.float32)  # signal qui mesure la netteté (squelch)
        self.origine = 0                        # index absolu de buf[0] (horodatage)
        self.i = 1
        self.figs = False
        self.uos = True
        self.sql = 0.40
        self.qualite = 0.0          # netteté moyenne récente, pour l'affichage
        self.fenetre = 0.5          # fraction du bit moyennée autour de son centre

    def alimenter(self, d, d_sql=None):
        """
        Ajoute des échantillons décimés, retourne la liste des (caractère, instant en s).
        Les codes non affichables (LTRS, FIGS, retour chariot) sont renvoyés avec un
        caractère vide : ils ne s'affichent pas mais servent au minutage du filtre rafale.
        d décide les bits ; d_sql (par défaut d) sert à mesurer la netteté pour le squelch.
        """
        self.buf = np.concatenate([self.buf, d])
        self.buf_sql = np.concatenate([self.buf_sql, d if d_sql is None else d_sql])
        buf, bsql = self.buf, self.buf_sql
        q, sortie = max(1.0, SPB * self.fenetre / 2), []

        def moyenne(pos, s=buf):
            return s[int(pos - q):int(pos + q)].mean()

        while self.i < len(buf) - int(7.2 * SPB):
            i = self.i
            # Front mark -> space = début possible d'un bit de start
            if not (buf[i - 1] > 0 >= buf[i]) or moyenne(i + 0.5 * SPB) >= 0:
                self.i += 1
                continue
            v = [moyenne(i + (0.5 + k) * SPB) for k in range(7)]
            nettete = float(np.mean([abs(moyenne(i + (0.5 + k) * SPB, bsql)) for k in range(7)]))
            self.qualite = 0.9 * self.qualite + 0.1 * nettete
            if v[6] <= 0 or nettete < self.sql:     # pas de stop, ou caractère trop flou
                self.i += int(0.5 * SPB)
                continue
            code = sum((vb > 0) << k for k, vb in enumerate(v[1:6]))
            c = ""
            if code == CODE_FIGS:
                self.figs = True
            elif code == CODE_LTRS:
                self.figs = False
            else:
                if code == CODE_SPACE and self.uos:
                    self.figs = False
                c = (FIGS if self.figs else LTRS)[code]
                if c in ("\0", "\r"):
                    c = ""
            sortie.append((c, (self.origine + i) / SR_D))
            self.i += int(6.9 * SPB)   # on repart juste avant la fin du stop

        # On ne garde qu'un peu d'historique pour ne pas faire grossir le tampon
        coupe = max(0, self.i - int(2 * SPB))
        self.buf, self.buf_sql, self.i = self.buf[coupe:], self.buf_sql[coupe:], self.i - coupe
        self.origine += coupe
        return sortie


class FiltreRafale:
    """
    Anti-parasites : un vrai texte RTTY arrive en rafale (un caractère toutes les 165 ms),
    alors que le bruit qui passe le squelch donne des caractères isolés.
    Hors émission, les caractères sont mis en attente et ne sont affichés que si au moins
    n arrivent à la suite (écart < 0,4 s, soit un caractère perdu toléré entre eux ;
    le vrai texte arrive toutes les 165 ms). Les codes non affichables (LTRS, FIGS,
    retour chariot) servent au minutage (une émission commence souvent par eux)
    mais ne comptent pas pour confirmer une rafale.
    Une fois la rafale confirmée, tout passe directement tant que le texte continue
    (écart < 0,4 s) ; après une pause, le texte qui reprend se reconfirme sans perte.
    """

    def __init__(self, n=3, ecart=0.4, maintien=0.4):
        self.n, self.ecart, self.maintien = n, ecart, maintien
        self.attente = []
        self.dernier = -1e9     # instant du dernier caractère affiché

    def filtrer(self, evenements):
        sortie = []
        for c, t in evenements:
            if self.n <= 1 or t - self.dernier <= self.maintien:
                sortie.append((c, t))       # filtre coupé ou texte en cours : on affiche
                self.dernier = t
                continue
            if self.attente and t - self.attente[-1][1] > self.ecart:
                self.attente = []           # trop d'écart : l'attente précédente était du bruit
            self.attente.append((c, t))
            # Seuls les caractères affichables comptent pour confirmer une rafale ;
            # les codes de service ne servent qu'à mesurer les écarts
            if sum(1 for ca, _ in self.attente if ca) >= self.n:  # rafale confirmée
                sortie += self.attente
                self.dernier = t
                self.attente = []
        return sortie


class AFC:
    """
    Contrôle automatique de fréquence.

    Mesures sur enregistrements réels : une station ne dérive quasiment pas pendant une
    émission (±3 Hz), mais en contest les émissions successives (appelants) arrivent
    décalées jusqu'à ±50 Hz ; le décodage se dégrade au-delà de ~25 Hz d'erreur.
    Donc : toutes les 250 ms, on cherche la paire mark/space la plus forte dans une
    fenêtre de ±60 Hz autour de la CONSIGNE choisie par l'opérateur (clic ou Auto).
    La fenêtre restant ancrée sur la consigne, l'AFC ne peut pas partir à la dérive
    vers une autre station. On ne se recale que si la paire sort nettement du bruit.
    """

    def __init__(self, fenetre=60.0, seuil_db=12.0):
        self.fenetre, self.seuil_db = fenetre, seuil_db
        self.n = 16384                      # ~0,34 s, résolution ~2,9 Hz
        self.win = np.hanning(self.n)
        self.f = np.fft.rfftfreq(self.n, 1 / SR)
        self.k_shift = SHIFT / (SR / self.n)

    def estimer(self, x, consigne):
        """x : ~0,5 s d'audio récent. Retourne le centre mesuré, ou None si rien de net."""
        n = self.n
        if len(x) < n:
            return None
        spec = (np.abs(np.fft.rfft(x[-n:] * self.win)) ** 2
                + np.abs(np.fft.rfft(x[-n - n // 2:-n // 2] * self.win)) ** 2
                if len(x) >= n + n // 2 else np.abs(np.fft.rfft(x[-n:] * self.win)) ** 2)
        db = 10 * np.log10(spec + 1e-20)
        f = self.f
        # Plancher de bruit estimé autour de la zone de recherche (tons compris)
        zone_bruit = (f > consigne - self.fenetre - 150) & (f < consigne + self.fenetre + 150)
        plancher = np.median(db[zone_bruit])
        # Pour chaque ton bas possible, on cherche son jumeau 170 Hz plus haut (±3 cases)
        bas = np.where((f >= consigne - self.fenetre - SHIFT / 2) &
                       (f <= consigne + self.fenetre - SHIFT / 2))[0]
        meilleur, score_max = None, -np.inf
        for k in bas:
            k2c = int(round(k + self.k_shift))
            k2 = k2c - 3 + int(np.argmax(db[k2c - 3:k2c + 4]))
            score = min(db[k], db[k2])      # une paire vaut sa raie la plus faible
            if score > score_max:
                score_max, meilleur = score, (f[k] + f[k2]) / 2
        if score_max - plancher < self.seuil_db:
            return None
        return meilleur


def trouver_centre(x):
    """Cherche la paire de raies la plus forte espacée de ~170 Hz ; retourne son centre."""
    # Spectre moyenné sur tranches de 16384 échantillons (~0,34 s, résolution ~3 Hz),
    # pour exploiter tout l'historique et pas seulement la dernière fraction de seconde
    n = 16384
    if len(x) < n:
        return None
    fen = np.hanning(n)
    spec = sum(np.abs(np.fft.rfft(x[k:k + n] * fen)) ** 2
               for k in range(len(x) - n, -1, -n // 2))
    db = 10 * np.log10(spec + 1e-20)
    df = SR / n
    kmin, kmax = int(F_MIN / df), int((F_MAX - SHIFT) / df)
    meilleur, score_max = None, -np.inf
    for k in np.argsort(db[kmin:kmax])[::-1][:40] + kmin:
        lo, hi = k + int((SHIFT - 15) / df), k + int((SHIFT + 15) / df)
        k2 = lo + int(np.argmax(db[lo:hi]))
        score = min(db[k], db[k2])
        if score > score_max:
            score_max, meilleur = score, (k + k2) / 2 * df
    return meilleur


# ---------------------------------------------------------------------------
# Émission : codage Baudot et génération AFSK
# ---------------------------------------------------------------------------

class GenerateurRTTY:
    """
    Transforme un texte en audio AFSK RTTY (45,45 bauds, 1,5 bit de stop).

    - Codage Baudot avec gestion LTRS/FIGS ; après un espace on considère que le
      récepteur est revenu en lettres (UOS, réglage par défaut de tous les logiciels),
      donc un chiffre qui suit un espace est toujours précédé d'un FIGS.
    - Phase continue ; fréquence lissée aux transitions (~4 ms) pour un spectre étroit.
    - Montée et descente d'amplitude de 10 ms : pas de clic à l'émission.
    """

    TRANSITION = 0.004      # durée du lissage des transitions mark/space (s)
    RAMPE = 0.010           # montée / descente d'amplitude (s)

    def __init__(self, amplitude=0.5):
        self.amplitude = amplitude
        self.table_l = {c: i for i, c in enumerate(LTRS) if c not in ("\0",)}
        self.table_f = {c: i for i, c in enumerate(FIGS) if c not in ("\0", "#")}
        self.table_f["#"] = 11          # un seul code retenu pour « # »

    def coder(self, texte):
        """Texte -> liste de codes Baudot 5 bits (commence par deux LTRS de synchronisation)."""
        codes, figs = [CODE_LTRS, CODE_LTRS], False
        texte = texte.upper().replace("\r\n", "\n")
        for ch in texte:
            if ch == "\n":
                codes += [8, 2]                         # CR LF
                continue
            if ch == " ":
                codes.append(CODE_SPACE)
                figs = False                            # UOS côté récepteur
                continue
            if ch in self.table_l and ch not in ("\r", "\n"):
                if figs:
                    codes.append(CODE_LTRS)
                    figs = False
                codes.append(self.table_l[ch])
            elif ch in self.table_f:
                if not figs:
                    codes.append(CODE_FIGS)
                    figs = True
                codes.append(self.table_f[ch])
            # caractère sans équivalent Baudot : ignoré
        return codes

    def audio(self, codes, f_mark, f_space, amorce=0.15, queue=0.10):
        """Codes Baudot -> échantillons float32 à 48 kHz."""
        bit = SR / BAUD
        # Trajectoire de fréquence : amorce en mark, puis chaque caractère
        # start (space), 5 bits (poids faible d'abord), 1,5 stop (mark)
        f = [np.full(int(amorce * SR), f_mark)]
        position, cumul = 0.0, 0
        for code in codes:
            niveaux = [0] + [(code >> k) & 1 for k in range(5)] + [1]
            durees = [1, 1, 1, 1, 1, 1, 1.5]
            for niv, du in zip(niveaux, durees):
                position += du * bit
                n = int(round(position)) - cumul        # pas de dérive d'horloge cumulée
                cumul += n
                f.append(np.full(n, f_mark if niv else f_space))
        f.append(np.full(int(queue * SR), f_mark))
        f = np.concatenate(f)
        # Lissage des transitions de fréquence (fenêtre de Hann)
        k = np.hanning(max(3, int(self.TRANSITION * SR)))
        f = np.convolve(f, k / k.sum(), mode="same")
        f[:len(k)] = f_mark
        f[-len(k):] = f_mark
        phase = 2 * np.pi * np.cumsum(f) / SR           # phase continue
        x = np.sin(phase)
        # Enveloppe : montée et descente en cosinus
        r = int(self.RAMPE * SR)
        env = np.ones(len(x))
        env[:r] = 0.5 - 0.5 * np.cos(np.pi * np.arange(r) / r)
        env[-r:] = env[:r][::-1]
        return (self.amplitude * env * x).astype(np.float32)


# ---------------------------------------------------------------------------
# Fichier pays cty.dat (AD1C)
# ---------------------------------------------------------------------------

# Emplacements où l'on cherche un cty.dat si aucun n'a été choisi
CTY_CANDIDATS = [CONFIG.parent / "cty.dat", Path.home() / ".local/share/WSJT-X/cty.dat",
                 Path("/usr/share/wsjtx/cty.dat"), Path.home() / ".fldigi/cty.dat"]

# Suffixes qui ne changent pas le pays (portable, mobile, QRP, phare...)
SUFFIXES_NEUTRES = {"P", "M", "A", "QRP", "QRPP", "LH", "B", "R", "AM", "J", "E"}


class CTY:
    """
    Lecture du cty.dat : une entité par bloc terminé par « ; »

        Madeira Islands:  33:  36:  AF:  32.75:  16.95:  0.0:  CT3:
            CQ3,CQ9,CR3,CR9,CS3,CS9,CT3,CT9,=CR3DX;

    8 champs (pays, zone CQ, zone ITU, continent, lat, lon, décalage UTC, préfixe
    principal ; « * » devant = entité WAE seulement, comptée en CQ WW), puis les alias.
    Alias : « = » devant = indicatif exact ; (n) zone CQ, [n] zone ITU, {XX} continent
    propres à cet alias.
    """

    RE_ALIAS = re.compile(r"(=?)([A-Z0-9/]+)(.*)")

    def __init__(self, chemin):
        self.chemin = Path(chemin)
        self.exacts, self.prefixes = {}, {}
        self.version = ""
        texte = self.chemin.read_text(encoding="latin-1")
        for bloc in texte.split(";"):
            morceaux = bloc.split(":", 8)
            if len(morceaux) < 9:
                continue
            pays, cq, _itu, cont, _la, _lo, _utc, principal, alias = (m.strip() for m in morceaux)
            base = {"pays": pays, "cq": int(cq), "cont": cont.upper(), "pfx": principal}
            for a in alias.replace("\n", " ").split(","):
                m = self.RE_ALIAS.match(a.strip().upper())
                if not m:
                    continue
                exact, code, options = m.groups()
                info = dict(base)
                if z := re.search(r"\((\d+)\)", options):
                    info["cq"] = int(z.group(1))
                if c := re.search(r"\{(\w+)\}", options):
                    info["cont"] = c.group(1)
                if exact and code.startswith("VER") and code[3:].isdigit():
                    self.version = code[3:]         # « =VER20260915 » : date du fichier
                (self.exacts if exact else self.prefixes)[code] = info
        if not self.prefixes:
            raise ValueError("aucune entité trouvée : ce n'est pas un cty.dat")

    @staticmethod
    def _pour_prefixe(call):
        """
        Partie de l'indicatif qui détermine le pays :
          EA8/ON5AM -> EA8   ON5AM/EA8 -> EA8   ON5AM/P -> ON5AM   W1AW/4 -> W4
        Retourne None pour un maritime mobile (/MM : pas de pays).
        """
        parties = [p for p in call.split("/") if p]
        if "MM" in parties:
            return None
        parties = [p for p in parties if p not in SUFFIXES_NEUTRES]
        if not parties:
            return call
        if len(parties) == 1:
            return parties[0]
        a, b = parties[0], parties[-1]
        if b.isdigit() and len(b) == 1:
            # W1AW/4 : même préfixe, autre région d'appel
            m = re.match(r"([A-Z0-9]*?[A-Z])(\d)", a)
            return (m.group(1) + b) if m else a
        return a if len(a) <= len(b) else b

    def chercher(self, call):
        """dict {pays, cq, cont, pfx} ou None si inconnu."""
        call = call.upper().strip()
        if call in self.exacts:
            return self.exacts[call]
        base = self._pour_prefixe(call)
        if base is None:
            return None
        if base in self.exacts:
            return self.exacts[base]
        for n in range(len(base), 0, -1):       # préfixe le plus long d'abord
            if base[:n] in self.prefixes:
                return self.prefixes[base[:n]]
        return None


def charger_cty(chemin_config):
    """cty.dat choisi dans la configuration, sinon le premier trouvé ; None si aucun."""
    candidats = ([Path(chemin_config).expanduser()] if chemin_config else []) + CTY_CANDIDATS
    for c in candidats:
        if c.is_file():
            try:
                return CTY(c)
            except (OSError, ValueError) as e:
                print(f"cty.dat illisible ({c}) : {e}")
    return None


# ---------------------------------------------------------------------------
# Journal ADIF
# ---------------------------------------------------------------------------

class Journal:
    """
    Journal minimal au format ADIF (ajout en fin de fichier, relu au démarrage).
    Importable tel quel dans N1MM, Log4OM, CQRLOG, StationMaster, etc.
    Sert aussi à signaler les doublons : même indicatif sur la même bande.
    """

    BANDES = [(1.8, 2.0, "160M"), (3.5, 4.0, "80M"), (5.3, 5.45, "60M"), (7.0, 7.3, "40M"),
              (10.1, 10.15, "30M"), (14.0, 14.35, "20M"), (18.068, 18.168, "17M"),
              (21.0, 21.45, "15M"), (24.89, 24.99, "12M"), (28.0, 29.7, "10M"), (50.0, 54.0, "6M")]

    def __init__(self, chemin):
        self.chemin = Path(chemin).expanduser()
        self.contacts = set()           # (indicatif, bande) déjà dans le journal
        self._relire()

    @classmethod
    def bande(cls, f_hz):
        mhz = f_hz / 1e6
        return next((nom for bas, haut, nom in cls.BANDES if bas <= mhz <= haut), "")

    @staticmethod
    def _champ(enreg, nom):
        # Champ ADIF : <NOM:longueur>valeur (un type optionnel peut suivre la longueur)
        m = re.search(rf"<{nom}:(\d+)(?::[^>]*)?>", enreg, re.I)
        return enreg[m.end():m.end() + int(m.group(1))] if m else ""

    CHAMPS = ("CALL", "QSO_DATE", "TIME_ON", "BAND", "FREQ", "MODE", "RST_SENT", "RST_RCVD",
              "STX_STRING", "SRX_STRING")

    def lire(self):
        """Tous les QSO du fichier, sous forme de dict (champs utiles seulement)."""
        try:
            texte = self.chemin.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        except OSError as e:
            print(f"Journal illisible ({self.chemin}) : {e}")
            return []
        texte = re.split(r"<eoh>", texte, flags=re.I)[-1]    # on saute l'en-tête
        qsos = []
        for enreg in re.split(r"<eor>", texte, flags=re.I):
            q = {c: self._champ(enreg, c).strip().upper() for c in self.CHAMPS}
            if q["CALL"]:
                qsos.append(q)
        return qsos

    def _relire(self):
        for q in self.lire():
            self.contacts.add((q["CALL"], q["BAND"]))

    def doublon(self, call, f_hz):
        return (call.upper(), self.bande(f_hz)) in self.contacts

    def ajouter(self, champs):
        """champs : dict nom ADIF -> valeur (les valeurs vides sont omises)."""
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        neuf = not self.chemin.exists()
        with open(self.chemin, "a", encoding="utf-8") as f:
            if neuf:
                f.write("Journal Emile-RTTY\n<ADIF_VER:5>3.1.4 <PROGRAMID:10>Emile-RTTY <EOH>\n")
            f.write(" ".join(f"<{k}:{len(str(v))}>{v}" for k, v in champs.items() if v)
                    + " <EOR>\n")
        self.contacts.add((champs["CALL"].upper(), champs.get("BAND", "").upper()))


def separer_echange(texte):
    """« 599 14 14 » -> (« 599 », « 14 ») : report d'abord, répétitions supprimées."""
    mots = texte.upper().split()
    rst = mots.pop(0) if mots and re.fullmatch(r"[1-5][1-9][1-9]", mots[0]) else ""
    reste = []
    for m in mots:
        if not reste or reste[-1] != m:
            reste.append(m)
    return rst, " ".join(reste)


def ascii_seul(texte):
    """Cabrillo = ASCII : « Müller » devient « Muller » plutôt qu'un caractère illisible."""
    return unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()


def ecrire_cabrillo(chemin, qsos, entete, indicatif):
    """
    Écrit un log Cabrillo 3.0. Ligne QSO (gabarit CQ WW RTTY) :
      QSO: freq mo date       time envoyé : call rst exch   reçu : call rst exch
      QSO: 14085 RY 2026-09-26 1651 ON5AM 599 14 DX  CR3DX 599 33 DX
    Retourne le nombre de QSO sans échange reçu (à corriger avant envoi).
    """
    rst_env, ech_env = separer_echange(entete["ECHANGE"])
    # En CQ WW RTTY, les stations hors W/VE mettent « DX » à la place de l'État/province
    cqww = entete["CONTEST"].upper() == "CQ-WW-RTTY"
    debut_bande = {nom: int(bas * 1000) for bas, _h, nom in Journal.BANDES}
    lignes, incomplets = [], 0
    for q in sorted(qsos, key=lambda q: (q["QSO_DATE"], q["TIME_ON"])):
        try:
            khz = int(round(float(q["FREQ"]) * 1000))
        except ValueError:
            khz = debut_bande.get(q["BAND"], 0)   # à défaut de fréquence : début de bande
        d, t = q["QSO_DATE"], q["TIME_ON"]
        ech_rec = q["SRX_STRING"]
        if not ech_rec:
            incomplets += 1
        elif cqww and len(ech_rec.split()) == 1:
            ech_rec += " DX"
        lignes.append(f"QSO: {khz:>5} RY {d[:4]}-{d[4:6]}-{d[6:8]} {t[:4]} "
                      f"{indicatif:<13} {rst_env or '599':<3} {ech_env:<6} "
                      f"{q['CALL']:<13} {q['RST_RCVD'] or '599':<3} {ech_rec}")
    ent = ["START-OF-LOG: 3.0", f"CONTEST: {entete['CONTEST']}", f"CALLSIGN: {indicatif}",
           f"CATEGORY-OPERATOR: {entete['CATEGORY-OPERATOR']}",
           f"CATEGORY-BAND: {entete['CATEGORY-BAND']}",
           f"CATEGORY-POWER: {entete['CATEGORY-POWER']}", "CATEGORY-MODE: RTTY",
           f"CATEGORY-ASSISTED: {entete['CATEGORY-ASSISTED']}",
           "CATEGORY-TRANSMITTER: ONE", "CATEGORY-STATION: FIXED",
           f"OPERATORS: {indicatif}"]
    for cle in ("CLUB", "NAME", "ADDRESS", "EMAIL", "SOAPBOX"):
        if entete.get(cle, "").strip():
            ent.append(f"{cle}: {entete[cle].strip()}")
    ent.append("CREATED-BY: Emile-RTTY")
    texte = "\n".join(ent + lignes + ["END-OF-LOG:", ""])
    Path(chemin).write_text(ascii_seul(texte), encoding="ascii")
    return incomplets


# Indicatif plausible : au moins une lettre et un chiffre, préfixe/suffixe « / » admis
RE_INDICATIF = re.compile(r"(?=[A-Z0-9/]*\d)(?=[A-Z0-9/]*[A-Z])[A-Z0-9]+(?:/[A-Z0-9]+)*")


# ---------------------------------------------------------------------------
# Client TCI (thread séparé avec sa propre boucle asyncio)
# ---------------------------------------------------------------------------

class ClientTCI(threading.Thread):
    """
    Client TCI dans un thread séparé (boucle asyncio propre).

    Réception : trames RX_AUDIO (type 1) -> file vers l'interface.
    Émission : sur demande de l'interface, envoie « trx:0,true,tci; », puis répond à
    chaque trame TX_CHRONO (type 3) du serveur par une trame TX_AUDIO (type 2) contenant
    exactement le nombre d'échantillons demandé. Si aucun TX_CHRONO n'arrive, l'audio
    est poussé au rythme de l'horloge (mode de secours). Fin : « trx:0,false; ».
    """

    TYPE_RX, TYPE_TX, TYPE_CHRONO = 1, 2, 3
    SILENCE_FIN = 0.3        # s de silence envoyés après l'audio avant de repasser en RX
    DELAI_CHRONO = 0.5       # s sans TX_CHRONO avant de passer en mode de secours

    def __init__(self, host, port, rx, file_sortie):
        super().__init__(daemon=True)
        self.uri, self.rx, self.q = f"ws://{host}:{port}", rx, file_sortie
        self.stop_evt = threading.Event()
        self.commandes = queue.Queue()      # ordres de l'interface : émettre / stop
        self.tx = None                      # audio en cours d'émission (np.float32)
        self.tx_pos = 0
        self.tx_debut = 0.0
        self.chrono_vu = False

    # --- appels depuis l'interface (thread tkinter) ---------------------------------
    def emettre(self, audio):
        self.commandes.put(("emettre", audio))

    def arreter_tx(self):
        self.commandes.put(("stop", None))

    # --- boucle réseau ----------------------------------------------------------------
    def run(self):
        try:
            asyncio.run(self._boucle())
        except Exception as e:  # noqa: BLE001 — tout échec est remonté à l'interface
            self.q.put(("etat", f"Erreur : {e}"))
        self.q.put(("deconnecte",))

    async def _boucle(self):
        self.q.put(("etat", f"Connexion à {self.uri} ..."))
        async with websockets.connect(self.uri, max_size=None) as ws:
            self.ws = ws
            for cmd in ("audio_samplerate:48000;", "audio_stream_sample_type:float32;",
                        "audio_stream_channels:1;", "audio_stream_samples:512;",
                        f"audio_start:{self.rx};"):
                await ws.send(cmd)
            self.q.put(("etat", "Connecté"))
            boucle = asyncio.get_running_loop()
            while not self.stop_evt.is_set():
                await self._traiter_commandes(boucle)
                try:
                    msg = await asyncio.wait_for(ws.recv(), 0.02)
                except asyncio.TimeoutError:
                    msg = None
                if isinstance(msg, bytes):
                    await self._binaire(msg)
                elif msg:
                    self._texte(msg)
                await self._secours_tx(boucle)
            if self.tx is not None:
                await self._fin_tx()
            await ws.send(f"audio_stop:{self.rx};")

    async def _traiter_commandes(self, boucle):
        while not self.commandes.empty():
            ordre, audio = self.commandes.get_nowait()
            if ordre == "emettre" and self.tx is None:
                silence = np.zeros(int(self.SILENCE_FIN * SR), np.float32)
                self.tx, self.tx_pos = np.concatenate([audio, silence]), 0
                self.tx_debut, self.chrono_vu = boucle.time(), False
                await self.ws.send(f"trx:{self.rx},true,tci;")
                self.q.put(("tx", True))
            elif ordre == "stop" and self.tx is not None:
                await self._fin_tx()

    async def _envoyer_bloc(self, n):
        """Envoie n échantillons de l'audio TX (complétés par du silence si besoin)."""
        bloc = self.tx[self.tx_pos:self.tx_pos + n]
        if len(bloc) < n:
            bloc = np.concatenate([bloc, np.zeros(n - len(bloc), np.float32)])
        entete = struct.pack("<16I", self.rx, SR, 3, 0, 0, n, self.TYPE_TX, 1, *([0] * 8))
        await self.ws.send(entete + bloc.astype("<f4").tobytes())
        self.tx_pos += n
        if self.tx_pos >= len(self.tx):
            await self._fin_tx()

    async def _secours_tx(self, boucle):
        """Sans TX_CHRONO du serveur, on pousse l'audio au rythme de l'horloge."""
        if self.tx is None or self.chrono_vu:
            return
        ecoule = boucle.time() - self.tx_debut
        if ecoule < self.DELAI_CHRONO:
            return
        du = int((ecoule - self.DELAI_CHRONO) * SR) - self.tx_pos
        while du >= 512 and self.tx is not None:
            await self._envoyer_bloc(512)
            du -= 512

    async def _fin_tx(self):
        self.tx = None
        await self.ws.send(f"trx:{self.rx},false;")
        self.q.put(("tx", False))

    async def _binaire(self, data):
        if len(data) < 64:
            return
        recepteur, _sr, fmt, _c, _crc, longueur, typ, canaux = struct.unpack("<8I", data[:32])
        if recepteur != self.rx:
            return
        if typ == self.TYPE_CHRONO:
            # Le serveur réclame « longueur » échantillons d'audio TX
            if self.tx is not None:
                self.chrono_vu = True
                await self._envoyer_bloc(longueur)
            return
        if typ == self.TYPE_RX and fmt == 3 and len(data) > 64:
            s = np.frombuffer(data[64:], dtype="<f4")
            self.q.put(("audio", s[0::2] if canaux == 2 else s))

    def _texte(self, msg):
        for cmd in msg.split(";"):
            nom, _, val = cmd.strip().partition(":")
            champs = val.split(",")
            if not champs or champs[0] != str(self.rx):
                continue
            if nom.lower() == "vfo" and champs[-1].isdigit():
                self.q.put(("vfo", int(champs[-1])))
            elif nom.lower() == "modulation" and len(champs) > 1:
                self.q.put(("mode", champs[1].lower()))


# ---------------------------------------------------------------------------
# Interface tkinter
# ---------------------------------------------------------------------------

class Application:
    def __init__(self, racine, args):
        self.r, self.args = racine, args
        self.q = queue.Queue()
        self.client = None
        self.demod, self.uart = Demodulateur(), DecodeurUART()
        self.historique = np.zeros(SR * 2, np.float32)   # 2 s d'audio pour spectre et Auto
        self.tons = ""                      # « centre 2210 Hz  AFC +3 » pour la barre d'état
        self.largeur = 600                  # largeur courante de la cascade (suit la fenêtre)
        self.cascade = np.zeros((H_CASCADE, self.largeur, 3), np.uint8)
        self.lut = palette_cascade()
        self.plancher = None                # niveau de bruit lissé de la cascade (dB)
        self.n_vu = 0                       # échantillons déjà montrés dans la cascade
        self.dernier_car = -1e9     # instant (s) du dernier caractère affiché
        self.rafale = FiltreRafale()
        self.afc = AFC()
        self.consigne = self.demod.centre   # fréquence choisie par l'opérateur (clic / Auto)
        self.pas_afc = 0                    # échantillons reçus depuis la dernière mesure AFC
        self.generateur = GenerateurRTTY()
        self.en_tx = False
        self.fin_tx = 0.0                   # instant (time.monotonic) de la fin de la dernière émission
        self.afc_prec = None                # mesure AFC précédente (confirmation en deux temps)
        self.tx_limite = 0.0                # instant (time.monotonic) de coupure de sécurité
        self.n_recu = 0
        self.etat, self.mode = "Déconnecté", ""   # éléments de la barre d'état
        self.cfg = charger_config()
        # Les options de la ligne de commande priment sur le fichier de configuration
        if args.indicatif:
            self.cfg["indicatif"] = args.indicatif.upper()
        if args.echange:
            self.cfg["echange"] = args.echange.upper()
        self.journal = Journal(self.cfg["journal"])
        self.vfo_hz = 0
        self.cty = charger_cty(self.cfg.get("cty"))
        self.recu_auto = ""             # dernière zone préremplie (écrasable tant qu'intacte)
        self.mults = set()              # (bande, "P", préfixe pays) et (bande, "Z", zone) déjà faits

        racine.title(f"Emile-RTTY — {self.cfg['indicatif']}")
        racine.protocol("WM_DELETE_WINDOW", self._quitter)
        self._construire()
        self._maj_titre_journal()
        self._recalculer_mults()
        if self.cty:
            self.etat = f"cty.dat {self.cty.version or ''} chargé ({self.cty.chemin})"
        self.r.after(50, self._scruter)
        self.r.after(150, self._rafraichir)
        # Une fois la fenêtre affichée : réglages de la station au tout premier lancement,
        # puis choix du journal
        if self.cfg["indicatif"] in ("", "N0CALL"):
            self.r.after(300, lambda: self._reglages_station(ensuite=self._choisir_journal))
        else:
            self.r.after(300, self._choisir_journal)

    # --- construction de la fenêtre -------------------------------------------------
    def _polices(self):
        """Polices du système (lisibles partout, rien à installer)."""
        mono = tkfont.nametofont("TkFixedFont").actual("family")
        sans = tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_mono, self.f_mono_p = (mono, 11), (mono, 9)
        self.f_freq, self.f_indic, self.f_texte = (mono, 20), (mono, 22), (mono, 13)
        self.f_sans, self.f_sans_p = (sans, 10), (sans, 9)
        self.f_titre, self.f_badge = (sans, 15, "bold"), (sans, 11, "bold")

    def _style_ttk(self):
        """Thème sombre pour les fenêtres secondaires (édition de macro, Cabrillo)."""
        st = ttk.Style(self.r)
        st.theme_use("clam")
        st.configure(".", background=C_FOND, foreground=C_TEXTE, fieldbackground=C_PANNEAU,
                     bordercolor=C_BORD, lightcolor=C_FOND, darkcolor=C_FOND,
                     insertcolor=C_AMBRE, selectbackground=C_AMBRE_SOMBRE,
                     selectforeground=C_AMBRE, arrowcolor=C_AMBRE)
        st.configure("TButton", background=C_PANNEAU, padding=(10, 3))
        st.map("TButton", background=[("active", C_AMBRE_SOMBRE)],
               foreground=[("active", C_AMBRE)])
        st.map("TCombobox", fieldbackground=[("readonly", C_PANNEAU)])
        self.r.option_add("*TCombobox*Listbox.background", C_PANNEAU)
        self.r.option_add("*TCombobox*Listbox.foreground", C_TEXTE)
        self.r.option_add("*TCombobox*Listbox.selectBackground", C_AMBRE_SOMBRE)

    # Petits constructeurs de widgets aux couleurs de la charte
    def _bouton(self, parent, texte, cmd, fond=C_PANNEAU, fg=C_TEXTE, **kw):
        return tk.Button(parent, text=texte, command=cmd, bg=fond, fg=fg, font=self.f_sans,
                         activebackground=C_AMBRE_SOMBRE, activeforeground=C_AMBRE,
                         relief="flat", bd=0, highlightthickness=1, padx=8, pady=3,
                         highlightbackground=C_BORD, highlightcolor=C_AMBRE,
                         cursor="hand2", **kw)

    def _saisie(self, parent, var, largeur, police=None):
        return tk.Entry(parent, textvariable=var, width=largeur, bg=C_PANNEAU, fg=C_TEXTE,
                        insertbackground=C_AMBRE, relief="flat", highlightthickness=1,
                        highlightbackground=C_BORD, highlightcolor=C_AMBRE,
                        selectbackground=C_AMBRE_SOMBRE, selectforeground=C_AMBRE,
                        font=police or self.f_mono)

    def _etiquette(self, parent, texte="", fg=C_GRIS, police=None, fond=C_FOND, **kw):
        return tk.Label(parent, text=texte, fg=fg, bg=fond, font=police or self.f_sans, **kw)

    def _puce(self, parent, texte, var):
        """Interrupteur façon « pastille » : ambre allumé, gris éteint."""
        lab = tk.Label(parent, text=texte, font=self.f_mono_p, padx=8, pady=1,
                       cursor="hand2", highlightthickness=1, bg=C_FOND)

        def peindre(*_):
            on = var.get()
            lab.config(fg=C_AMBRE if on else C_GRIS,
                       highlightbackground=C_AMBRE if on else C_BORD)
        lab.bind("<Button-1>", lambda ev: var.set(not var.get()))
        var.trace_add("write", peindre)
        peindre()
        return lab

    def _carte(self, parent):
        return tk.Frame(parent, bg=C_PANNEAU, highlightthickness=1, highlightbackground=C_BORD)

    def _construire(self):
        self._polices()
        self._style_ttk()
        self.r.configure(bg=C_FOND)

        # En-tête : nom, indicatif, fréquence, bande, voyant RX/TX
        haut = tk.Frame(self.r, bg=C_FOND)
        haut.pack(fill="x", padx=10, pady=(8, 0))
        self._etiquette(haut, "EMILE", C_AMBRE, self.f_titre).pack(side="left")
        self._etiquette(haut, "·RTTY", C_TEXTE, self.f_titre).pack(side="left")
        self.l_moi = self._etiquette(haut, self.cfg["indicatif"], C_GRIS, self.f_mono)
        self.l_moi.pack(side="left", padx=14)
        self.l_tx = tk.Label(haut, text=" RX ", bg=C_AMBRE, fg=C_FOND, font=self.f_badge, padx=8)
        self.l_tx.pack(side="right")
        self.l_bande = self._etiquette(haut, "MHz", C_GRIS, self.f_mono)
        self.l_bande.pack(side="right", padx=12)
        self.l_freq_fin = self._etiquette(haut, "", C_AMBRE, self.f_freq)
        self.l_freq_fin.pack(side="right")
        self.l_freq = self._etiquette(haut, "—", C_TEXTE, self.f_freq)
        self.l_freq.pack(side="right")
        tk.Frame(self.r, bg=C_BORD, height=1).pack(fill="x", padx=10, pady=6)

        # Le bas de la fenêtre est placé avant le corps pour ne jamais être écrasé
        self._construire_bas()

        corps = tk.Frame(self.r, bg=C_FOND)
        corps.pack(fill="both", expand=True, padx=10)
        droite = tk.Frame(corps, bg=C_FOND, width=230)
        droite.pack(side="right", fill="y", padx=(10, 0))
        gauche = tk.Frame(corps, bg=C_FOND)
        gauche.pack(side="left", fill="both", expand=True)
        self._construire_cascade(gauche)
        self._construire_reglages(gauche)

        self.texte = tk.Text(gauche, wrap="word", bg=C_FOND_TEXTE, fg=C_TEXTE,
                             insertbackground=C_AMBRE, font=self.f_texte, relief="flat",
                             highlightthickness=1, highlightbackground=C_BORD,
                             highlightcolor=C_BORD, padx=10, pady=8,
                             selectbackground=C_AMBRE_SOMBRE, selectforeground=C_AMBRE)
        self.texte.pack(fill="both", expand=True, pady=(8, 0))
        self.texte.tag_configure("tx", foreground=C_AMBRE)                      # texte émis
        self.texte.tag_configure("log", foreground=C_CYAN, font=self.f_mono_p)  # QSO enregistrés
        self.texte.bind("<Double-Button-1>", self._double_clic_texte)
        self._construire_droite(droite)
        self._changer_mode()
        self._maj_tons()

    def _construire_cascade(self, parent):
        """Cascade (waterfall) : chaque ligne = spectre de l'instant, les plus récentes en haut."""
        self.canvas = tk.Canvas(parent, height=H_CASCADE, bg=C_FOND_TEXTE, highlightthickness=1,
                                highlightbackground=C_BORD, cursor="crosshair")
        self.canvas.pack(fill="x")
        self.img_cascade = self.canvas.create_image(1, 1, anchor="nw")
        self.photo = None
        # Zone de capture de l'AFC (±60 Hz autour de la consigne, tons compris)
        self.zone_afc = self.canvas.create_rectangle(0, 0, 0, 0, outline=C_AMBRE, dash=(2, 4))
        self.m_bas = self.canvas.create_line(0, 0, 0, 0, fill=C_AMBRE)
        self.m_haut = self.canvas.create_line(0, 0, 0, 0, fill=C_CYAN)
        self.t_bas = self.canvas.create_text(0, 4, anchor="ne", font=self.f_mono_p, fill=C_AMBRE)
        self.t_haut = self.canvas.create_text(0, 4, anchor="nw", font=self.f_mono_p, fill=C_CYAN)
        self.canvas.bind("<Button-1>", self._clic_spectre)
        self.canvas.bind("<Configure>", self._redim_cascade)

    def _redim_cascade(self, ev):
        """La cascade suit la largeur de la fenêtre (historique remis à zéro)."""
        w = max(100, ev.width - 2)
        if w == self.largeur and self.photo:
            return
        self.largeur = w
        self.cascade = np.zeros((H_CASCADE, w, 3), np.uint8)
        self.canvas.delete("grad")
        for f in range(500, int(F_MAX) + 1, 500):      # graduations tous les 500 Hz
            x = self._f2x(f)
            self.canvas.create_line(x, H_CASCADE - 6, x, H_CASCADE + 2, fill=C_GRIS, tags="grad")
            self.canvas.create_text(x + 3, H_CASCADE, anchor="sw", text=str(f), fill=C_GRIS,
                                    font=self.f_mono_p, tags="grad")
        self._afficher_cascade()
        self._maj_tons()

    def _construire_reglages(self, parent):
        """Ligne des réglages du décodeur : pastilles, rafale, squelch, netteté."""
        ligne = tk.Frame(parent, bg=C_FOND)
        ligne.pack(fill="x", pady=(6, 0))
        self._bouton(ligne, "Auto", self._auto).pack(side="left", padx=(0, 6))
        self.v_afc, self.v_atc = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.v_uos, self.v_rev = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        for texte, var in (("AFC", self.v_afc), ("ATC", self.v_atc), ("UOS", self.v_uos),
                           ("Inverser", self.v_rev)):
            self._puce(ligne, texte, var).pack(side="left", padx=2)
        # Rafale : nb de caractères consécutifs exigés pour afficher (1 = filtre coupé) ;
        # un clic fait défiler 1 -> 5
        self.v_rafale = tk.IntVar(value=3)
        l_raf = tk.Label(ligne, text="Rafale 3", font=self.f_mono_p, padx=8, pady=1,
                         cursor="hand2", bg=C_FOND, fg=C_AMBRE, highlightthickness=1,
                         highlightbackground=C_BORD)
        l_raf.pack(side="left", padx=2)
        l_raf.bind("<Button-1>", lambda ev: self.v_rafale.set(self.v_rafale.get() % 5 + 1))
        self.v_rafale.trace_add("write", lambda *_: l_raf.config(
            text=f"Rafale {self.v_rafale.get()}"))
        self._etiquette(ligne, "squelch", police=self.f_mono_p).pack(side="left", padx=(10, 2))
        self.v_sql = tk.DoubleVar(value=0.40)
        tk.Scale(ligne, from_=0, to=0.8, resolution=0.01, orient="horizontal",
                 variable=self.v_sql, showvalue=0, length=70, width=8, sliderlength=12,
                 bg=C_AMBRE, troughcolor=C_PANNEAU, activebackground=C_AMBRE, bd=0,
                 highlightthickness=0, sliderrelief="flat").pack(side="left")
        self.l_sql = self._etiquette(ligne, "0.40", C_TEXTE, self.f_mono_p, width=5)
        self.l_sql.pack(side="left")

    def _construire_droite(self, parent):
        """Colonne de droite : carte du QSO en cours, infos, mode, outils."""
        self.v_dx, self.v_recu = tk.StringVar(), tk.StringVar()
        self.v_ech = tk.StringVar(value=self.cfg["echange"])
        self.v_rst = tk.StringVar(value=self.cfg["rst"])
        self.v_mode = tk.StringVar(value=self.cfg.get("mode", "contest"))

        carte = self._carte(parent)
        carte.pack(fill="x")
        dedans = tk.Frame(carte, bg=C_PANNEAU)
        dedans.pack(fill="x", padx=10, pady=8)
        self._etiquette(dedans, "QSO en cours", police=self.f_sans_p, fond=C_PANNEAU
                        ).pack(anchor="w")
        # Le grand indicatif EST la case « Correspondant » : on tape directement dedans
        self.e_dx = tk.Entry(dedans, textvariable=self.v_dx, width=11, font=self.f_indic,
                             bg=C_PANNEAU, fg=C_TEXTE, insertbackground=C_AMBRE, relief="flat",
                             highlightthickness=0, selectbackground=C_AMBRE_SOMBRE)
        self.e_dx.pack(anchor="w", fill="x")
        tk.Frame(dedans, bg=C_BORD, height=1).pack(fill="x", pady=(0, 4))
        self.l_pays = self._etiquette(dedans, "", C_CYAN, fond=C_PANNEAU, anchor="w",
                                      justify="left", wraplength=200)
        self.l_pays.pack(fill="x")
        self.l_zone = self._etiquette(dedans, "", fond=C_PANNEAU, police=self.f_mono_p,
                                      anchor="w")
        self.l_zone.pack(fill="x")
        badges = tk.Frame(dedans, bg=C_PANNEAU)
        badges.pack(fill="x", pady=(4, 0))
        self.l_doublon = tk.Label(badges, text="", bg=C_PANNEAU, font=self.f_mono_p)
        self.l_doublon.pack(side="left")
        self.l_mult = tk.Label(badges, text="", bg=C_PANNEAU, font=self.f_mono_p)
        self.l_mult.pack(side="left", padx=(4, 0))
        self.v_dx.trace_add("write", lambda *_: self._maj_doublon())

        carte = self._carte(parent)
        carte.pack(fill="x", pady=(8, 0))
        g = tk.Frame(carte, bg=C_PANNEAU)
        g.pack(fill="x", padx=10, pady=8)
        self._etiquette(g, "reçu", fond=C_PANNEAU).grid(row=0, column=0, sticky="w")
        self._saisie(g, self.v_recu, 12).grid(row=0, column=1, sticky="we", pady=2)
        self.l_envoye = self._etiquette(g, "envoyé", fond=C_PANNEAU)
        self.l_envoye.grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.e_envoye = self._saisie(g, None, 12)
        self.e_envoye.grid(row=1, column=1, sticky="we", pady=2)
        self._etiquette(g, "journal", fond=C_PANNEAU).grid(row=2, column=0, sticky="w")
        self.l_journal = self._etiquette(g, "", C_AMBRE, self.f_mono_p, fond=C_PANNEAU,
                                         anchor="w")
        self.l_journal.grid(row=2, column=1, sticky="we", pady=2)
        self._etiquette(g, "QSO", fond=C_PANNEAU).grid(row=3, column=0, sticky="w")
        self.l_nb_qso = self._etiquette(g, "0", C_TEXTE, self.f_mono, fond=C_PANNEAU,
                                        anchor="w")
        self.l_nb_qso.grid(row=3, column=1, sticky="we")
        g.columnconfigure(1, weight=1)
        self._bouton(g, "Log  (Ctrl+L)", self._loguer, fond=C_AMBRE_SOMBRE, fg=C_AMBRE
                     ).grid(row=4, column=0, columnspan=2, sticky="we", pady=(6, 0))

        # Choix du jeu de macros : deux pastilles exclusives
        modes = tk.Frame(parent, bg=C_FOND)
        modes.pack(fill="x", pady=(8, 0))
        self.l_modes = {}
        for texte, val in (("Contest", "contest"), ("QSO", "qso")):
            lab = tk.Label(modes, text=texte, font=self.f_sans, pady=3, cursor="hand2",
                           highlightthickness=1)
            lab.pack(side="left", fill="x", expand=True, padx=(0 if val == "contest" else 4, 0))
            lab.bind("<Button-1>", lambda ev, v=val: (self.v_mode.set(v), self._changer_mode()))
            self.l_modes[val] = lab

        outils = tk.Frame(parent, bg=C_FOND)
        outils.pack(fill="x", pady=(8, 0))
        for i, (texte, cmd) in enumerate((("Journal…", self._choisir_journal),
                                          ("Cabrillo…", self._exporter_cabrillo),
                                          ("CTY…", self._choisir_cty),
                                          ("Station…", self._reglages_station),
                                          ("Effacer", lambda: self.texte.delete("1.0", "end")))):
            self._bouton(outils, texte, cmd).grid(row=i // 2, column=i % 2, sticky="we",
                                                  columnspan=2 if i == 4 else 1,
                                                  padx=(0 if i % 2 == 0 else 4, 0), pady=2)
        outils.columnconfigure((0, 1), weight=1, uniform="o")

    def _construire_bas(self):
        """Barre d'état (+ connexion), ligne d'émission libre, tuiles de macros."""
        etat = tk.Frame(self.r, bg=C_FOND)
        etat.pack(fill="x", side="bottom", padx=10, pady=(4, 6))
        self.b_connexion = self._bouton(etat, "Connecter", self._basculer_connexion)
        self.b_connexion.pack(side="right")
        self.v_port = tk.StringVar(value=str(self.args.port))
        self._saisie(etat, self.v_port, 6, self.f_mono_p).pack(side="right", padx=4)
        self.v_host = tk.StringVar(value=self.args.host)
        self._saisie(etat, self.v_host, 14, self.f_mono_p).pack(side="right")
        self._etiquette(etat, "TCI", police=self.f_mono_p).pack(side="right", padx=4)
        # Netteté moyenne des derniers caractères décodés, en barre
        self.barre = tk.Canvas(etat, width=60, height=8, bg=C_PANNEAU, highlightthickness=0)
        self.barre.pack(side="right", padx=(4, 12))
        self.barre_val = self.barre.create_rectangle(0, 0, 0, 8, fill=C_AMBRE, width=0)
        self._etiquette(etat, "netteté", police=self.f_mono_p).pack(side="right")
        # Le texte d'état est placé en dernier : il prend la place qui reste sans
        # jamais repousser la connexion ni la netteté hors de la fenêtre
        self.v_etat = tk.StringVar(value="Déconnecté")
        tk.Label(etat, textvariable=self.v_etat, bg=C_FOND, fg=C_GRIS, font=self.f_mono_p,
                 anchor="w").pack(side="left", fill="x", expand=True)

        ligne = tk.Frame(self.r, bg=C_FOND)
        ligne.pack(fill="x", side="bottom", padx=10, pady=(6, 0))
        tk.Button(ligne, text="STOP · Échap", command=self._stop_tx, bg=C_ROUGE, fg="white",
                  activebackground=C_ROUGE_CLAIR, relief="flat", bd=0, font=self.f_badge,
                  padx=14, pady=3, cursor="hand2").pack(side="right")
        self.b_envoyer = self._bouton(ligne, "Envoyer", self._envoyer_libre)
        self.b_envoyer.pack(side="right", padx=6)
        self.v_libre = tk.StringVar()
        e = self._saisie(ligne, self.v_libre, 20)
        e.pack(side="left", fill="x", expand=True, ipady=3)
        e.bind("<Return>", lambda ev: self._envoyer_libre())

        # Tuiles de macros (reconstruites à chaque changement de mode ou d'édition)
        self.cadre_macros = tk.Frame(self.r, bg=C_FOND)
        self.cadre_macros.pack(fill="x", side="bottom", padx=10, pady=(8, 0))
        self.boutons_macros = []

        # Raccourcis : F1-F12 selon le jeu actif ; « break » empêche Tk d'utiliser F10
        # pour ouvrir un menu
        for n in range(1, 13):
            self.r.bind(f"<F{n}>", lambda ev, n=n: self._touche(n))
        self.r.bind("<Escape>", lambda ev: self._stop_tx())
        self.r.bind("<Control-l>", lambda ev: self._loguer())

    # --- macros ----------------------------------------------------------------------
    def _macros(self):
        return self.cfg["macros"][self.v_mode.get()]

    def _changer_mode(self):
        contest = self.v_mode.get() == "contest"
        self.e_envoye.config(textvariable=self.v_ech if contest else self.v_rst)
        self.l_envoye.config(text="envoyé" if contest else "RST")
        for val, lab in self.l_modes.items():
            actif = val == self.v_mode.get()
            lab.config(bg=C_AMBRE if actif else C_FOND, fg=C_FOND if actif else C_GRIS,
                       highlightbackground=C_AMBRE if actif else C_BORD)
        self._dessiner_macros()
        self._maj_doublon()

    def _dessiner_macros(self):
        """Tuiles de macros : une ligne par groupe (RUN, S&P), la touche Fn en ambre."""
        for w in self.cadre_macros.winfo_children():
            w.destroy()
        self.boutons_macros = []
        groupes = []
        for i, m in enumerate(self._macros()):
            if not groupes or groupes[-1][0] != m["groupe"]:
                groupes.append((m["groupe"], []))
            groupes[-1][1].append(i)
        nb_col = max(len(idx) for _, idx in groupes)
        for ligne, (nom, indices) in enumerate(groupes):
            self._etiquette(self.cadre_macros, nom, police=self.f_mono_p, width=4, anchor="w"
                            ).grid(row=ligne, column=0, sticky="w")
            for col, i in enumerate(indices):
                m = self._macros()[i]
                tuile = tk.Frame(self.cadre_macros, bg=C_PANNEAU, highlightthickness=1,
                                 highlightbackground=C_BORD, cursor="hand2")
                tuile.grid(row=ligne, column=col + 1, sticky="we", padx=2, pady=2)
                t = tk.Label(tuile, text=f"F{i + 1}" if i < 12 else "", bg=C_PANNEAU,
                             fg=C_AMBRE, font=self.f_mono_p, anchor="w")
                t.pack(fill="x", padx=6, pady=(3, 0))
                n = tk.Label(tuile, text=m["nom"], bg=C_PANNEAU, fg=C_TEXTE, font=self.f_sans,
                             anchor="w")
                n.pack(fill="x", padx=6, pady=(0, 3))
                for w in (tuile, t, n):
                    w.bind("<Button-1>", lambda ev, i=i: self._executer(i))
                    w.bind("<Button-3>", lambda ev, i=i: self._editer_macro(i))
                    w.bind("<Enter>", lambda ev, f=tuile: f.config(highlightbackground=C_AMBRE))
                    w.bind("<Leave>", lambda ev, f=tuile: f.config(highlightbackground=C_BORD))
                self.boutons_macros.append(n)
        for col in range(1, 13):
            self.cadre_macros.columnconfigure(col, weight=1 if col <= nb_col else 0,
                                              uniform="tuiles" if col <= nb_col else "")
        self._etat_boutons()

    def _touche(self, n):
        if n <= len(self._macros()):
            self._executer(n - 1)
        return "break"

    def _substituer(self, modele):
        v = {"{MOI}": self.cfg["indicatif"], "{DX}": self.v_dx.get(), "{ECH}": self.v_ech.get(),
             "{RST}": self.v_rst.get(), "{NOM}": self.cfg["nom"], "{QTH}": self.cfg["qth"]}
        for cle, val in v.items():
            modele = modele.replace(cle, val.strip().upper())
        return modele

    def _executer(self, i):
        modele = self._macros()[i]["texte"]
        loguer = "{LOG}" in modele
        texte = self._substituer(modele.replace("{LOG}", ""))
        if texte.strip():
            if "{DX}" in modele and not self.v_dx.get().strip():
                self.etat = "Correspondant vide : macro non émise"
                return
            if not self._emettre(texte):
                return
        if loguer:
            self._loguer()

    def _editer_macro(self, i):
        """Petite fenêtre d'édition d'une macro (clic droit sur son bouton)."""
        m = self._macros()[i]
        f = tk.Toplevel(self.r)
        f.title(f"Macro F{i + 1} — {self.v_mode.get()} {m['groupe']}")
        f.transient(self.r)
        f.configure(bg=C_FOND)
        ttk.Label(f, text="Nom du bouton").pack(anchor="w", padx=6, pady=(6, 0))
        v_nom = tk.StringVar(value=m["nom"])
        ttk.Entry(f, textvariable=v_nom, width=20).pack(anchor="w", padx=6)
        ttk.Label(f, text="Texte (un retour à la ligne = CR LF émis)").pack(anchor="w", padx=6,
                                                                         pady=(6, 0))
        zone = tk.Text(f, width=60, height=4, font=self.f_mono, bg=C_PANNEAU, fg=C_TEXTE,
                       insertbackground=C_AMBRE, relief="flat", highlightthickness=1,
                       highlightbackground=C_BORD)
        zone.pack(fill="both", expand=True, padx=6)
        zone.insert("1.0", m["texte"])
        ttk.Label(f, text="{MOI} {DX} {ECH} {RST} {NOM} {QTH}   {LOG} = enregistrer le QSO",
                  foreground=C_GRIS).pack(anchor="w", padx=6)

        def valider():
            m["nom"] = v_nom.get().strip() or m["nom"]
            m["texte"] = zone.get("1.0", "end-1c")
            sauver_config(self._cfg_a_jour())
            self._dessiner_macros()
            f.destroy()

        bas = ttk.Frame(f)
        bas.pack(fill="x", pady=6)
        ttk.Button(bas, text="Annuler", command=f.destroy).pack(side="right", padx=6)
        ttk.Button(bas, text="Enregistrer", command=valider).pack(side="right")
        f.grab_set()

    def _reglages_station(self, ensuite=None):
        """
        Indicatif, prénom, QTH et échange de contest (variables {MOI}, {NOM}, {QTH}, {ECH}).
        Ouverte d'office au premier lancement : sans elle, les macros émettraient « N0CALL ».
        """
        f = tk.Toplevel(self.r)
        f.title("Station")
        f.transient(self.r)
        f.configure(bg=C_FOND)
        champs = [("indicatif", "Indicatif", "ON4XYZ"), ("nom", "Prénom", "JEAN"),
                  ("qth", "QTH", "NAMUR"), ("echange", "Échange contest", "599 14 14")]
        vars_ = {}
        for ligne, (cle, lib, exemple) in enumerate(champs):
            ttk.Label(f, text=lib).grid(row=ligne, column=0, sticky="w", padx=8, pady=3)
            valeur = self.v_ech.get() if cle == "echange" else self.cfg.get(cle, "")
            v = vars_[cle] = tk.StringVar(value="" if valeur == "N0CALL" else valeur)
            ttk.Entry(f, textvariable=v, width=24).grid(row=ligne, column=1, padx=8, pady=3)
            ttk.Label(f, text=f"ex. {exemple}", foreground=C_GRIS).grid(row=ligne, column=2,
                                                                       sticky="w", padx=(0, 8))
        ttk.Label(f, text="CQ WW RTTY : RST + zone CQ (+ État/province pour W/VE)",
                  foreground=C_GRIS).grid(row=len(champs), column=0, columnspan=3,
                                          sticky="w", padx=8, pady=(4, 0))
        l_err = ttk.Label(f, text="", foreground=C_ROUGE_CLAIR)
        l_err.grid(row=len(champs) + 1, column=0, columnspan=3, sticky="w", padx=8)

        def valider():
            indic = vars_["indicatif"].get().strip().upper()
            if not RE_INDICATIF.fullmatch(indic):
                l_err.config(text="Indique un indicatif valide.")
                return
            self.cfg.update(indicatif=indic, nom=vars_["nom"].get().strip().upper(),
                            qth=vars_["qth"].get().strip().upper())
            self.v_ech.set(vars_["echange"].get().strip().upper())
            sauver_config(self._cfg_a_jour())
            self.l_moi.config(text=indic)
            self.r.title(f"Emile-RTTY — {indic}")
            f.destroy()
            if ensuite:
                ensuite()

        bas = ttk.Frame(f)
        bas.grid(row=len(champs) + 2, column=0, columnspan=3, sticky="e", pady=8, padx=8)
        ttk.Button(bas, text="Enregistrer", command=valider).pack(side="right")
        ttk.Button(bas, text="Annuler", command=f.destroy).pack(side="right", padx=6)
        f.grab_set()

    def _cfg_a_jour(self):
        self.cfg.update(mode=self.v_mode.get(), echange=self.v_ech.get().strip().upper(),
                        rst=self.v_rst.get().strip().upper(),
                        journal=str(self.journal.chemin))
        return self.cfg

    def _quitter(self):
        sauver_config(self._cfg_a_jour())
        if self.client:
            # Le thread réseau (daemon) doit avoir le temps d'envoyer « trx:0,false; »,
            # sinon une fermeture pendant l'émission pourrait laisser l'émetteur en TX
            self.client.arreter_tx()
            self.client.stop_evt.set()
            self.client.join(1.0)
        self.r.destroy()

    # --- journal ---------------------------------------------------------------------
    def _choisir_journal(self):
        """
        Choix du fichier ADIF : un existant (les QSO sont ajoutés à la suite et servent
        aux doublons) ou un nouveau nom (fichier créé au premier QSO).
        Annuler = on garde le journal en cours.
        """
        actuel = self.journal.chemin
        chemin = filedialog.asksaveasfilename(
            parent=self.r, title="Journal ADIF à utiliser (existant ou nouveau)",
            initialdir=str(actuel.parent if actuel.parent.exists() else Path.home()),
            initialfile=actuel.name, defaultextension=".adi", confirmoverwrite=False,
            filetypes=[("ADIF", "*.adi *.adif"), ("Tous les fichiers", "*")])
        if not chemin:
            return
        self.journal = Journal(chemin)
        self.cfg["journal"] = str(self.journal.chemin)
        sauver_config(self._cfg_a_jour())
        self._maj_titre_journal()
        self._recalculer_mults()
        self._maj_doublon()
        n = len(self.journal.lire())
        self.etat = f"Journal {self.journal.chemin.name} : {n} QSO" if n else \
            f"Nouveau journal {self.journal.chemin.name}"

    def _maj_titre_journal(self):
        """Nom du journal et nombre de QSO dans la carte d'infos."""
        nom = self.journal.chemin.name
        self.l_journal.config(text=nom if len(nom) <= 20 else nom[:9] + "…" + nom[-10:])
        self.l_nb_qso.config(text=str(len(self.journal.lire())))

    def _exporter_cabrillo(self):
        """Fenêtre d'en-tête Cabrillo, puis écriture du .log à partir du journal ouvert."""
        qsos = self.journal.lire()
        if not qsos:
            messagebox.showinfo("Cabrillo", "Le journal ouvert ne contient aucun QSO.",
                                parent=self.r)
            return
        cab = self.cfg["cabrillo"]
        f = tk.Toplevel(self.r)
        f.title(f"Cabrillo — {len(qsos)} QSO de {self.journal.chemin.name}")
        f.transient(self.r)
        f.configure(bg=C_FOND)
        # (clé, libellé, valeurs proposées ; None = saisie libre)
        champs = [
            ("CONTEST", "Concours", ["CQ-WW-RTTY", "CQ-WPX-RTTY", "ARRL-RTTY", "BARTG-RTTY",
                                     "SARTG-RTTY", "EA-RTTY"]),
            ("CATEGORY-OPERATOR", "Opérateur", ["SINGLE-OP", "MULTI-OP", "CHECKLOG"]),
            ("CATEGORY-BAND", "Bande", ["ALL", "80M", "40M", "20M", "15M", "10M"]),
            ("CATEGORY-POWER", "Puissance", ["HIGH", "LOW", "QRP"]),
            ("CATEGORY-ASSISTED", "Assistance", ["NON-ASSISTED", "ASSISTED"]),
            ("ECHANGE", "Échange envoyé", None),
            ("NAME", "Nom", None), ("ADDRESS", "Adresse", None), ("EMAIL", "E-mail", None),
            ("CLUB", "Club", None), ("SOAPBOX", "Commentaire", None),
        ]
        vars_ = {}
        for ligne, (cle, lib, valeurs) in enumerate(champs):
            ttk.Label(f, text=lib).grid(row=ligne, column=0, sticky="w", padx=6, pady=2)
            v = vars_[cle] = tk.StringVar(value=cab.get(cle, ""))
            w = (ttk.Combobox(f, textvariable=v, values=valeurs, width=34) if valeurs
                 else ttk.Entry(f, textvariable=v, width=36))
            w.grid(row=ligne, column=1, sticky="we", padx=6, pady=2)
        ttk.Label(f, text="Échange CQ WW RTTY hors W/VE : « 599 zone DX »",
                  foreground=C_GRIS).grid(row=len(champs), column=0, columnspan=2,
                                             sticky="w", padx=6)

        def ecrire():
            for cle, v in vars_.items():
                valeur = v.get().strip()
                cab[cle] = valeur if cle in ("NAME", "ADDRESS", "EMAIL", "SOAPBOX", "CLUB") \
                    else valeur.upper()
            sauver_config(self._cfg_a_jour())
            chemin = filedialog.asksaveasfilename(
                parent=f, title="Enregistrer le log Cabrillo",
                initialdir=str(self.journal.chemin.parent),
                initialfile=f"{self.cfg['indicatif'].replace('/', '_')}.log",
                defaultextension=".log", filetypes=[("Cabrillo", "*.log *.cbr")])
            if not chemin:
                return
            try:
                incomplets = ecrire_cabrillo(chemin, qsos, cab, self.cfg["indicatif"])
            except OSError as e:
                messagebox.showerror("Cabrillo", f"Écriture impossible : {e}", parent=f)
                return
            msg = f"{len(qsos)} QSO écrits dans\n{chemin}"
            if incomplets:
                msg += (f"\n\n⚠ {incomplets} QSO sans échange reçu : "
                        "complète-les dans le fichier avant l'envoi.")
            messagebox.showinfo("Cabrillo", msg, parent=f)
            f.destroy()

        bas = ttk.Frame(f)
        bas.grid(row=len(champs) + 1, column=0, columnspan=2, sticky="e", pady=6)
        ttk.Button(bas, text="Écrire le .log…", command=ecrire).pack(side="right", padx=6)
        ttk.Button(bas, text="Annuler", command=f.destroy).pack(side="right")
        f.grab_set()

    def _choisir_cty(self):
        chemin = filedialog.askopenfilename(
            parent=self.r, title="Fichier pays cty.dat (AD1C)",
            initialdir=str(self.cty.chemin.parent if self.cty else Path.home()),
            filetypes=[("Fichier pays", "*.dat *.DAT"), ("Tous les fichiers", "*")])
        if not chemin:
            return
        try:
            self.cty = CTY(chemin)
        except (OSError, ValueError) as e:
            messagebox.showerror("cty.dat", f"Fichier refusé : {e}", parent=self.r)
            return
        self.cfg["cty"] = chemin
        sauver_config(self._cfg_a_jour())
        self._recalculer_mults()
        self._maj_doublon()
        self.etat = f"cty.dat {self.cty.version} chargé"

    def _zone(self, q, info):
        """Zone CQ d'un QSO : celle reçue si elle a été notée, sinon celle du cty.dat."""
        mots = q["SRX_STRING"].split()
        if mots and mots[0].isdigit():
            return int(mots[0])
        return info["cq"] if info else None

    def _recalculer_mults(self):
        """Pays et zones déjà contactés par bande, d'après le journal ouvert."""
        self.mults = set()
        if not self.cty:
            return
        for q in self.journal.lire():
            info = self.cty.chercher(q["CALL"])
            if info:
                self.mults.add((q["BAND"], "P", info["pfx"]))
            z = self._zone(q, info)
            if z:
                self.mults.add((q["BAND"], "Z", z))

    def _maj_doublon(self):
        """À chaque frappe dans « Correspondant » : doublon, pays, multiplicateur, zone."""
        call = self.v_dx.get().strip().upper()
        vide = {"text": "", "bg": C_PANNEAU, "padx": 0}
        if call and self.journal.doublon(call, self.vfo_hz):
            self.l_doublon.config(text="DOUBLON", bg=C_ROUGE_SOMBRE, fg=C_ROUGE_CLAIR, padx=6)
        else:
            self.l_doublon.config(**vide)
        info = self.cty.chercher(call) if (self.cty and len(call) >= 2) else None
        if not info:
            self.l_pays.config(text="" if self.cty or not call else "(pas de cty.dat)")
            self.l_zone.config(text="")
            self.l_mult.config(**vide)
            return
        self.l_pays.config(text=f"{info['pays']} ({info['pfx'].lstrip('*')})")
        self.l_zone.config(text=f"zone CQ {info['cq']} · {info['cont']}")
        if self.v_mode.get() != "contest":
            self.l_mult.config(**vide)
            return
        # Nouveaux multiplicateurs sur la bande en cours (pays et/ou zone)
        bande = self.journal.bande(self.vfo_hz)
        nouveaux = []
        if (bande, "P", info["pfx"]) not in self.mults:
            nouveaux.append("PAYS")
        if (bande, "Z", info["cq"]) not in self.mults:
            nouveaux.append("ZONE")
        if nouveaux and not self.journal.doublon(call, self.vfo_hz):
            self.l_mult.config(text=f"NOUV. {'+'.join(nouveaux)}", bg=C_AMBRE_SOMBRE,
                               fg=C_AMBRE, padx=6)
        else:
            self.l_mult.config(**vide)
        # Zone reçue préremplie, tant que l'opérateur n'y a pas touché
        if self.v_recu.get() in ("", self.recu_auto):
            self.recu_auto = f"599 {info['cq']:02d}"
            self.v_recu.set(self.recu_auto)

    def _loguer(self):
        call = self.v_dx.get().strip().upper()
        if not call:
            self.etat = "Pas d'indicatif à enregistrer"
            return
        maintenant = datetime.datetime.now(datetime.timezone.utc)
        if self.v_mode.get() == "contest":
            rst_env, stx = separer_echange(self.v_ech.get())
        else:
            rst_env, stx = self.v_rst.get().strip().upper(), ""
        rst_rec, srx = separer_echange(self.v_recu.get())
        doublon = self.journal.doublon(call, self.vfo_hz)
        champs = {
            "CALL": call,
            "QSO_DATE": maintenant.strftime("%Y%m%d"),
            "TIME_ON": maintenant.strftime("%H%M%S"),
            "BAND": self.journal.bande(self.vfo_hz),
            "FREQ": f"{self.vfo_hz / 1e6:.6f}" if self.vfo_hz else "",
            "MODE": "RTTY",
            "RST_SENT": rst_env or "599",
            "RST_RCVD": rst_rec or "599",
            "STX_STRING": stx,
            "SRX_STRING": srx,
            "STATION_CALLSIGN": self.cfg["indicatif"],
        }
        info = self.cty.chercher(call) if self.cty else None
        if info:
            champs["COUNTRY"] = ascii_seul(info["pays"])
            champs["CONT"] = info["cont"]
        zone = self._zone({"SRX_STRING": srx}, info) if (srx or info) else None
        if zone:
            champs["CQZ"] = str(zone)
        try:
            self.journal.ajouter(champs)
        except OSError as e:
            self.etat = f"Journal : écriture impossible ({e})"
            return
        # Trace dans la fenêtre, puis on vide les cases pour le QSO suivant
        if self.texte.get("end-2c") not in ("\n", ""):
            self.texte.insert("end", "\n")
        ligne = f"[LOG {maintenant:%H:%M}z] {call} {champs['BAND']} " \
                f"env {rst_env} {stx} / reçu {rst_rec} {srx}"
        self.texte.insert("end", " ".join(ligne.split()) + (" (doublon)" if doublon else "")
                          + "\n", "log")
        self.texte.see("end")
        self.etat = f"{call} enregistré dans {self.journal.chemin.name}"
        self.l_nb_qso.config(text=str(int(self.l_nb_qso.cget("text") or 0) + 1))
        if info:
            self.mults.add((champs["BAND"], "P", info["pfx"]))
        if zone:
            self.mults.add((champs["BAND"], "Z", zone))
        self.v_recu.set("")
        self.recu_auto = ""
        self.v_dx.set("")
        self.e_dx.focus_set()

    def _double_clic_texte(self, ev):
        """Double-clic sur un indicatif reçu : il devient le correspondant."""
        idx = self.texte.index(f"@{ev.x},{ev.y}")
        ligne, col = map(int, idx.split("."))
        txt = self.texte.get(f"{ligne}.0", f"{ligne}.end").upper()
        debut = fin = col
        while debut > 0 and (txt[debut - 1].isalnum() or txt[debut - 1] == "/"):
            debut -= 1
        while fin < len(txt) and (txt[fin].isalnum() or txt[fin] == "/"):
            fin += 1
        mot = txt[debut:fin].strip("/")
        if RE_INDICATIF.fullmatch(mot) and 3 <= len(mot) <= 12:
            if mot != self.v_dx.get().strip().upper():
                self.v_recu.set("")
            self.v_dx.set(mot)
        return "break"

    def _envoyer_libre(self):
        texte = self.v_libre.get()
        if texte.strip():
            if self._emettre(" " + texte + " "):
                self.v_libre.set("")

    def _emettre(self, texte):
        """Lance l'émission du texte ; retourne True si elle a bien démarré."""
        if self.en_tx:
            return False
        if not self.client:
            self.etat = "Pas connecté : émission impossible"
            return False
        if self.cfg["indicatif"] in ("", "N0CALL"):
            self.etat = "Indicatif non réglé (bouton Station…) : émission refusée"
            return False
        d = self.demod
        # On émet là où l'on écoute : même centre (AFC compris) et même polarité qu'en RX
        f_mark, f_space = (d.f_haut, d.f_bas) if self.v_rev.get() else (d.f_bas, d.f_haut)
        audio = self.generateur.audio(self.generateur.coder(texte), f_mark, f_space)
        if len(audio) / SR > TX_MAX:
            self.etat = f"Texte trop long (> {TX_MAX:.0f} s d'émission)"
            return False
        self.tx_limite = time.monotonic() + len(audio) / SR + 5
        self.client.emettre(audio)
        # En TX dès la demande (sans attendre la confirmation du thread réseau) :
        # un second appui rapide sur une macro ne doit pas l'afficher deux fois
        self._etat_tx(True)
        # Écho du texte émis dans la fenêtre, en rouge
        if self.texte.get("end-2c") not in ("\n", ""):
            self.texte.insert("end", "\n")
        self.texte.insert("end", texte.strip("\n") + "\n", "tx")
        self.texte.see("end")
        return True

    def _stop_tx(self):
        if self.client:
            self.client.arreter_tx()

    def _etat_tx(self, actif):
        if self.en_tx and not actif:
            self.fin_tx = time.monotonic()
        self.en_tx = actif
        self.afc_prec = None
        self.l_tx.config(text=" TX " if actif else " RX ", bg=C_ROUGE if actif else C_AMBRE,
                         fg="white" if actif else C_FOND)
        self._etat_boutons()

    def _etat_boutons(self):
        # Pendant le TX, les tuiles et « Envoyer » passent en gris (STOP reste actif)
        for b in self.boutons_macros:
            b.config(fg=C_GRIS if self.en_tx else C_TEXTE)
        self.b_envoyer.config(state="disabled" if self.en_tx else "normal")

    # --- conversions fréquence <-> abscisse ------------------------------------------
    def _f2x(self, f):
        return 1 + (f - F_MIN) / (F_MAX - F_MIN) * self.largeur

    def _x2f(self, x):
        return F_MIN + (x - 1) / self.largeur * (F_MAX - F_MIN)

    # --- actions ----------------------------------------------------------------------
    def _basculer_connexion(self):
        if self.client:
            self.client.stop_evt.set()
            self.b_connexion.config(state="disabled")
            return
        try:
            port = int(self.v_port.get())
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            self.etat = "Port TCI invalide"
            return
        self.client = ClientTCI(self.v_host.get().strip(), port, 0, self.q)
        self.client.start()
        self.b_connexion.config(text="Déconnecter")

    def _regler(self, centre):
        """Nouvelle consigne choisie par l'opérateur : l'AFC travaillera autour d'elle."""
        centre = float(np.clip(centre, F_MIN + SHIFT / 2, F_MAX - SHIFT / 2))
        self.consigne = centre
        self.demod.regler(centre)
        self._maj_tons()

    def _suivre_afc(self):
        """Appelée toutes les 250 ms : recalage sur la paire la plus forte près de la consigne."""
        if not self.v_afc.get():
            if self.demod.centre != self.consigne:     # AFC coupé : retour à la consigne
                self.demod.regler(self.consigne, reinit=False)
            return
        mesure = self.afc.estimer(self.historique, self.consigne)
        precedente, self.afc_prec = self.afc_prec, mesure
        if mesure is None:
            return
        ecart = abs(mesure - self.demod.centre)
        if ecart > 25:
            # Gros écart (nouvelle station) : le décodage est de toute façon perdu,
            # on se recale tout de suite pour ne pas manquer le début du texte
            self.demod.regler(mesure, reinit=False)
        elif ecart > 12 and precedente is not None and abs(mesure - precedente) <= 8:
            # Petit écart (12-25 Hz) : le décodage fonctionne encore, on exige deux mesures
            # concordantes, ce qui évite les sauts inutiles sur les signaux faibles.
            # En dessous de 12 Hz on ne bouge pas : le décodeur tolère ~20 Hz d'erreur.
            self.demod.regler((mesure + precedente) / 2, reinit=False)

    def _clic_spectre(self, ev):
        self._regler(self._x2f(ev.x))

    def _auto(self):
        c = trouver_centre(self.historique)
        if c:
            self._regler(c)

    def _maj_freq(self):
        """En-tête : « 14.084 » + « .710 » en ambre, puis unité, mode et bande."""
        if self.vfo_hz:
            f = f"{self.vfo_hz / 1e6:.6f}"
            self.l_freq.config(text=f[:-3])
            self.l_freq_fin.config(text="." + f[-3:])
        bande = self.journal.bande(self.vfo_hz) if self.vfo_hz else ""
        self.l_bande.config(text=" · ".join(["MHz"] + [m for m in (self.mode, bande) if m]))

    def _maj_tons(self):
        """Marqueurs mark/space sur la cascade, zone AFC, centre et écart AFC."""
        d = self.demod
        ecart = d.centre - self.consigne
        afc = f"  AFC {ecart:+.0f}" if abs(ecart) >= 1 else ""
        self.tons = f"centre {d.centre:.0f} Hz{afc}"
        h = H_CASCADE
        if self.v_afc.get():
            g = self.consigne - self.afc.fenetre - SHIFT / 2
            dr = self.consigne + self.afc.fenetre + SHIFT / 2
            self.canvas.coords(self.zone_afc, self._f2x(g), 2, self._f2x(dr), h - 12)
        else:
            self.canvas.coords(self.zone_afc, 0, 0, 0, 0)
        xb, xh = self._f2x(d.f_bas), self._f2x(d.f_haut)
        self.canvas.coords(self.m_bas, xb, 0, xb, h + 2)
        self.canvas.coords(self.m_haut, xh, 0, xh, h + 2)
        # Le trait ambre est toujours le mark, le cyan le space
        inv = self.v_rev.get()
        c_bas, c_haut = (C_CYAN, C_AMBRE) if inv else (C_AMBRE, C_CYAN)
        n_bas, n_haut = ("S", "M") if inv else ("M", "S")
        self.canvas.itemconfig(self.m_bas, fill=c_bas)
        self.canvas.itemconfig(self.m_haut, fill=c_haut)
        self.canvas.itemconfig(self.t_bas, text=f"{n_bas} {d.f_bas:.0f} ", fill=c_bas)
        self.canvas.itemconfig(self.t_haut, text=f" {n_haut} {d.f_haut:.0f}", fill=c_haut)
        self.canvas.coords(self.t_bas, xb - 2, 4)
        self.canvas.coords(self.t_haut, xh + 2, 4)

    # --- boucle de traitement ----------------------------------------------------------
    def _scruter(self):
        # Relance garantie : une exception ne doit pas arrêter la réception
        # ni la coupure de sécurité de l'émission
        try:
            self._scruter_etape()
        finally:
            self.r.after(50, self._scruter)

    def _scruter_etape(self):
        # Sécurité : coupure si l'émission dure anormalement longtemps
        if self.en_tx and time.monotonic() > self.tx_limite:
            self._stop_tx()
        self.uart.sql = self.v_sql.get()
        self.uart.uos = self.v_uos.get()
        blocs = []
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "audio":
                    blocs.append(m[1])
                elif m[0] == "etat":
                    self.etat = m[1]
                elif m[0] == "vfo":
                    self.vfo_hz = m[1]
                    self._maj_freq()
                    self._maj_doublon()     # le doublon dépend de la bande
                elif m[0] == "mode":
                    # RTTY Flex / DIGL / LSB : mark = ton bas ; DIGU / USB : mark = ton haut
                    self.v_rev.set(m[1] in ("digu", "usb"))
                    self._maj_tons()
                    self.mode = m[1].upper()
                    self._maj_freq()
                elif m[0] == "tx":
                    self._etat_tx(m[1])
                elif m[0] == "deconnecte":
                    self._etat_tx(False)
                    self.client = None
                    self.b_connexion.config(text="Connecter", state="normal")
                    if not self.etat.startswith("Erreur"):
                        self.etat = "Déconnecté"
                    self.mode = ""
        except queue.Empty:
            pass

        if blocs:
            x = np.concatenate(blocs)
            # Pendant l'émission (et 0,5 s après), l'audio reçu n'est pas celui d'une station :
            # il n'alimente ni l'historique (spectre, Auto) ni l'AFC
            if not self.en_tx and time.monotonic() - self.fin_tx > 0.5:
                self.historique = np.concatenate([self.historique, x])[-len(self.historique):]
                self.pas_afc += len(x)
                if self.pas_afc >= SR // 4:
                    self.pas_afc = 0
                    self._suivre_afc()
            d_simple, d_atc = self.demod.traiter(x)
            s = -1 if self.v_rev.get() else 1
            d = d_atc if self.v_atc.get() else d_simple
            self.rafale.n = self.v_rafale.get()
            chars = self.rafale.filtrer(self.uart.alimenter(s * d, s * d_simple))
            self.n_recu += len(x)
            self._afficher(chars)

    def _afficher(self, chars):
        if not chars:
            return
        for c, t in chars:
            # Plus de 1,5 s sans caractère : nouvelle émission, on passe à la ligne
            if t - self.dernier_car > 1.5 and self.texte.get("end-2c") not in ("\n", ""):
                self.texte.insert("end", "\n")
            self.dernier_car = t
            if not c:
                continue    # code de service (LTRS, FIGS, CR) : minutage seulement
            debut_ligne = self.texte.get("end-2c") in ("\n", "")
            if c == "\n" and debut_ligne:
                continue    # pas de lignes vides en série
            if c == " " and debut_ligne:
                continue    # espace de synchro envoyé avant le texte : inutile en début de ligne
            self.texte.insert("end", c)
        self.texte.see("end")

    def _rafraichir(self):
        """Toutes les 150 ms : nouvelle ligne de cascade, barre d'état, netteté, marqueurs."""
        try:
            self._rafraichir_etape()
        finally:
            self.r.after(150, self._rafraichir)

    def _rafraichir_etape(self):
        n = 8192                                        # ~0,17 s, résolution ~6 Hz
        if self.n_recu != self.n_vu and not self.en_tx and np.any(self.historique[-n:]):
            self.n_vu = self.n_recu
            x = self.historique[-n:]
            s = 20 * np.log10(np.abs(np.fft.rfft(x * np.hanning(n))) + 1e-9)
            f = np.fft.rfftfreq(n, 1 / SR)
            zone = (f >= F_MIN) & (f <= F_MAX)
            # Une valeur par colonne de pixels
            ligne = np.interp(np.arange(self.largeur) + 1, self._f2x(f[zone]), s[zone])
            # Plancher de bruit. Avec un filtre RTTY étroit (Flex : ~500 Hz), le bruit dans
            # le filtre est 60 dB au-dessus du reste : on prend alors le bruit DU filtre
            # comme référence, sinon toute la bande passante sature.
            bas = np.percentile(ligne, 30)
            dedans = ligne[ligne > bas + 15]
            if len(dedans) > 0.08 * len(ligne):
                bas = np.median(dedans)
            self.plancher = bas if self.plancher is None else 0.9 * self.plancher + 0.1 * bas
            # Échelle : du plancher de bruit à +30 dB
            v = np.clip((ligne - self.plancher - 3) / 30, 0, 1)
            self.cascade[1:] = self.cascade[:-1]        # défilement vers le bas
            self.cascade[0] = self.lut[(v * 255).astype(np.uint8)]
            self._afficher_cascade()
        morceaux = [self.etat] + ([f"mode {self.mode}"] if self.mode else []) + [self.tons]
        self.v_etat.set("  ·  ".join(morceaux))
        self.l_sql.config(text=f"{self.v_sql.get():.2f}")
        q = self.uart.qualite if self.client else 0
        self.barre.coords(self.barre_val, 0, 0, 60 * min(1.0, q), 8)
        self._maj_tons()

    def _afficher_cascade(self):
        """Image PPM construite en mémoire à partir du tableau numpy (rapide, sans PIL)."""
        h, w = self.cascade.shape[:2]
        entete = f"P6 {w} {h} 255 ".encode()
        self.photo = tk.PhotoImage(data=entete + self.cascade.tobytes(), format="PPM")
        self.canvas.itemconfig(self.img_cascade, image=self.photo)
        self.canvas.tag_lower(self.img_cascade)

def main():
    p = argparse.ArgumentParser(description="Emile-RTTY — terminal RTTY pour Linux via TCI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=50001)
    p.add_argument("--indicatif", help="mon indicatif (sinon celui de la configuration)")
    p.add_argument("--echange", help="échange de contest (sinon celui de la configuration)")
    args = p.parse_args()
    # Nom de classe fixe : permet au dock (GNOME, KDE) de ranger la fenêtre sous son lanceur
    racine = tk.Tk(className="Emile-RTTY")
    icone = Path(__file__).resolve().parent / "docs" / "emile-rtty-icone.png"
    if icone.is_file():
        racine.iconphoto(True, tk.PhotoImage(file=str(icone)))
    racine.geometry("960x820")
    racine.minsize(820, 700)
    Application(racine, args)
    racine.mainloop()


if __name__ == "__main__":
    main()
