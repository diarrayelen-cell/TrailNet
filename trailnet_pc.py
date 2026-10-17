#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────
#  TrailNet — prototype « PC + carte LoRa »                      v1.6
#  Parler → texte ou note vocale → radio LoRa → affichage, lecture.
#  Sparkia Box · Sekou & Issa · 2026
#
#  Le PC est le « cerveau » : micro, transcription Whisper, Codec2, envoi.
#  La carte LoRa (Meshtastic), branchée en USB, n'est que la radio.
#  Tout reste sur l'ordinateur : aucun serveur, aucun compte, aucun
#  envoi sur internet (le modèle Whisper se télécharge une seule fois).
#
#  Commandes :
#    py trailnet_pc.py ports                       ports USB détectés
#    py trailnet_pc.py canal   --port COM3         canal privé sur toutes tes cartes
#    py trailnet_pc.py noeuds  --port COM3         appareils connus, état de la radio
#    py trailnet_pc.py dicter  --port COM3         tu parles → texte → radio
#    py trailnet_pc.py vocal   --port COM3         tu parles → voix Codec2 → radio
#    py trailnet_pc.py ecouter --port COM4 --voix  réception, lecture à voix haute
#    py trailnet_pc.py dicter  --essai             transcription seule, sans carte
#    py trailnet_pc.py vocal   --essai             note vocale écoutée en local, sans carte
#
#  Copyright (C) 2026 Sekou et Issa — Sparkia Box
#  Ce programme est un logiciel libre : tu peux le redistribuer et/ou le
#  modifier selon les termes de la GNU General Public License publiée par
#  la Free Software Foundation, version 3 ou (à ton choix) toute version
#  ultérieure. Il est distribué SANS AUCUNE GARANTIE. Voir le fichier LICENSE.
# ─────────────────────────────────────────────────────────────────────
import argparse, base64, math, os, queue, re, secrets, struct, sys, threading, time, wave
from datetime import datetime

VERSION = "1.6"
SR = 16000          # Whisper travaille en 16 kHz mono
SR_VOIX = 8000      # Codec2 travaille en 8 kHz mono
MAX_OCTETS = 200    # texte par paquet (limite Meshtastic : 233 octets de données)
CODEC2_BPS = 800    # Codec2 700C (trames de 4 octets / 40 ms), pour comparer avec le texte
QUOTA_HEURE = 360   # Europe, 869,4–869,65 MHz : 10 % d'une heure = 360 s d'émission
DOSSIER_VOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages_vocaux")


