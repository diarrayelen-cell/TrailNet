#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────
#  TrailNet — prototype « PC + carte LoRa »                      v1.3
#  Parler → texte → radio LoRa → affichage et lecture à voix haute.
#  Sparkia Box · Sekou & Issa · 2026
#
#  Le PC est le « cerveau » : micro, transcription Whisper, envoi.
#  La carte LoRa (Meshtastic), branchée en USB, n'est que la radio.
#  Tout reste sur l'ordinateur : aucun serveur, aucun compte, aucun
#  envoi sur internet (le modèle Whisper se télécharge une seule fois).
#
#  Commandes :
#    python trailnet_pc.py ports                       ports USB détectés
#    python trailnet_pc.py canal   --port COM3         canal privé sur toutes tes cartes
#    python trailnet_pc.py noeuds  --port COM3         appareils connus de la carte
#    python trailnet_pc.py dicter  --port COM3         tu parles → texte → radio
#    python trailnet_pc.py ecouter --port COM4 --voix  réception + lecture à voix haute
#    python trailnet_pc.py dicter  --essai             transcription seule, sans carte
#
#  Copyright (C) 2026 Sekou et Issa — Sparkia Box
#  Ce programme est un logiciel libre : tu peux le redistribuer et/ou le
#  modifier selon les termes de la GNU General Public License publiée par
#  la Free Software Foundation, version 3 ou (à ton choix) toute version
#  ultérieure. Il est distribué SANS AUCUNE GARANTIE. Voir le fichier LICENSE.
# ─────────────────────────────────────────────────────────────────────
import argparse, base64, math, os, queue, re, sys, threading, time, wave
from datetime import datetime

SR = 16000          # Whisper travaille en 16 kHz mono
MAX_OCTETS = 200    # texte par paquet (limite Meshtastic : 233 octets de données)
CODEC2_BPS = 800    # Codec2 700C, pour comparer avec la voix compressée


