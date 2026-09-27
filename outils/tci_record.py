#!/usr/bin/env python3
"""
tci_record.py — Enregistre le flux audio RX d'un serveur TCI dans un fichier WAV.

Étape 1 du projet RTTY TCI : vérifier que le TCI d'AetherSDR fournit bien l'audio,
et constituer des enregistrements réels pour développer le décodeur hors ligne.

Usage :
    python3 tci_record.py                      # 30 s, localhost:50001
    python3 tci_record.py --duree 120 --port 50001 --host 192.168.1.10

Dépendances : pip install websockets numpy

Fait partie d'Emile-RTTY — (C) 2026 Albert Müller, ON5AM — licence GNU GPL v3.
"""

import argparse
import asyncio
import datetime
import struct
import sys
import wave

import numpy as np
import websockets

# Taille de l'en-tête binaire TCI : 16 entiers 32 bits non signés = 64 octets
TCI_HEADER_SIZE = 64

# Codes du champ "format" de l'en-tête TCI
FORMATS = {
    0: ("<i2", 32768.0),        # int16
    2: ("<i4", 2147483648.0),   # int32
    3: ("<f4", 1.0),            # float32
}

# Codes du champ "type" de l'en-tête TCI
TYPE_RX_AUDIO = 1


def parse_args():
    p = argparse.ArgumentParser(description="Enregistrement audio TCI vers WAV")
    p.add_argument("--host", default="127.0.0.1", help="adresse du serveur TCI")
    p.add_argument("--port", type=int, default=50001, help="port du serveur TCI")
    p.add_argument("--rx", type=int, default=0, help="numéro de récepteur TCI (0 = RX1)")
    p.add_argument("--duree", type=float, default=30.0, help="durée en secondes")
    p.add_argument("--sortie", default=None, help="fichier WAV (nom auto si absent)")
    return p.parse_args()


class Enregistreur:
    def __init__(self, args):
        self.args = args
        self.blocs = []           # liste de tableaux numpy float32 mono
        self.samplerate = None    # lu dans les en-têtes des trames
        self.infos = {}           # messages texte utiles (vfo, modulation, ...)
        self.entete_affiche = False

    def traiter_texte(self, msg):
        """Mémorise les messages d'état du serveur (une trame peut en contenir plusieurs)."""
        for cmd in msg.strip().split(";"):
            cmd = cmd.strip()
            if not cmd or ":" not in cmd:
                continue
            nom, _, val = cmd.partition(":")
            nom = nom.lower()
            if nom in ("protocol", "device", "vfo", "modulation", "rx_filter_band"):
                self.infos[nom] = val

    def traiter_binaire(self, data):
        """Décode une trame audio TCI : en-tête 64 octets puis échantillons."""
        if len(data) < TCI_HEADER_SIZE:
            return
        h = struct.unpack("<16I", data[:TCI_HEADER_SIZE])
        recepteur, sr, fmt, _codec, _crc, longueur, typ, canaux = h[:8]

        # Affiche le premier en-tête reçu : utile pour diagnostiquer une implémentation TCI exotique
        if not self.entete_affiche:
            print(f"  1re trame : rx={recepteur} sr={sr} format={fmt} "
                  f"longueur={longueur} type={typ} canaux={canaux} "
                  f"octets={len(data) - TCI_HEADER_SIZE}")
            self.entete_affiche = True

        if typ != TYPE_RX_AUDIO or recepteur != self.args.rx:
            return
        if fmt not in FORMATS:
            print(f"  Format audio {fmt} non géré", file=sys.stderr)
            return

        dtype, echelle = FORMATS[fmt]
        # On déduit le nombre d'échantillons de la taille réelle de la trame
        # plutôt que du champ "longueur", dont l'interprétation varie selon les serveurs
        s = np.frombuffer(data[TCI_HEADER_SIZE:], dtype=dtype).astype(np.float32) / echelle

        # Stéréo entrelacé -> on ne garde que le canal gauche
        if canaux == 2:
            s = s[0::2]

        self.samplerate = sr or 48000
        self.blocs.append(s)

    def nb_echantillons(self):
        return sum(len(b) for b in self.blocs)

    async def run(self):
        uri = f"ws://{self.args.host}:{self.args.port}"
        print(f"Connexion à {uri} ...")
        async with websockets.connect(uri, max_size=None) as ws:
            # 1) Attente de "ready;" (le serveur envoie son état complet à la connexion)
            try:
                async with asyncio.timeout(5):
                    while True:
                        msg = await ws.recv()
                        if isinstance(msg, str):
                            self.traiter_texte(msg)
                            if "ready" in msg.lower():
                                break
            except TimeoutError:
                print("  Pas de 'ready;' reçu en 5 s, on continue quand même.")

            print(f"  Serveur : {self.infos.get('device', '?')} "
                  f"(protocole {self.infos.get('protocol', '?')})")

            # 2) Configuration du flux audio : float32 mono 48 kHz, blocs de 512
            rx = self.args.rx
            for cmd in ("audio_samplerate:48000;",
                        "audio_stream_sample_type:float32;",
                        "audio_stream_channels:1;",
                        "audio_stream_samples:512;",
                        f"audio_start:{rx};"):
                await ws.send(cmd)

            # 3) Réception pendant la durée demandée
            cible = int(self.args.duree * 48000)
            print(f"Enregistrement de {self.args.duree:.0f} s ...")
            debut = asyncio.get_running_loop().time()
            dernier_affichage = 0
            while self.nb_echantillons() < cible:
                try:
                    async with asyncio.timeout(5):
                        msg = await ws.recv()
                except TimeoutError:
                    print("  Aucune donnée depuis 5 s : le serveur n'envoie pas d'audio.",
                          file=sys.stderr)
                    break
                if isinstance(msg, bytes):
                    self.traiter_binaire(msg)
                else:
                    self.traiter_texte(msg)

                ecoule = int(asyncio.get_running_loop().time() - debut)
                if ecoule >= dernier_affichage + 5:
                    dernier_affichage = ecoule
                    print(f"  {self.nb_echantillons() / 48000:5.1f} s reçues")

            await ws.send(f"audio_stop:{rx};")

    def sauver(self):
        if not self.blocs:
            print("Aucun échantillon reçu, rien à sauver.")
            return None
        audio = np.concatenate(self.blocs)
        sr = self.samplerate

        # Nom de fichier automatique avec fréquence et horodatage UTC
        nom = self.args.sortie
        if not nom:
            freq = self.infos.get("vfo", "").split(",")[-1] or "inconnue"
            horo = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%SZ")
            nom = f"tci_{freq}_{horo}.wav"

        crete = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(audio ** 2)))

        # Conversion en int16 pour un WAV lisible partout (écrêtage de sécurité)
        pcm = np.clip(audio * 32767, -32768, 32767).astype("<i2")
        with wave.open(nom, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())

        print(f"\nFichier : {nom}")
        print(f"  Durée {len(audio) / sr:.1f} s à {sr} Hz — crête {crete:.3f}, RMS {rms:.4f}")
        if crete < 0.01:
            print("  ⚠ Niveau très faible : monte le gain audio du slice / du TCI.")
        elif crete >= 1.0:
            print("  ⚠ Saturation : baisse le niveau audio.")
        if "vfo" in self.infos:
            print(f"  VFO : {self.infos['vfo']}  Mode : {self.infos.get('modulation', '?')}")
        return audio, sr