# ── temps d'antenne (formule Semtech, préréglage LONG_FAST) ──────────
def temps_antenne(octets_utiles, sf=11, bw=250_000, cr=1, preambule=16, entete=20):
    """Durée d'émission d'un paquet, en secondes (entête Meshtastic ≈ 20 octets)."""
    pl = octets_utiles + entete
    tsym = (2 ** sf) / bw
    de = 1 if tsym > 0.016 else 0
    n = 8 + max(math.ceil((8 * pl - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * (cr + 4), 0)
    return (preambule + 4.25) * tsym + n * tsym


def part_du_quota(secondes):
    """Part du quota européen d'une heure (360 s) utilisée par `secondes` d'émission."""
    return 100 * secondes / QUOTA_HEURE


def decouper(texte, limite=MAX_OCTETS):
    """Coupe un texte trop long en morceaux de ≤ limite octets, sans couper les mots."""
    if len(texte.encode("utf-8")) <= limite:
        return [texte]
    morceaux, cur = [], ""
    for mot in texte.split():
        essai = (cur + " " + mot).strip()
        if len(essai.encode("utf-8")) + 8 <= limite:      # 8 octets pour « (12/15) »
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
def reechantillonner_vers(a, sr_in, sr_out):
    """Change la fréquence d'échantillonnage (par FFT : filtre anti-repliement inclus)."""
    import numpy as np
    a = np.asarray(a, dtype=np.float32)
    if sr_in == sr_out or len(a) == 0:
        return a
    marge = int(0.05 * sr_in)                         # évite les clics aux extrémités
    x = np.concatenate([np.zeros(marge, np.float32), a, np.zeros(marge, np.float32)])
    n_out = int(round(len(x) * sr_out / sr_in))
    X = np.fft.rfft(x)
    nb = n_out // 2 + 1
    Y = np.zeros(nb, dtype=complex)
    m = min(nb, len(X))
    Y[:m] = X[:m]
    y = np.fft.irfft(Y, n=n_out) * (n_out / len(x))
    m_out = int(round(marge * sr_out / sr_in))
    return y[m_out:m_out + int(round(len(a) * sr_out / sr_in))].astype(np.float32)


def reechantillonner(a, sr):
    return reechantillonner_vers(a, sr, SR)


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


def jouer(pcm, sr=SR_VOIX):
    """Joue un son (int16 ou float) sur les haut-parleurs et attend la fin."""
    import numpy as np
    try:
        import sounddevice as sd
    except Exception as e:
        print(f"   (lecture audio impossible : {e})")
        return
    x = np.asarray(pcm)
    x = x.astype(np.float32) / 32768.0 if x.dtype == np.int16 else x.astype(np.float32)
    try:
        sd.play(x, sr)
        sd.wait()
    except Exception:
        # certaines cartes son refusent le 8 kHz : on passe à leur fréquence native
        try:
            sr_sortie = int(sd.query_devices(kind="output")["default_samplerate"])
            sd.play(reechantillonner_vers(x, sr, sr_sortie), sr_sortie)
            sd.wait()
        except Exception as e:
            print(f"   (lecture audio impossible : {e})")


def sauver_wav(pcm, nom, sr=SR_VOIX):
    """Enregistre un son int16 dans messages_vocaux/ ; rend le chemin."""
    import numpy as np
    os.makedirs(DOSSIER_VOCAL, exist_ok=True)
    nom = re.sub(r"[^\w.-]+", "_", nom).strip("_") or "note"
    chemin = os.path.join(DOSSIER_VOCAL, nom + ".wav")
    x = np.asarray(pcm)
    if x.dtype != np.int16:
        x = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(chemin, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.astype("<i2").tobytes())
    return chemin


# ── Whisper, en local ────────────────────────────────────────────────
# Phrases que Whisper invente parfois sur du silence ou du bruit (vues pendant nos tests).
HALLUCINATIONS = re.compile(
    r"sous-titr|amara\.org|merci d'avoir regard|abonnez-vous|vous abonner|"
    r"merci de votre attention|prochaine vidéo|radio-canada|subtitles by|thanks for watching",
    re.I)


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
    """Rend (texte, durée de transcription, secondes de parole réelle)."""
    t0 = time.time()
    segs, info = m.transcribe(audio, language="fr", beam_size=5, vad_filter=True,
                              condition_on_previous_text=False)
    morceaux = [s.text.strip() for s in segs]
    texte = " ".join(t for t in morceaux if t and not HALLUCINATIONS.search(t)).strip()
    parole = getattr(info, "duration_after_vad", None) or len(audio) / SR
    return texte, time.time() - t0, parole


# ── préparation de la voix pour Codec2 ───────────────────────────────
QUALITES = ("700C", "1200", "1600", "3200")
MODE_C2 = {"700C": 700, "1200": 1200, "1600": 1600, "3200": 3200}
# En-tête de chaque paquet vocal : « TV », qualité, n° de note, trames au total,
# n° du morceau, nombre de morceaux. Puis les trames Codec2.
VOIX_ENTETE = struct.Struct("<2sBBHBB")
VOIX_MAGIC = b"TV"
VOIX_CHARGE = 176   # octets de voix par paquet (marge pour l'en-tête et le chiffrement)


def codec(qualite):
    import pycodec2
    return pycodec2.Codec2(MODE_C2[qualite])


def vers_8k(a):
    return reechantillonner_vers(a, SR, SR_VOIX)


def passe_haut(a, sr=SR_VOIX, fc=90):
    """Coupe les graves sous ~90 Hz (souffle, ronflement, chocs sur le micro)."""
    import numpy as np
    a = np.asarray(a, dtype=np.float32)
    if len(a) < 16:
        return a
    X = np.fft.rfft(a)
    f = np.fft.rfftfreq(len(a), 1 / sr)
    gain = np.clip((f - fc / 2) / (fc / 2), 0, 1)     # rampe douce de fc/2 à fc
    return np.fft.irfft(X * gain, n=len(a)).astype(np.float32)


def retirer_silences(audio):
    """Garde uniquement la parole (détecteur Silero, fourni avec faster-whisper)."""
    import numpy as np
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception:
        return audio
    ts = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=400, speech_pad_ms=150))
    if not ts:
        return audio[:0]
    pause = np.zeros(int(0.12 * SR), np.float32)
    morceaux = []
    for i, t in enumerate(ts):
        if i:
            morceaux.append(pause)
        morceaux.append(audio[t["start"]:t["end"]])
    return np.concatenate(morceaux).astype(np.float32)