# ── temps d'antenne (formule Semtech, préréglage LONG_FAST) ──────────
def temps_antenne(octets_utiles, sf=11, bw=250_000, cr=1, preambule=16, entete=20):
    """Durée d'émission d'un paquet, en secondes (entête Meshtastic ≈ 20 octets)."""
    pl = octets_utiles + entete
    tsym = (2 ** sf) / bw
    de = 1 if tsym > 0.016 else 0
    n = 8 + max(math.ceil((8 * pl - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * (cr + 4), 0)
    return (preambule + 4.25) * tsym + n * tsym


def decouper(texte, limite=MAX_OCTETS):
    """Coupe un texte trop long en morceaux de ≤ limite octets, sans couper les mots."""
    if len(texte.encode("utf-8")) <= limite:
        return [texte]
    morceaux, cur = [], ""
    for mot in texte.split():
        essai = (cur + " " + mot).strip()
        if len(essai.encode("utf-8")) + 6 <= limite:      # 6 octets pour « (1/3) »
            cur = essai
        else:
            if cur:
                morceaux.append(cur)
            cur = mot
    if cur:
        morceaux.append(cur)
    n = len(morceaux)
    return [f"({i}/{n}) {m}" for i, m in enumerate(morceaux, 1)]


# ── audio ────────────────────────────────────────────────────────────
def lire_wav(chemin):
    """Lit un WAV PCM 16 bits (mono ou stéréo), le ramène en 16 kHz mono."""
    import numpy as np
    with wave.open(chemin, "rb") as w:
        nch, sw, sr, nfr = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        brut = w.readframes(nfr)
    if sw != 2:
        sys.exit("Le WAV doit être en 16 bits. Convertis-le : ffmpeg -i note.m4a -ar 16000 -ac 1 note.wav")
    a = np.frombuffer(brut, dtype="<i2").astype(np.float32) / 32768.0
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return reechantillonner(a, sr)


def reechantillonner(a, sr):
    import numpy as np
    if sr == SR:
        return a.astype(np.float32)
    t_out = np.arange(int(len(a) * SR / sr)) / SR
    return np.interp(t_out, np.arange(len(a)) / sr, a).astype(np.float32)


def enregistrer():
    """Enregistre au micro entre deux appuis sur Entrée."""
    import numpy as np
    import sounddevice as sd
    blocs = []
    def rappel(data, frames, t, status):
        blocs.append(data.copy())
    try:
        flux = sd.InputStream(samplerate=SR, channels=1, dtype="float32", callback=rappel)
        sr = SR
    except Exception:
        sr = int(sd.query_devices(kind="input")["default_samplerate"])
        flux = sd.InputStream(samplerate=sr, channels=1, dtype="float32", callback=rappel)
    with flux:
        input("   ● J'écoute… parle, puis appuie sur Entrée pour arrêter.")
    if not blocs:
        return None
    a = np.concatenate(blocs)[:, 0]
    return reechantillonner(a, sr)


# ── Whisper, en local ────────────────────────────────────────────────
def charger_whisper(modele):
    from faster_whisper import WhisperModel
    t0 = time.time()
    try:
        # d'abord sans réseau : si le modèle est déjà sur le disque, rien ne sort du PC
        m = WhisperModel(modele, device="cpu", compute_type="int8", local_files_only=True)
    except Exception:
        print(f"→ Premier lancement : téléchargement unique du modèle « {modele} »…")
        m = WhisperModel(modele, device="cpu", compute_type="int8")
    print(f"→ Modèle « {modele} » prêt ({time.time() - t0:.1f} s). Il tourne sur ce PC, hors ligne.")
    return m


def transcrire(m, audio):
    t0 = time.time()
    segs, _ = m.transcribe(audio, language="fr", beam_size=5, vad_filter=True)
    texte = " ".join(s.text.strip() for s in segs).strip()
    return texte, time.time() - t0


# ── carte Meshtastic ─────────────────────────────────────────────────
def connecter(port, verifier=True):
    import meshtastic.serial_interface
    print(f"→ Connexion à la carte sur {port}…")
    try:
        iface = meshtastic.serial_interface.SerialInterface(devPath=port)
    except Exception as e:
        sys.exit(f"Impossible d'ouvrir {port} : {e}\n"
                 "   Vérifie le port (commande « ports »), attends que la carte ait fini de démarrer,\n"
                 "   et ferme tout autre logiciel qui utilise ce port (site Meshtastic, autre terminal).")
    print(f"   Connecté à « {iface.getLongName()} » ({mon_id(iface)}).")
    if verifier:
        verifier_canal(iface)
    return iface


def mon_id(iface):
    return f"!{iface.myInfo.my_node_num:08x}"


def verifier_canal(iface):
    """Prévient si le canal principal utilise la clé publique par défaut."""
    try:
        psk = bytes(iface.localNode.channels[0].settings.psk)
    except Exception:
        return
    if len(psk) <= 1:
        print("   ⚠ Le canal principal utilise la clé par défaut, connue de tous les utilisateurs "
              "Meshtastic.\n     Les messages directs restent chiffrés par clé publique ; les "
              "messages de canal, non.\n     Crée ton canal privé : python trailnet_pc.py canal --port …")


def autres_noeuds(iface):
    moi = iface.myInfo.my_node_num
    out = []
    for nid, n in (iface.nodes or {}).items():
        if n.get("num") == moi:
            continue
        u = n.get("user", {})
        out.append({"id": u.get("id", nid), "nom": u.get("longName", nid),
                    "court": u.get("shortName", ""), "cle": u.get("publicKey"),
                    "vu": n.get("lastHeard"), "snr": n.get("snr")})
    out.sort(key=lambda x: -(x["vu"] or 0))
    return out


def nom_de(iface, nid):
    n = (iface.nodes or {}).get(nid, {})
    return n.get("user", {}).get("longName", nid)


def choisir_destinataire(iface, vers):
    noeuds = autres_noeuds(iface)
    if vers:
        for n in noeuds:
            if vers in (n["id"], n["nom"], n["court"]):
                return n
        sys.exit(f"Appareil « {vers} » inconnu de la carte. Liste-les avec : "
                 f"python {os.path.basename(__file__)} noeuds --port …")
    if not noeuds:
        sys.exit("La carte ne connaît encore aucun autre appareil. Allume l'autre carte et attends "
                 "une minute qu'elles se découvrent.")
    if len(noeuds) == 1:
        return noeuds[0]
    print("   Plusieurs appareils connus :")
    for i, n in enumerate(noeuds, 1):
        print(f"     {i}. {n['nom']} ({n['id']})")
    while True:
        r = input("   Numéro du destinataire : ").strip()
        if r.isdigit() and 1 <= int(r) <= len(noeuds):
            return noeuds[int(r) - 1]


def envoyer(iface, texte, dest):
    """Envoie un message direct et attend l'accusé de réception (60 s max)."""
    from meshtastic.protobuf import portnums_pb2
    fini, etat = threading.Event(), {}
    def reponse(p):
        r = p.get("decoded", {}).get("routing", {})
        etat["erreur"] = r.get("errorReason", "NONE")
        etat["implicite"] = int(p.get("from", 0)) == iface.myInfo.my_node_num
        fini.set()
    cle = base64.b64decode(dest["cle"]) if dest.get("cle") else None
    iface.sendData(texte.encode("utf-8"), destinationId=dest["id"],
                   portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, wantAck=True,
                   onResponse=reponse, onResponseAckPermitted=True,
                   pkiEncrypted=cle is not None, publicKey=cle)
    if not fini.wait(60):
        return "pas d'accusé de réception en 60 s (hors de portée ?)"
    if etat["erreur"] != "NONE":
        return f"refusé ({etat['erreur']})"
    if etat["implicite"]:
        return "parti, relayé (accusé implicite)"
    return f"reçu par {dest['nom']}"


def envoyer_canal(iface, texte):
    """Envoie à tous les membres du canal privé (pas d'accusé individuel)."""
    iface.sendText(texte, channelIndex=0)
    nom = iface.localNode.channels[0].settings.name or "principal"
    return f"envoyé sur le canal « {nom} »"


def expediteur(p):
    return p.get("fromId") or f"!{p.get('from', 0):08x}"


def afficher_message(iface, p, texte=None):
    d = p.get("decoded", {})
    qui = expediteur(p)
    prive = "message direct, chiffré par clé publique" if p.get("pkiEncrypted") \
        else ("message direct" if p.get("toId") not in (None, "^all") else "canal")
    snr = p.get("rxSnr")
    radio = f" · SNR {snr:.1f} dB" if isinstance(snr, (int, float)) else ""
    print(f"\n   [{datetime.now():%H:%M}] {nom_de(iface, qui)} ({qui}) · {prive}{radio}")
    print(f"   « {d.get('text', '') if texte is None else texte} »")


PARTIE = re.compile(r"^\((\d+)/(\d+)\) ?(.*)$", re.S)


class Assembleur:
    """Recolle les messages longs, envoyés en plusieurs paquets « (1/3) … (3/3) ».

    Les morceaux arrivent un par un ; on attend d'avoir tout avant d'afficher et de
    lire. Si un morceau manque au bout de `delai` secondes, on rend ce qu'on a.
    """

    def __init__(self, delai=90):
        self.delai, self.en_cours, self.verrou = delai, {}, threading.Lock()

    def ajouter(self, qui, texte):
        """Rend (message complet ou None, (reçus, total) ou None)."""
        m = PARTIE.match(texte)
        if not m or not (1 <= int(m.group(1)) <= int(m.group(2)) <= 50):
            return texte, None
        k, n, corps = int(m.group(1)), int(m.group(2)), m.group(3)
        with self.verrou:
            e = self.en_cours.get(qui)
            if e is None or e["n"] != n or k in e["parts"]:
                e = self.en_cours[qui] = {"n": n, "parts": {}, "t": time.time(), "p": None}
            e["parts"][k] = corps
            if len(e["parts"]) < n:
                return None, (len(e["parts"]), n)
            del self.en_cours[qui]
            return " ".join(e["parts"][i] for i in range(1, n + 1)), (n, n)

    def garder_paquet(self, qui, p):
        with self.verrou:
            if qui in self.en_cours:
                self.en_cours[qui]["p"] = p

    def expires(self):
        """Messages incomplets trop anciens : [(paquet, texte partiel)]."""
        out, now = [], time.time()
        with self.verrou:
            for qui in [q for q, e in self.en_cours.items() if now - e["t"] > self.delai]:
                e = self.en_cours.pop(qui)
                txt = " […] ".join(e["parts"][i] for i in sorted(e["parts"]))
                out.append((e["p"], f"{txt} [incomplet : {len(e['parts'])}/{e['n']} morceaux reçus]"))
        return out


def recevoir(iface, p, assembleur):
    """Affiche un paquet texte reçu ; rend le message complet quand il l'est."""
    qui = expediteur(p)
    complet, prog = assembleur.ajouter(qui, p.get("decoded", {}).get("text", ""))
    if complet is None:
        assembleur.garder_paquet(qui, p)
        print(f"   … {nom_de(iface, qui)} : morceau {prog[0]}/{prog[1]} reçu")
        return None
    afficher_message(iface, p, complet)
    return complet


# ── lecture à voix haute, hors ligne ─────────────────────────────────
VOIX_FR = ("French", "Français", "Francais", "Hortense", "Julie", "Paul", "Denise", "Henri")


class Lecteur:
    """Voix de synthèse de l'ordinateur, sans internet.

    Sous Windows, on parle directement au moteur vocal du système (SAPI) :
    pyttsx3 a un défaut connu sous Windows, il ne lit que le premier message.
    """

    def __init__(self):
        self.sapi = None
        if sys.platform == "win32":
            try:
                import win32com.client
                v = win32com.client.Dispatch("SAPI.SpVoice")
                voix = v.GetVoices()
                for i in range(voix.Count):
                    if any(k in voix.Item(i).GetDescription() for k in VOIX_FR):
                        v.Voice = voix.Item(i)
                        break
                v.Rate = 1
                self.sapi = v
                nom = v.Voice.GetDescription()
            except Exception as e:
                print(f"   (moteur vocal Windows indisponible : {e} — repli sur pyttsx3)")
        if self.sapi is None:
            nom = "pyttsx3"
        print(f"   Lecture à voix haute activée : {nom} (hors ligne).")

    def dire(self, phrase):
        if self.sapi is not None:
            self.sapi.Speak(phrase)          # synchrone : rend la main quand la phrase est dite
            return
        import pyttsx3                       # repli : un moteur neuf par message
        m = pyttsx3.init()
        for v in m.getProperty("voices"):
            if "fr" in str(v.languages).lower() or any(k in v.name for k in VOIX_FR):
                m.setProperty("voice", v.id)
                break
        m.setProperty("rate", 175)
        m.say(phrase)
        m.runAndWait()
        m.stop()


# ── commandes ────────────────────────────────────────────────────────
def ports_detectes():
    """Ports série présents, avec leur description (Silicon Labs = carte Heltec)."""
    from serial.tools import list_ports
    return [(p.device, p.description or "") for p in list_ports.comports()]


def cmd_ports(a):
    ports = ports_detectes()
    if not ports:
        print("Aucun port trouvé. Branche la carte avec un câble de DONNÉES, "
              "et vérifie le pilote CP210x.")
    for dev, desc in ports:
        marque = "   ← carte LoRa probable" if ("CP210" in desc or "Silicon" in desc
                                                or "CH9102" in desc or "CH340" in desc) else ""
        print(f"  {dev:<8} {desc}{marque}")


def cmd_canal(a):
    """Crée un canal privé et l'installe sur chaque carte, l'une après l'autre.

    La clé (32 octets aléatoires) est fabriquée ici, gardée en mémoire le temps
    de la commande, et n'est jamais affichée ni écrite sur le disque.
    """
    import secrets
    from meshtastic.protobuf import channel_pb2
    cle = secrets.token_bytes(32)
    precision = 32 if a.position == "precise" else 0
    port, faites = a.port, []
    print(f"→ Canal privé « {a.nom} » · nouvelle clé aléatoire (jamais affichée) · "
          f"position {'partagée dans le canal' if precision else 'non partagée'}")
    while True:
        try:
            iface = connecter(port, verifier=False)
        except SystemExit as e:
            print(f"   {e}")
            r = input("   Entrée pour réessayer · q pour arrêter : ").strip().lower()
            if r == "q":
                break
            continue
        ch = iface.localNode.channels[0]
        ch.settings.name = a.nom
        ch.settings.psk = cle
        ch.settings.module_settings.position_precision = precision
        ch.role = channel_pb2.Channel.Role.PRIMARY
        iface.localNode.writeChannel(0)
        time.sleep(2)                       # laisse la carte enregistrer avant de fermer
        faites.append(f"{iface.getLongName()} ({mon_id(iface)})")
        iface.close()
        print(f"   ✓ Canal « {a.nom} » installé sur {faites[-1]}.")
        r = input("\n   Branche la carte suivante, attends son écran d'accueil, puis Entrée"
                  " · q si c'était la dernière : ").strip().lower()
        if r == "q":
            break
        presents = [d for d, _ in ports_detectes()]
        defaut = port if port in presents else (presents[0] if presents else port)
        r = input(f"   Port de cette carte [{defaut}] : ").strip()
        port = r or defaut
    del cle
    print(f"\n   {len(faites)} carte(s) partagent le canal « {a.nom} » : " + ", ".join(faites))
    print("   La clé n'existe plus que dans ces cartes. Pour ajouter un proche plus tard,"
          " il scanne le QR code du canal depuis l'app Meshtastic, sur ton écran.")


def cmd_noeuds(a):
    iface = connecter(a.port)
    noeuds = autres_noeuds(iface)
    if not noeuds:
        print("   Aucun autre appareil connu pour l'instant.")
    for n in noeuds:
        vu = datetime.fromtimestamp(n["vu"]).strftime("%d/%m %H:%M") if n["vu"] else "jamais"
        cle = "clé publique reçue" if n["cle"] else "pas encore de clé"
        print(f"   {n['nom']:<24} {n['id']}   vu {vu}   {cle}")
    iface.close()


def cmd_dicter(a):
    modele = charger_whisper(a.modele)
    iface = dest = None
    if not a.essai:
        iface = connecter(a.port)
        if getattr(a, "canal", False):
            print(f"   Envoi à tout le canal « {iface.localNode.channels[0].settings.name or 'principal'} ».")
        else:
            dest = choisir_destinataire(iface, a.vers)
            print(f"   Destinataire : {dest['nom']} ({dest['id']})"
                  + (" · chiffrement par clé publique" if dest.get("cle") else
                     " · ⚠ pas encore de clé publique : attends que les cartes se découvrent"))
        from pubsub import pub
        assembleur = Assembleur()
        def recu(packet, interface):          # gardé en vie : pubsub ne garde qu'une référence faible
            recevoir(interface, packet, assembleur)
        pub.subscribe(recu, "meshtastic.receive.text")

    while True:
        if a.fichier:
            audio = lire_wav(a.fichier)
        else:
            r = input("\nEntrée pour parler · q pour quitter : ").strip().lower()
            if r == "q":
                break
            audio = enregistrer()
            if audio is None or len(audio) < SR // 2:
                print("   Trop court, recommence.")
                continue
        duree = len(audio) / SR
        texte, dt = transcrire(modele, audio)
        if not texte:
            print("   Rien compris. Parle plus près du micro.")
            if a.fichier:
                break
            continue

        n = len(texte.encode("utf-8"))
        voix = math.ceil(duree * CODEC2_BPS / 8)
        print(f"\n   Transcrit en {dt:.1f} s : « {texte} »")
        print(f"   {duree:.1f} s de parole → {n} octets de texte "
              f"(la voix en Codec2 : {voix} octets, soit {voix / max(n, 1):.0f}× plus).")
        morceaux = decouper(texte)
        ta = sum(temps_antenne(len(m.encode('utf-8'))) for m in morceaux)
        print(f"   {len(morceaux)} paquet(s) · ≈ {ta:.1f} s de temps d'antenne.")

        if a.essai:
            print("   Mode essai : rien n'est envoyé.")
        else:
            r = input("   Envoyer ? [Entrée = oui · n = non · ou tape une correction] : ").strip()
            if r.lower() == "n":
                print("   Annulé.")
            else:
                if r:
                    morceaux = decouper(r)
                for m in morceaux:
                    etat = envoyer_canal(iface, m) if getattr(a, "canal", False) else envoyer(iface, m, dest)
                    print(f"   → envoi… {etat}")
        if a.fichier:
            break
    if iface:
        iface.close()


def cmd_ecouter(a):
    from pubsub import pub
    iface = connecter(a.port)
    file, assembleur = queue.Queue(), Assembleur()

    def annoncer(p, texte):
        if a.voix:
            file.put(f"Message de {nom_de(iface, expediteur(p))}. {texte}")

    def recu(packet, interface):
        complet = recevoir(interface, packet, assembleur)
        if complet is not None:
            annoncer(packet, complet)
    pub.subscribe(recu, "meshtastic.receive.text")

    lecteur = Lecteur() if a.voix else None
    print("   En attente de messages… (Ctrl+C pour quitter)")
    try:
        while True:
            for p, texte in assembleur.expires():
                if p is not None:
                    afficher_message(iface, p, texte)
                    annoncer(p, texte)
            try:
                phrase = file.get(timeout=0.5)
            except queue.Empty:
                continue
            lecteur.dire(phrase)
    except KeyboardInterrupt:
        pass
    iface.close()


def main():
    ap = argparse.ArgumentParser(description="TrailNet (prototype) — le PC comme cerveau de la carte radio LoRa")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="lister les ports USB")
    p = sub.add_parser("canal", help="créer un canal privé et l'installer sur chaque carte")
    p.add_argument("--port", required=True, help="port de la première carte (ex. COM3)")
    p.add_argument("--nom", default="TrailNet", help="nom du canal (même nom sur toutes les cartes)")
    p.add_argument("--position", choices=["non", "precise"], default="non",
                   help="partager la position dans le canal (défaut : non)")
    p = sub.add_parser("noeuds", help="lister les appareils connus")
    p.add_argument("--port", required=True)
    p = sub.add_parser("dicter", help="parler → texte → envoi radio")
    p.add_argument("--port")
    p.add_argument("--vers", help="nom ou identifiant (!a1b2c3d4) du destinataire")
    p.add_argument("--canal", action="store_true", help="envoyer à tout le canal privé")
    p.add_argument("--modele", default="small", choices=["tiny", "base", "small", "medium"])
    p.add_argument("--fichier", help="utiliser un fichier WAV au lieu du micro")
    p.add_argument("--essai", action="store_true", help="transcrire sans envoyer (pas besoin de carte)")
    p = sub.add_parser("ecouter", help="recevoir les messages")
    p.add_argument("--port", required=True)
    p.add_argument("--voix", action="store_true", help="lire les messages à voix haute")
    a = ap.parse_args()
    if a.cmd == "dicter" and not a.essai and not a.port:
        ap.error("--port est obligatoire (sauf avec --essai)")
    {"ports": cmd_ports, "canal": cmd_canal, "noeuds": cmd_noeuds, "dicter": cmd_dicter, "ecouter": cmd_ecouter}[a.cmd](a)


if __name__ == "__main__":
    main()