def analyse_spectrale(audio, sr):
    """Spectre moyen et recherche de paires de raies espacées d'environ 170 Hz (mark/space RTTY)."""
    n = 8192
    fenetre = np.hanning(n)
    nb = len(audio) // n
    if nb == 0:
        return
    # Moyenne des spectres de puissance sur des tranches successives (méthode type Welch)
    spec = np.zeros(n // 2 + 1)
    for i in range(nb):
        spec += np.abs(np.fft.rfft(audio[i * n:(i + 1) * n] * fenetre)) ** 2
    spec_db = 10 * np.log10(spec / nb + 1e-20)
    freqs = np.fft.rfftfreq(n, 1 / sr)

    # Zone audio utile uniquement
    zone = (freqs > 200) & (freqs < 3500)
    f, s = freqs[zone], spec_db[zone]
    plancher = np.median(s)

    # Maxima locaux à plus de 10 dB au-dessus du plancher de bruit
    pics = [i for i in range(2, len(s) - 2)
            if s[i] == max(s[i - 2:i + 3]) and s[i] > plancher + 10]
    pics.sort(key=lambda i: s[i], reverse=True)

    # On écarte les raies à moins de 60 Hz d'une raie plus forte :
    # ce sont les bandes latérales de manipulation, pas des signaux distincts
    retenus = []
    for i in pics:
        if all(abs(f[i] - f[j]) > 60 for j in retenus):
            retenus.append(i)
    pics = retenus[:12]

    print("\nPrincipales raies audio (dB au-dessus du bruit) :")
    for i in sorted(pics, key=lambda i: f[i]):
        print(f"  {f[i]:7.1f} Hz  +{s[i] - plancher:4.1f} dB")

    # Paire RTTY : deux raies fortes, de niveau proche (±6 dB), espacées de 170 Hz ±10
    forts = [i for i in pics if s[i] > s[pics[0]] - 15] if pics else []
    paires = [(f[a], f[b]) for a in forts for b in forts
              if f[a] < f[b] and abs((f[b] - f[a]) - 170) < 10
              and abs(s[a] - s[b]) < 6]
    if paires:
        print("\nPaires compatibles RTTY 170 Hz (mark/space probables) :")
        for bas, haut in sorted(paires):
            print(f"  {bas:7.1f} Hz / {haut:7.1f} Hz  (écart {haut - bas:.0f} Hz)")
    else:
        print("\nAucune paire à 170 Hz détectée nettement.")


def main():
    args = parse_args()
    rec = Enregistreur(args)
    try:
        asyncio.run(rec.run())
    except OSError as e:
        print(f"Connexion impossible : {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrompu, sauvegarde de ce qui a été reçu.")
    res = rec.sauver()
    if res:
        analyse_spectrale(*res)


if __name__ == "__main__":
    main()