def diagnostic_micro(a, sr=SR_VOIX):
    """Mesure la prise de son : part de voix nette et écart entre la voix et le fond.

    Codec2 est un vocodeur : il modélise UNE voix. Une télé, de la musique ou une
    vidéo à côté le font échouer, et la note sonne comme du bruit.
    """
    import numpy as np
    a = np.asarray(a, dtype=np.float64)
    lg, pas = int(0.040 * sr), int(0.016 * sr)
    if len(a) < lg * 4:
        return None
    trames = np.lib.stride_tricks.sliding_window_view(a, lg)[::pas]
    db = 10 * np.log10(np.mean(trames ** 2, axis=1) + 1e-10)
    haut, fond = np.percentile(db, 95), np.percentile(db, 10)
    actives = trames[db > haut - 25]
    lags = np.arange(sr // 400, sr // 70)                 # voix entre 70 et 400 Hz
    voisees = 0
    for t in actives:
        # trame « voisée » = presque périodique : corrélation normalisée avec elle-même,
        # décalée d'une période de la voix, supérieure à 0,6
        t = t - t.mean()
        croise = np.correlate(t, t, mode="full")[lg - 1 + lags]
        cumul = np.cumsum(t * t)
        e_debut = cumul[lg - lags - 1]
        e_fin = cumul[-1] - cumul[lags - 1]
        nccf = croise / np.sqrt(np.maximum(e_debut * e_fin, 1e-12))
        if nccf.max() > 0.6:
            voisees += 1
    return {"voix": 100 * voisees / max(len(actives), 1), "ecart": float(haut - fond)}


def afficher_diagnostic(diag):
    if not diag:
        return
    print(f"   Micro : {diag['voix']:.0f} % de voix nette · voix {diag['ecart']:.0f} dB au-dessus du fond.")
    if diag["voix"] < 35 or diag["ecart"] < 15:
        print("   ⚠ Il y a beaucoup de bruit autour de toi (télé, musique, vidéo, ventilateur ?).\n"
              "     Codec2 ne sait coder qu'une voix seule : la note risque de sonner comme du bruit.\n"
              "     Coupe la source, rapproche-toi du micro (15–20 cm), ou ajoute --debruiter.")


def charger_rnnoise():
    """Charge la bibliothèque RNNoise livrée avec le paquet pyrnnoise (sans l'importer)."""
    import ctypes, importlib.util
    spec = importlib.util.find_spec("pyrnnoise")
    if spec is None or not spec.submodule_search_locations:
        return None
    dossier = list(spec.submodule_search_locations)[0]
    nom = {"win32": "rnnoise.dll", "darwin": "librnnoise.dylib"}.get(sys.platform, "librnnoise.so")
    chemin = os.path.join(dossier, nom)
    if not os.path.exists(chemin):
        return None
    lib = ctypes.CDLL(chemin)
    lib.rnnoise_create.argtypes = [ctypes.c_void_p]
    lib.rnnoise_create.restype = ctypes.c_void_p
    lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
    lib.rnnoise_get_frame_size.restype = ctypes.c_int
    lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                          ctypes.POINTER(ctypes.c_float)]
    lib.rnnoise_process_frame.restype = ctypes.c_float
    return lib


def debruiter(audio, sr=SR):
    """Réduit le bruit de fond avec RNNoise (réseau de neurones léger, sur le PC)."""
    import ctypes
    import numpy as np
    try:
        lib = charger_rnnoise()
    except OSError:
        lib = None
    if lib is None:
        print("   (débruitage indisponible : installe-le avec  py -m pip install pyrnnoise --no-deps)")
        return audio
    x = reechantillonner_vers(audio, sr, 48000) * 32767.0      # RNNoise : 48 kHz, échelle 16 bits
    n = lib.rnnoise_get_frame_size()
    x = np.concatenate([x, np.zeros((-len(x)) % n, np.float32)]).astype(np.float32)
    sortie = np.empty_like(x)
    etat = lib.rnnoise_create(None)
    tampon_in, tampon_out = (ctypes.c_float * n)(), (ctypes.c_float * n)()
    vue_in = np.frombuffer(tampon_in, dtype=np.float32)
    vue_out = np.frombuffer(tampon_out, dtype=np.float32)
    for i in range(0, len(x), n):
        vue_in[:] = x[i:i + n]
        lib.rnnoise_process_frame(etat, tampon_out, tampon_in)
        sortie[i:i + n] = vue_out
    lib.rnnoise_destroy(etat)
    sortie = np.concatenate([sortie[n:], np.zeros(n, np.float32)])  # RNNoise décale d'une trame
    return reechantillonner_vers(sortie / 32767.0, 48000, sr)[:len(audio)]


def preparer_voix(audio, debruit=False, max_s=30):
    """16 kHz brut → parole seule, 8 kHz, filtrée, au bon niveau, en int16 (+ diagnostic)."""
    import numpy as np
    diag = diagnostic_micro(vers_8k(audio))
    if debruit:
        audio = debruiter(audio)
    parole = retirer_silences(audio)
    if len(parole) < SR // 4:
        return None, diag
    if len(parole) > max_s * SR:
        print(f"   (note coupée à {max_s} s de parole : change la limite avec --max)")
        parole = parole[:max_s * SR]
    x = passe_haut(vers_8k(parole))
    rms = float(np.sqrt(np.mean(x ** 2)))
    x = x * (0.1 / max(rms, 1e-6))                       # parole à −20 dBFS
    pic = float(np.max(np.abs(x)))
    if pic > 0.89:
        x = x * (0.89 / pic)                             # crêtes sous −1 dBFS : pas de saturation
    return (np.clip(x, -1, 1) * 32767).astype(np.int16), diag


def encoder_voix(pcm, qualite):
    """int16 8 kHz → trames Codec2 collées les unes aux autres."""
    import numpy as np
    c2 = codec(qualite)
    spf = c2.samples_per_frame()
    pcm = np.asarray(pcm, dtype=np.int16)
    pcm = np.concatenate([pcm, np.zeros((-len(pcm)) % spf, np.int16)])
    return b"".join(c2.encode(pcm[i:i + spf]) for i in range(0, len(pcm), spf))


def decoder_voix(octets, qualite, c2=None):
    """Trames Codec2 → int16 8 kHz."""
    import numpy as np
    c2 = c2 or codec(qualite)
    bpf = c2.bytes_per_frame()
    trames = [c2.decode(octets[i:i + bpf]) for i in range(0, len(octets) - bpf + 1, bpf)]
    return np.concatenate(trames).astype(np.int16) if trames else np.zeros(0, np.int16)


def paquets_voix(octets, qualite, id_note):
    """Découpe la note en paquets radio, chacun avec son en-tête."""
    c2 = codec(qualite)
    bpf = c2.bytes_per_frame()
    charge = (VOIX_CHARGE // bpf) * bpf                  # jamais de trame coupée en deux
    n_trames = len(octets) // bpf
    blocs = [octets[i:i + charge] for i in range(0, len(octets), charge)]
    if len(blocs) > 255:
        raise ValueError("note trop longue : 255 paquets au maximum")
    q = QUALITES.index(qualite)
    return [VOIX_ENTETE.pack(VOIX_MAGIC, q, id_note, n_trames, i, len(blocs)) + b
            for i, b in enumerate(blocs, 1)]


def lire_entete(payload):
    """Rend l'en-tête d'un paquet vocal TrailNet, ou None si ce n'en est pas un."""
    if not isinstance(payload, (bytes, bytearray)) or len(payload) <= VOIX_ENTETE.size:
        return None
    magic, q, id_note, n_trames, k, n = VOIX_ENTETE.unpack_from(payload)
    if magic != VOIX_MAGIC or q >= len(QUALITES) or not (1 <= k <= n):
        return None
    return {"qualite": QUALITES[q], "id": id_note, "trames": n_trames, "k": k, "n": n,
            "voix": bytes(payload[VOIX_ENTETE.size:])}


class ReceptionVocale:
    """Recolle une note vocale arrivée en plusieurs paquets, puis la décode.

    Si un paquet manque au bout de `delai` secondes sans nouveau morceau, on rend
    la note quand même, avec un silence à la place du morceau perdu.
    """

    def __init__(self, delai=300):
        self.delai, self.en_cours, self.verrou = delai, {}, threading.Lock()
        self.finies = {}          # notes déjà rendues : leurs morceaux en retard sont ignorés

    def ajouter(self, qui, payload, p=None):
        """Rend (note complète ou None, (reçus, total) ou None si ce n'est pas de la voix)."""
        h = lire_entete(payload)
        if h is None:
            return None, None
        cle = (qui, h["id"])
        with self.verrou:
            fin = self.finies.get(cle)
            if fin and time.time() - fin[0] < 600 and fin[1:] == (h["n"], h["qualite"]):
                return None, None                                 # morceau en retard d'une note déjà jouée
            e = self.en_cours.get(cle)
            if e is None or e["n"] != h["n"] or e["qualite"] != h["qualite"]:
                e = self.en_cours[cle] = {"n": h["n"], "qualite": h["qualite"],
                                          "trames": h["trames"], "parts": {}, "qui": qui}
            e["parts"][h["k"]] = h["voix"]
            e["t"], e["p"] = time.time(), p
            if len(e["parts"]) < e["n"]:
                return None, (len(e["parts"]), e["n"])
            del self.en_cours[cle]
            self.finies[cle] = (time.time(), e["n"], e["qualite"])
        return self.assembler(e), (e["n"], e["n"])

    def expires(self):
        """Notes incomplètes trop anciennes, rendues avec des silences."""
        now, vieilles = time.time(), []
        with self.verrou:
            for cle in [c for c, e in self.en_cours.items() if now - e["t"] > self.delai]:
                e = self.en_cours.pop(cle)
                self.finies[cle] = (now, e["n"], e["qualite"])
                vieilles.append(e)
        return [self.assembler(e) for e in vieilles]

    @staticmethod
    def assembler(e):
        import numpy as np
        c2 = codec(e["qualite"])
        bpf, spf = c2.bytes_per_frame(), c2.samples_per_frame()
        par_paquet = VOIX_CHARGE // bpf
        sons = []
        for k in range(1, e["n"] + 1):
            if k in e["parts"]:
                sons.append(decoder_voix(e["parts"][k], e["qualite"], c2))
            else:
                reste = e["trames"] - (k - 1) * par_paquet
                sons.append(np.zeros(max(0, min(par_paquet, reste)) * spf, np.int16))
        pcm = np.concatenate(sons) if sons else np.zeros(0, np.int16)
        return {"pcm": pcm, "qualite": e["qualite"], "recus": len(e["parts"]), "total": e["n"],
                "duree": len(pcm) / SR_VOIX, "qui": e["qui"], "p": e.get("p")}


# ── carte Meshtastic ─────────────────────────────────────────────────
ERREURS = {
    "DUTY_CYCLE_LIMIT": "bloqué par la limite européenne de temps d'émission (10 % sur une heure) : "
                        "attends quelques minutes",
    "MAX_RETRANSMIT": "pas de réponse après plusieurs essais de la carte (hors de portée ?)",
    "TIMEOUT": "délai dépassé (hors de portée ?)",
    "NO_ROUTE": "aucun chemin vers ce destinataire",
    "GOT_NAK": "refusé par le destinataire",
    "NOT_AUTHORIZED": "refusé par le destinataire",
    "TOO_LARGE": "paquet trop gros",
    "NO_CHANNEL": "le destinataire n'a pas le même canal (clés différentes ?)",
    "NO_INTERFACE": "la radio de la carte n'est pas prête (région réglée ?)",
    "PKI_FAILED": "échec du chiffrement par clé publique",
    "PKI_UNKNOWN_PUBKEY": "le destinataire ne connaît pas encore ta clé publique : attends une minute",
    "PKI_SEND_FAIL_PUBLIC_KEY": "ta carte n'a pas la clé publique du destinataire : attends une minute",
    "RATE_LIMIT_EXCEEDED": "trop de paquets d'un coup : la carte freine, attends un peu",
}


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
              "messages de canal, non.\n     Crée ton canal privé : py trailnet_pc.py canal --port …")


def etat_radio(iface):
    """Temps d'émission de la carte sur la dernière heure, et occupation du canal (en %)."""
    try:
        m = (iface.getMyNodeInfo() or {}).get("deviceMetrics", {}) or {}
    except Exception:
        return None
    tx, canal = m.get("airUtilTx"), m.get("channelUtilization")
    if tx is None and canal is None:
        return None
    return {"tx": tx, "canal": canal}


def afficher_etat_radio(iface):
    e = etat_radio(iface)
    if not e or e["tx"] is None:
        print("   Radio : pas encore de mesure du temps d'émission (la carte la publie de temps en temps).")
        return
    ligne = f"   Radio : ta carte a émis {e['tx']:.1f} % du temps sur la dernière heure (limite européenne : 10 %)"
    if e["canal"] is not None:
        ligne += f" · canal occupé à {e['canal']:.0f} %"
    print(ligne + ".")
    if e["tx"] >= 8:
        print("   ⚠ Tu approches de la limite : la carte va retarder ou refuser les prochains envois.\n"
              "     Attends quelques minutes, ou envoie des messages plus courts.")


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
                 f"py {os.path.basename(__file__)} noeuds --port …")
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


def annoncer_destinataire(dest):
    print(f"   Destinataire : {dest['nom']} ({dest['id']})"
          + (" · chiffrement par clé publique" if dest.get("cle") else
             " · ⚠ pas encore sa clé publique : le message sera chiffré avec la clé du canal.\n     Attends une minute que les cartes se découvrent (vérifie avec « noeuds »)."))


def envoyer_donnees(iface, donnees, dest, port=None, attente=60):
    """Envoie un paquet. dest = None : tout le canal (pas d'accusé individuel).

    Pour un message direct, attend l'accusé de réception. Rend (ok, explication).
    """
    from meshtastic.protobuf import portnums_pb2
    if port is None:
        port = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    if dest is None:
        iface.sendData(donnees, portNum=port, channelIndex=0)
        nom = iface.localNode.channels[0].settings.name or "principal"
        return True, f"envoyé sur le canal « {nom} »"
    fini, etat = threading.Event(), {}
    def reponse(p):
        r = p.get("decoded", {}).get("routing", {})
        etat["erreur"] = r.get("errorReason", "NONE")
        etat["implicite"] = int(p.get("from", 0)) == iface.myInfo.my_node_num
        fini.set()
    cle = base64.b64decode(dest["cle"]) if dest.get("cle") else None
    iface.sendData(donnees, destinationId=dest["id"], portNum=port, wantAck=True,
                   onResponse=reponse, onResponseAckPermitted=True,
                   pkiEncrypted=cle is not None, publicKey=cle)
    if not fini.wait(attente):
        return False, f"pas d'accusé de réception en {attente} s (hors de portée, ou carte qui attend son quota ?)"
    if etat["erreur"] != "NONE":
        return False, ERREURS.get(etat["erreur"], f"refusé ({etat['erreur']})")
    if etat["implicite"]:
        return True, "parti, relayé (accusé implicite)"
    return True, f"reçu par {dest['nom']}"


def envoyer_serie(iface, paquets, dest, port=None, quoi="paquet"):
    """Envoie les paquets un par un ; s'arrête après 2 échecs de suite. Rend le nombre réussi."""
    n, reussis, echecs = len(paquets), 0, 0
    for i, p in enumerate(paquets, 1):
        ok, info = envoyer_donnees(iface, p, dest, port)
        print(f"   → {quoi} {i}/{n}… {info}" if n > 1 else f"   → envoi… {info}")
        if ok:
            reussis, echecs = reussis + 1, 0
            continue
        echecs += 1
        if echecs >= 2 and i < n:
            print(f"   ✗ 2 échecs de suite : j'arrête là ({n - i} {quoi}(s) non envoyé(s)) pour ne pas "
                  "user le quota d'émission pour rien.")
            break
    if reussis < n:
        afficher_etat_radio(iface)
    return reussis


def expediteur(p):
    return p.get("fromId") or f"!{p.get('from', 0):08x}"


def decrire_paquet(iface, p):
    qui = expediteur(p)
    prive = "message direct, chiffré par clé publique" if p.get("pkiEncrypted") \
        else ("message direct" if p.get("toId") not in (None, "^all") else "canal")
    snr = p.get("rxSnr")
    radio = f" · SNR {snr:.1f} dB" if isinstance(snr, (int, float)) else ""
    return f"[{datetime.now():%H:%M}] {nom_de(iface, qui)} ({qui}) · {prive}{radio}"


def afficher_message(iface, p, texte=None):
    print(f"\n   {decrire_paquet(iface, p)}")
    print(f"   « {p.get('decoded', {}).get('text', '') if texte is None else texte} »")


PARTIE = re.compile(r"^\((\d+)/(\d+)\) ?(.*)$", re.S)


class Assembleur:
    """Recolle les messages longs, envoyés en plusieurs paquets « (1/3) … (3/3) ».

    Les morceaux arrivent un par un, parfois à plusieurs minutes d'écart quand la
    carte attend son quota d'émission. On attend d'avoir tout avant d'afficher et
    de lire. Si aucun nouveau morceau n'arrive pendant `delai` secondes, on rend
    ce qu'on a, marqué « incomplet ».
    """

    def __init__(self, delai=300):
        self.delai, self.en_cours, self.verrou = delai, {}, threading.Lock()
        self.abandonnes = []

    def ajouter(self, qui, texte):
        """Rend (message complet ou None, (reçus, total) ou None)."""
        m = PARTIE.match(texte)
        if not m or not (1 <= int(m.group(1)) <= int(m.group(2)) <= 50):
            return texte, None
        k, n, corps = int(m.group(1)), int(m.group(2)), m.group(3)
        with self.verrou:
            e = self.en_cours.get(qui)
            if e is not None and e["parts"].get(k) == corps:
                return None, (len(e["parts"]), e["n"])          # doublon : déjà reçu
            if e is None or e["n"] != n or k in e["parts"]:
                if e is not None:                                  # un nouveau message commence :
                    self.abandonnes.append(e)                      # l'ancien sera rendu « incomplet »
                e = self.en_cours[qui] = {"n": n, "parts": {}, "p": None}
            e["parts"][k] = corps
            e["t"] = time.time()                                  # le délai repart à chaque morceau
            if len(e["parts"]) < n:
                return None, (len(e["parts"]), n)
            del self.en_cours[qui]
            return " ".join(e["parts"][i] for i in range(1, n + 1)), (n, n)

    def garder_paquet(self, qui, p):
        with self.verrou:
            if qui in self.en_cours:
                self.en_cours[qui]["p"] = p

    def expires(self):
        """Messages incomplets sans nouveau morceau depuis `delai` s : [(paquet, texte partiel)]."""
        out, now = [], time.time()
        with self.verrou:
            vieux = [self.en_cours.pop(q) for q, e in list(self.en_cours.items()) if now - e["t"] > self.delai]
            vieux, self.abandonnes = self.abandonnes + vieux, []
            for e in vieux:
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
    afficher_etat_radio(iface)
    iface.close()


def ecouter_reponses(iface):
    """Pendant qu'on dicte, affiche aussi ce que l'autre carte nous envoie."""
    from pubsub import pub
    assembleur = Assembleur()
    def recu(packet, interface):
        recevoir(interface, packet, assembleur)
    pub.subscribe(recu, "meshtastic.receive.text")
    return recu                              # à garder en vie : pubsub ne garde qu'une référence faible


def cmd_dicter(a):
    modele = charger_whisper(a.modele)
    iface = dest = abonnement = None
    if not a.essai:
        iface = connecter(a.port)
        if a.canal:
            print(f"   Envoi à tout le canal « {iface.localNode.channels[0].settings.name or 'principal'} ».")
        else:
            dest = choisir_destinataire(iface, a.vers)
            annoncer_destinataire(dest)
        abonnement = ecouter_reponses(iface)

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
        texte, dt, parole = transcrire(modele, audio)
        if not texte:
            print("   Rien compris. Parle plus près du micro, dans une pièce calme.")
            if a.fichier:
                break
            continue

        n = len(texte.encode("utf-8"))
        voix = math.ceil(parole * CODEC2_BPS / 8)
        print(f"\n   Transcrit en {dt:.1f} s : « {texte} »")
        print(f"   {parole:.1f} s de parole → {n} octets de texte "
              f"(la voix en Codec2 : {voix} octets, soit {voix / max(n, 1):.0f}× plus).")
        morceaux = decouper(texte)
        ta = sum(temps_antenne(len(m.encode('utf-8'))) for m in morceaux)
        print(f"   {len(morceaux)} paquet(s) · ≈ {ta:.1f} s de temps d'antenne "
              f"({part_du_quota(ta):.1f} % de ton quota de l'heure).")

        if a.essai:
            print("   Mode essai : rien n'est envoyé.")
        else:
            r = input("   Envoyer ? [Entrée = oui · n = non · ou tape une correction] : ").strip()
            if r.lower() == "n":
                print("   Annulé.")
            else:
                if r:
                    morceaux = decouper(r)
                if len(morceaux) > 3:
                    r2 = input(f"   Ce message part en {len(morceaux)} paquets. Les longs messages peuvent "
                               "arriver en retard\n   (quota européen de 10 %). Envoyer quand même ? "
                               "[Entrée = oui · n = non] : ").strip().lower()
                    if r2 == "n":
                        print("   Annulé. Astuce : fais plusieurs messages courts.")
                        morceaux = []
                if morceaux:
                    envoyer_serie(iface, [m.encode("utf-8") for m in morceaux], dest, quoi="morceau")
        if a.fichier:
            break
    if iface:
        iface.close()
    del abonnement


def comparer_qualites(pcm):
    """Encode la même note dans les 4 qualités, enregistre et fait écouter chacune."""
    duree = len(pcm) / SR_VOIX
    horo = f"{datetime.now():%Y%m%d-%H%M%S}"
    print(f"\n   Comparaison sur {duree:.1f} s de voix :")
    print("   Qualité   Octets   Paquets   Antenne   Quota de l'heure")
    sons = []
    for q in QUALITES:
        octets = encoder_voix(pcm, q)
        paquets = paquets_voix(octets, q, 0)
        ta = sum(temps_antenne(len(p)) for p in paquets)
        print(f"   {q:<8} {len(octets):>7} {len(paquets):>9} {ta:>8.0f} s {part_du_quota(ta):>12.0f} %")
        son = decoder_voix(octets, q)
        sons.append((q, son, sauver_wav(son, f"comparaison_{horo}_{q}")))
    sauver_wav(pcm, f"comparaison_{horo}_original")
    print(f"   Fichiers enregistrés dans {DOSSIER_VOCAL} (original + 4 qualités).")
    for q, son, _ in sons:
        print(f"   ▶ {q}")
        jouer(son)
        time.sleep(0.4)


def cmd_vocal(a):
    iface = dest = None
    sans_carte = a.essai or a.comparer
    if not sans_carte:
        iface = connecter(a.port)
        dest = choisir_destinataire(iface, a.vers)
        annoncer_destinataire(dest)
    print(f"   Note vocale en Codec2 {a.qualite}, {a.max} s de parole au maximum"
          + (", débruitage activé." if a.debruiter else "."))

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
        pcm, diag = preparer_voix(audio, a.debruiter, a.max)
        afficher_diagnostic(diag)
        if pcm is None:
            print("   Je n'entends pas de voix. Parle plus près du micro.")
            if a.fichier:
                break
            continue

        if a.comparer:
            comparer_qualites(pcm)
        else:
            octets = encoder_voix(pcm, a.qualite)
            paquets = paquets_voix(octets, a.qualite, secrets.randbelow(256))
            ta = sum(temps_antenne(len(p)) for p in paquets)
            print(f"   {len(pcm) / SR_VOIX:.1f} s de voix → {len(octets)} octets en Codec2 {a.qualite} · "
                  f"{len(paquets)} paquet(s) · ≈ {ta:.0f} s d'antenne ({part_du_quota(ta):.0f} % de ton quota de l'heure).")
            apercu = decoder_voix(octets, a.qualite)
            chemin = sauver_wav(apercu, f"envoi_{datetime.now():%Y%m%d-%H%M%S}_{a.qualite}")
            if a.essai:
                print(f"   Mode essai : rien n'est envoyé. Voici ce que l'autre PC entendrait ({chemin}) :")
                jouer(apercu)
            else:
                r = input("   [Entrée = envoyer · e = écouter d'abord · n = annuler] : ").strip().lower()
                if r == "e":
                    jouer(apercu)
                    r = input("   Envoyer ? [Entrée = oui · n = non] : ").strip().lower()
                if r == "n":
                    print("   Annulé.")
                else:
                    from meshtastic.protobuf import portnums_pb2
                    envoyer_serie(iface, paquets, dest, portnums_pb2.PortNum.PRIVATE_APP, quoi="morceau")
        if a.fichier:
            break
    if iface:
        iface.close()


def cmd_ecouter(a):
    from pubsub import pub
    iface = connecter(a.port)
    file, assembleur, vocal = queue.Queue(), Assembleur(), ReceptionVocale()

    def annoncer(p, texte):
        if a.voix:
            file.put(("dire", f"Message de {nom_de(iface, expediteur(p))}. {texte}"))

    def note_prete(note):
        qui = note["qui"]
        nom = nom_de(iface, qui)
        incomplet = note["recus"] < note["total"]
        chemin = sauver_wav(note["pcm"], f"{datetime.now():%Y%m%d-%H%M%S}_{nom}")
        entete = decrire_paquet(iface, note["p"]) if note["p"] else f"{nom} ({qui})"
        print(f"\n   {entete}")
        print(f"   🎙 Note vocale de {note['duree']:.1f} s (Codec2 {note['qualite']})"
              + (f" · incomplète : {note['recus']}/{note['total']} morceaux, silences à la place des trous"
                 if incomplet else "") + f" · enregistrée : {chemin}")
        if a.voix:
            file.put(("dire", f"Note vocale de {nom}."))
            file.put(("jouer", note["pcm"]))

    def texte_recu(packet, interface):
        complet = recevoir(interface, packet, assembleur)
        if complet is not None:
            annoncer(packet, complet)

    def donnees_recues(packet, interface):
        qui = expediteur(packet)
        note, prog = vocal.ajouter(qui, packet.get("decoded", {}).get("payload", b""), packet)
        if prog is None:
            return                                   # pas un paquet TrailNet
        if note is None:
            print(f"   … {nom_de(iface, qui)} : note vocale, morceau {prog[0]}/{prog[1]} reçu")
        else:
            note_prete(note)

    # gardés en vie dans cette fonction : pubsub ne garde qu'une référence faible
    pub.subscribe(texte_recu, "meshtastic.receive.text")
    pub.subscribe(donnees_recues, "meshtastic.receive.data.PRIVATE_APP")

    lecteur = Lecteur() if a.voix else None
    print("   En attente de messages et de notes vocales… (Ctrl+C pour quitter)")
    try:
        while True:
            try:
                for p, texte in assembleur.expires():
                    if p is not None:
                        afficher_message(iface, p, texte)
                        annoncer(p, texte)
                for note in vocal.expires():
                    note_prete(note)
            except Exception as e:
                print(f"   (erreur pendant la réception : {e})")
            try:
                quoi, contenu = file.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if quoi == "dire":
                    lecteur.dire(contenu)
                else:
                    jouer(contenu)
            except Exception as e:                        # la voix plante : on continue d'écouter
                print(f"   (lecture impossible : {e})")
    except KeyboardInterrupt:
        pass
    iface.close()


def main():
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="TrailNet (prototype) — le PC comme cerveau de la carte radio LoRa")
    ap.add_argument("--version", action="version", version=f"TrailNet v{VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="lister les ports USB")
    p = sub.add_parser("canal", help="créer un canal privé et l'installer sur chaque carte")
    p.add_argument("--port", required=True, help="port de la première carte (ex. COM3)")
    p.add_argument("--nom", default="TrailNet", help="nom du canal (même nom sur toutes les cartes)")
    p.add_argument("--position", choices=["non", "precise"], default="non",
                   help="partager la position dans le canal (défaut : non)")
    p = sub.add_parser("noeuds", help="lister les appareils connus et l'état de la radio")
    p.add_argument("--port", required=True)
    p = sub.add_parser("dicter", help="parler → texte → envoi radio")
    p.add_argument("--port")
    p.add_argument("--vers", help="nom ou identifiant (!a1b2c3d4) du destinataire")
    p.add_argument("--canal", action="store_true", help="envoyer à tout le canal privé")
    p.add_argument("--modele", default="small", choices=["tiny", "base", "small", "medium"])
    p.add_argument("--fichier", help="utiliser un fichier WAV au lieu du micro")
    p.add_argument("--essai", action="store_true", help="transcrire sans envoyer (pas besoin de carte)")
    p = sub.add_parser("vocal", help="parler → note vocale Codec2 → envoi radio")
    p.add_argument("--port")
    p.add_argument("--vers", help="nom ou identifiant (!a1b2c3d4) du destinataire")
    p.add_argument("--qualite", default="700C", choices=QUALITES,
                   help="débit Codec2 : 700C (défaut, le plus léger) … 3200 (le plus fidèle)")
    p.add_argument("--max", type=int, default=30, help="secondes de parole au maximum (défaut : 30)")
    p.add_argument("--fichier", help="utiliser un fichier WAV au lieu du micro")
    p.add_argument("--essai", action="store_true", help="écouter le résultat sans envoyer (pas besoin de carte)")
    p.add_argument("--comparer", action="store_true", help="écouter la même note dans les 4 qualités")
    p.add_argument("--debruiter", action="store_true", help="réduire le bruit de fond (paquet pyrnnoise)")
    p = sub.add_parser("ecouter", help="recevoir les messages et les notes vocales")
    p.add_argument("--port", required=True)
    p.add_argument("--voix", action="store_true", help="lire à voix haute et jouer les notes vocales")
    a = ap.parse_args()
    if a.cmd == "dicter" and not a.essai and not a.port:
        ap.error("--port est obligatoire (sauf avec --essai)")
    if a.cmd == "vocal" and not (a.essai or a.comparer) and not a.port:
        ap.error("--port est obligatoire (sauf avec --essai ou --comparer)")
    if a.cmd == "vocal" and not 1 <= a.max <= 100:
        ap.error("--max doit être entre 1 et 100 secondes")
    {"ports": cmd_ports, "canal": cmd_canal, "noeuds": cmd_noeuds, "dicter": cmd_dicter,
     "vocal": cmd_vocal, "ecouter": cmd_ecouter}[a.cmd](a)


if __name__ == "__main__":
    main()
