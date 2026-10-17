# TrailNet — prototype

**Tu parles à ton PC. Ta phrase part par radio LoRa, sans réseau mobile ni internet. À l'arrivée, elle s'affiche et se lit à voix haute.**

Deux cartes LoRa à 23 €, deux PC, un script Python. Pas de serveur, pas de carte SIM, pas d'abonnement, pas de compte. Ce dépôt contient tout ce qu'il faut pour refaire le montage chez toi, étape par étape, avec les vraies erreurs qu'on a rencontrées et comment on les a corrigées.

TrailNet vise un communicateur hors réseau pour la randonnée. Ce prototype en valide la première brique : **la parole transmise sous forme de texte**. Le PC joue le rôle du futur ordinateur embarqué (micro, transcription, synthèse vocale) ; la carte LoRa n'est que la radio.

> ⚠ **Prototype de laboratoire.** Ce n'est pas un moyen d'alerte. En montagne, garde ton téléphone, le 112 et les moyens de secours officiels.

---

## Sommaire

1. [Pourquoi envoyer du texte plutôt que la voix](#pourquoi-envoyer-du-texte-plutôt-que-la-voix)
2. [Ce qu'on a mesuré](#ce-quon-a-mesuré)
3. [Matériel](#matériel)
4. [Logiciels](#logiciels)
5. [Étape 0 — Deux règles avant de brancher quoi que ce soit](#étape-0--deux-règles-avant-de-brancher-quoi-que-ce-soit)
6. [Étape 1 — Installer Meshtastic sur les cartes](#étape-1--installer-meshtastic-sur-les-cartes)
7. [Étape 2 — Préparer chaque PC](#étape-2--préparer-chaque-pc)
8. [Étape 3 — Tester la transcription, sans carte](#étape-3--tester-la-transcription-sans-carte)
9. [Étape 4 — Brancher une carte et trouver son port](#étape-4--brancher-une-carte-et-trouver-son-port)
10. [Étape 5 — Région et nom des cartes](#étape-5--région-et-nom-des-cartes)
11. [Étape 6 — Créer ton canal privé](#étape-6--créer-ton-canal-privé)
12. [Étape 7 — Test 1 : dicter et recevoir](#étape-7--test-1--dicter-et-recevoir)
13. [Étape 8 — Test 2 : lecture à voix haute](#étape-8--test-2--lecture-à-voix-haute)
14. [Toutes les commandes](#toutes-les-commandes)
15. [Dépannage](#dépannage)
16. [Ce qui est protégé, ce qui ne l'est pas](#ce-qui-est-protégé-ce-qui-ne-lest-pas)
17. [Portée : à quoi t'attendre](#portée--à-quoi-tattendre)
18. [Réglementation](#réglementation)
19. [Pour aller plus loin](#pour-aller-plus-loin)
20. [Ce qui existe déjà](#ce-qui-existe-déjà)
21. [Structure du dépôt, auteurs, licence](#structure-du-dépôt)

---

## Pourquoi envoyer du texte plutôt que la voix

LoRa porte loin, mais son débit est minuscule : environ **1 kbit/s** avec le réglage par défaut de Meshtastic (LONG_FAST), et chaque paquet ne transporte qu'environ **230 octets**. En Europe, le temps d'émission est en plus limité par la réglementation.

Même avec Codec2, un codec conçu pour la voix à très bas débit, une note vocale de 25 s pèse environ 2 500 octets : 13 paquets et une vingtaine de secondes d'émission. La même phrase transcrite en texte tient en **un seul paquet**.

D'où le principe du prototype :

```
micro → Whisper (sur le PC) → texte → carte A ~~~ radio ~~~ carte B → texte → voix de synthèse
```

## Ce qu'on a mesuré

Tests du 9 octobre 2026, en intérieur, PC portable récent (Ryzen 5), modèle Whisper `small`. Les valeurs Codec2 sont **calculées** pour comparaison, pas transmises.

| | Note 1 | Note 2 | Note 3 |
|---|---|---|---|
| Durée de parole | 24,9 s | 22,4 s | 33,2 s |
| Temps de transcription | 2,9 s | 2,9 s | 5,0 s |
| Texte transmis | 141 octets | 184 octets | 224 octets |
| Paquets radio | 1 | 1 | 2 |
| Temps d'antenne | ≈ 1,5 s | ≈ 1,8 s | ≈ 2,6 s |
| Même note en voix Codec2 700C | 2 489 o · 13 paquets · ≈ 23 s | 2 239 o · 12 paquets · ≈ 21 s | 3 323 o · 17 paquets · ≈ 31 s |
| Accusé de réception | ✓ | ✓ | ✓ (2/2) |

**11 octobre 2026, PC → PC** : 43,1 s de parole sont devenues 433 octets, en 3 paquets et environ 4,5 s d'antenne, contre 4 311 octets, 22 paquets et environ 40 s en Codec2. Les 5 paquets des deux messages du test sont arrivés, avec un accusé de réception chacun et un SNR de 6 à 7 dB. Sur le PC d'arrivée, ils ont été affichés et lus à voix haute.

Le texte est **6 à 18 fois plus léger** que la voix compressée : plus tu parles vite et sans pause, plus l'écart se réduit. La transcription n'est pas parfaite : on a eu par exemple « divas » pour *device* et « récits » pour *reçus*. C'est pour ça que le script te montre le texte et te laisse le corriger avant l'envoi.

## Matériel

| Article | Qté | Prix indicatif | À savoir |
|---|---|---|---|
| Carte **Heltec WiFi LoRa 32 V3**, version **868 MHz**, avec son antenne | 2 | ≈ 23 € pièce | Prends la version 863–928 MHz ou 868 MHz, pas 433 MHz. Achète chez un distributeur sérieux. |
| Câble **USB-C de données** | 2 | ≈ 5 € | **Piège n° 1.** Beaucoup de câbles ne font que charger. Sur l'emballage, cherche « données », « data » ou « synchronisation ». |
| PC Windows 10 ou 11 avec micro et haut-parleurs | 2 | — | Emprunte le deuxième. Un seul PC + un téléphone suffit pour les tests 1 et 2 (voir plus bas). |
| Téléphone avec l'app **Meshtastic** | 1 | — | Optionnel, pratique pour vérifier qu'un message arrive. |

Total si tu as déjà les PC : **≈ 56 €** (prix constatés en septembre–octobre 2026).

## Logiciels

Tout est gratuit :

- **Pilote CP210x** de Silicon Labs : la puce USB des cartes Heltec ;
- **Firmware Meshtastic** : le logiciel libre qui transforme la carte en radio maillée ;
- **Python 3.12 ou plus récent** (testé avec 3.12 et 3.14) ;
- **ce dépôt** : le script `trailnet_pc.py`.

Testé sous **Windows**. Linux et macOS devraient fonctionner mais n'ont pas été testés : remplace `py` par `python3`, et les ports s'appellent `/dev/ttyUSB0` ou `/dev/cu.usbserial-…`.

Le script utilise `meshtastic` (dialogue avec la carte), `faster-whisper` (transcription locale), `sounddevice` (micro) et la voix intégrée de Windows (lecture à voix haute).

---

## Étape 0 — Deux règles avant de brancher quoi que ce soit

1. **Visse l'antenne avant d'alimenter la carte.** Une radio qui émet sans antenne peut griller son étage de sortie.
2. **Un seul programme à la fois par carte.** Si le site Meshtastic, le flasheur ou un autre terminal utilise le port, le script ne pourra pas s'y connecter.

## Étape 1 — Installer Meshtastic sur les cartes

À faire une fois par carte.

1. Installe le **pilote CP210x** depuis le site de Silicon Labs (« CP210x Universal Windows Driver ») : décompresse, clic droit sur `silabser.inf`, puis **Installer**.
2. Branche la carte avec un **câble de données**. Son écran doit s'allumer.
3. Ouvre **https://flasher.meshtastic.org** dans **Chrome** ou **Edge** (Firefox ne sait pas parler aux ports série).
4. Choisis l'appareil **Heltec V3**, la dernière version **stable**, clique sur **Flash**, puis sélectionne le port de la carte. Pour une première installation, coche l'effacement complet.
5. Attends la fin : la carte redémarre et affiche l'écran Meshtastic.

Recommence avec la deuxième carte.

## Étape 2 — Préparer chaque PC

À faire sur les deux PC.

**1. Vérifie Python.** Dans PowerShell :

```powershell
py --list
```

Si tu vois `-V:3.12`, `-V:3.13` ou `-V:3.14`, c'est bon. Sinon, installe Python depuis python.org et coche **« Add python.exe to PATH »**.

> Plusieurs versions installées ? `py` utilise celle marquée d'une étoile. Installe et lance **toujours avec la même** : si tu installes avec `py -3.12`, lance avec `py -3.12`.

**2. Crée un dossier et place-toi dedans.**

```powershell
mkdir C:\projets\TrailNet
cd C:\projets\TrailNet
explorer .
```

La dernière commande ouvre le dossier : copies-y les fichiers de ce dépôt (bouton de téléchargement **ZIP** en haut de la page du dépôt, ou `git clone`).

> Un nom de dossier avec un espace, comme `Sekou Lamine` ? Mets-le entre guillemets : `cd "Sekou Lamine"`.

**3. Installe les bibliothèques.**

```powershell
py -m pip install -r requirements.txt
```

Si pip ne trouve pas un seul des paquets demandés, **il n'installe rien du tout**. Une faute de frappe dans un nom suffit : relis la commande si une erreur s'affiche.

## Étape 3 — Tester la transcription, sans carte

```powershell
py trailnet_pc.py dicter --essai
```

Au premier lancement, le modèle Whisper `small` se télécharge (environ 460 Mo). **C'est la seule connexion à internet de tout le projet** : ensuite, le script charge le modèle depuis le disque, sans réseau.

Appuie sur Entrée, parle, appuie de nouveau sur Entrée. Tu dois voir :

```
   Transcrit en 2.9 s : « On fait une pause au col, on repart dans vingt minutes. »
   3.8 s de parole → 55 octets de texte (la voix en Codec2 : 380 octets, soit 7× plus).
   1 paquet(s) · ≈ 0.8 s de temps d'antenne.
   Mode essai : rien n'est envoyé.
```

Tape `q` pour quitter.

## Étape 4 — Brancher une carte et trouver son port

Antenne vissée, carte branchée avec le câble de données :

```powershell
py trailnet_pc.py ports
```

```
  COM3     Silicon Labs CP210x USB to UART Bridge (COM3)   ← carte LoRa probable
```

Note ce numéro : c'est lui que tu mettras après `--port`. Il peut changer d'une carte à l'autre ou d'une prise USB à l'autre : en cas de doute, relance `ports`.

**Rien ne s'affiche ?** C'est presque toujours le câble (charge seule) ou le pilote. Vérifie avec :

```powershell
Get-PnpDevice -PresentOnly | Where-Object FriendlyName -match 'CP210|UART'
```

- `Silicon Labs CP210x … (COM5)` avec le statut `OK` : la carte est reconnue sur COM5 ;
- `CP2102 USB to UART Bridge Controller` avec le statut `Error` : le pilote manque (étape 1) ;
- rien du tout : change de câble ou de prise USB.

## Étape 5 — Région et nom des cartes

Sur chaque carte, une fois. Donne-lui un nom lisible (le nom court fait 4 caractères au maximum) :

```powershell
py -m meshtastic --port COM3 --set-owner "Sekou A" --set-owner-short "SKA"
```

Puis règle la région européenne. **Sans région, la carte n'émet pas.** Elle redémarre après cette commande :

```powershell
py -m meshtastic --port COM3 --set lora.region EU_868
```

Attends que l'écran revienne, branche l'autre carte et recommence avec un autre nom.

## Étape 6 — Créer ton canal privé

Par défaut, Meshtastic utilise un canal public, dont la clé est connue de tous les utilisateurs. En le testant à Lille, nos cartes ont d'ailleurs aussitôt repéré deux appareils inconnus du voisinage.

La commande `canal` fabrique une **clé aléatoire de 256 bits**, l'installe sur tes cartes **l'une après l'autre**, puis l'oublie. Elle n'est jamais affichée et jamais écrite sur le disque. Avec un seul câble de données, tu fais tout depuis un seul PC :

```powershell
py trailnet_pc.py canal --port COM3
```

```
→ Canal privé « TrailNet » · nouvelle clé aléatoire (jamais affichée) · position non partagée
   ✓ Canal « TrailNet » installé sur Sekou A (!a1b2c3d4).

   Branche la carte suivante, attends son écran d'accueil, puis Entrée · q si c'était la dernière :
   Port de cette carte [COM3] :
   ✓ Canal « TrailNet » installé sur Sekou B (!e5f6a7b8).

   2 carte(s) partagent le canal « TrailNet » : Sekou A (!a1b2c3d4), Sekou B (!e5f6a7b8)
```

Par défaut, **la position n'est pas partagée**. Pour la partager uniquement avec les membres du canal, ajoute `--position precise`.

> 🔒 **Ne colle jamais l'adresse du canal** (`https://meshtastic.org/e/#…`) dans un message, un mail, un chat ou une capture d'écran : elle contient la clé. Pour ajouter un proche, il scanne le QR code du canal depuis l'app Meshtastic, directement sur ton écran.

## Étape 7 — Test 1 : dicter et recevoir

Carte A sur le PC 1, carte B alimentée (sur le PC 2, ou par n'importe quel chargeur USB) :

```powershell
py trailnet_pc.py dicter --port COM3
```

Le script te demande à qui envoyer si la carte connaît plusieurs appareils. Les appareils d'autres utilisateurs Meshtastic peuvent apparaître dans la liste : choisis le tien, avec le nom ou l'identifiant affiché sur l'écran de ta carte B.

```
   Destinataire : Sekou B (!e5f6a7b8) · chiffrement par clé publique
   Transcrit en 2.9 s : « Ceci est le tout premier test que j'ai réalisé sur ce projet. »
   4.6 s de parole → 63 octets de texte (la voix en Codec2 : 460 octets, soit 7× plus).
   1 paquet(s) · ≈ 0.9 s de temps d'antenne.
   Envoyer ? [Entrée = oui · n = non · ou tape une correction] :
   → envoi… reçu par Sekou B
```

**« reçu par … »** signifie que la carte B a renvoyé un accusé de réception. Le message s'affiche aussi sur son écran, et sur le téléphone s'il y est relié en Bluetooth.

Pour écrire à **tout le canal** plutôt qu'à une seule carte, ajoute `--canal`.

## Étape 8 — Test 2 : lecture à voix haute

Sur le PC 2, avec la carte B :

```powershell
py trailnet_pc.py ecouter --port COM4 --voix
```

```
   Lecture à voix haute activée : Microsoft Hortense - French (France) (hors ligne).
   En attente de messages… (Ctrl+C pour quitter)

   [21:47] Sekou A (!a1b2c3d4) · message direct, chiffré par clé publique · SNR 9.5 dB
   « On fait une pause au col, on repart dans vingt minutes. »
```

… et le PC le dit à voix haute.

Un message long part en plusieurs paquets, numérotés « (1/3) », « (2/3) »… À l'arrivée, le script attend d'avoir tous les morceaux, les recolle dans l'ordre, puis affiche et lit le message **en une seule fois**. Si un morceau se perd, il rend ce qu'il a reçu au bout de 90 s, avec la mention « incomplet ».

**Un seul PC ?** Mets-le en `ecouter --voix` sur la carte A, relie ton téléphone à la carte B en Bluetooth, et envoie un message depuis l'app, dans le canal « TrailNet ». Il traverse la radio, arrive sur la carte A et le PC le lit. C'est aussi la meilleure façon de vérifier que tes deux cartes partagent bien le même canal privé.

---

## Toutes les commandes

| Commande | Ce qu'elle fait |
|---|---|
| `py trailnet_pc.py ports` | Liste les ports série, repère les cartes LoRa |
| `py trailnet_pc.py canal --port COM3` | Crée un canal privé et l'installe carte après carte |
| `py trailnet_pc.py noeuds --port COM3` | Liste les appareils connus de la carte, et s'ils ont envoyé leur clé publique |
| `py trailnet_pc.py dicter --port COM3` | Micro → texte → message direct chiffré, avec accusé de réception |
| `py trailnet_pc.py dicter --port COM3 --canal` | Pareil, envoyé à tout le canal privé |
| `py trailnet_pc.py dicter --essai` | Transcription seule, sans carte |
| `py trailnet_pc.py dicter --essai --fichier note.wav` | Transcription d'un fichier WAV 16 bits |
| `py trailnet_pc.py ecouter --port COM4 --voix` | Affiche les messages reçus et les lit à voix haute |
| `--modele medium` (avec `dicter`) | Transcription plus précise, plus lente (≈ 1,5 Go) |

## Dépannage

| Ce que tu vois | Pourquoi | Que faire |
|---|---|---|
| `Aucun port trouvé` | Câble « charge seule » ou pilote absent | Câble de données, pilote CP210x, puis `Get-PnpDevice` (étape 4) |
| `No module named 'meshtastic'` (ou `'pubsub'`) | L'installation a échoué ou a visé une autre version de Python | Relance l'étape 2.3 avec **le même** `py` que pour lancer le script |
| `could not open port 'COM3': FileNotFoundError` | Aucune carte sur ce port | Relance `ports` et utilise le bon numéro |
| `Connection timed out` | Carte encore en train de démarrer, ou téléphone connecté | Attends 10 s, déconnecte le téléphone dans l'app, réessaie |
| Le port est « utilisé par un autre processus » | Site Meshtastic, flasheur ou autre terminal ouvert | Ferme-les |
| `Impossible de trouver un paramètre positionnel` dans PowerShell | Un espace dans un nom de dossier | Guillemets : `cd "Mon Dossier"` |
| La voix ne lit que le premier message | Ancienne version du script | Utilise celle de ce dépôt (v1.3, voix de Windows) |
| Aucune voix française | Pas de voix française installée | Paramètres Windows → Heure et langue → Voix → Ajouter |
| `pas d'accusé de réception en 60 s` | Carte B éteinte, hors de portée, ou clés publiques pas encore échangées | Vérifie avec `noeuds` (« clé publique reçue »), attends une minute après l'allumage |
| Un message de canal n'arrive pas, alors que les messages directs passent | Les deux cartes n'ont pas la même clé de canal | Relance `canal` et passe **toutes** les cartes dans la même session |
| Mots mal transcrits | Micro loin, phrases longues, mots techniques | Parle près du micro, en phrases courtes, corrige avant d'envoyer, ou essaie `--modele medium` |

## Ce qui est protégé, ce qui ne l'est pas

**Protégé :**

- **Messages directs** : chiffrés avec la clé publique de la carte destinataire (Meshtastic 2.5 et plus récent). Seule cette carte peut les lire.
- **Canal privé** : chiffré avec ta clé de 256 bits, que seules tes cartes connaissent.
- **Position** : pas partagée par défaut. Avec `--position precise`, seulement aux membres du canal.
- **Ta voix** : elle ne quitte jamais le PC. Seul le texte part, par radio.

**Pas protégé** (c'est le fonctionnement de Meshtastic, pas un bug du script) :

- chaque émission radio porte **en clair le numéro de la carte** qui émet ;
- n'importe qui équipé d'un récepteur peut **détecter** qu'une radio émet, et approximativement d'où.

## Portée : à quoi t'attendre

Ce sont des ordres de grandeur ; ça dépend énormément du terrain.

- **En ville, à l'intérieur** : de quelques centaines de mètres à quelques kilomètres.
- **En terrain dégagé ou en hauteur** : souvent plusieurs dizaines de kilomètres entre deux points qui se voient.
- **Chaque carte relaie les messages des autres** (3 sauts par défaut). Plus il y a de cartes, plus le réseau porte loin.

Deux cartes **ne relient pas deux villes** : Lille–Paris, c'est environ 200 km. Pour se parler à cette distance, il faudrait toute une chaîne de relais entre les deux. Fais tes tests dans le même quartier, puis éloigne-toi progressivement.

## Réglementation

En Europe, la bande **868 MHz** est utilisable sans licence, à condition de respecter des limites de puissance, de fréquences et de temps d'émission. Le firmware Meshtastic applique ces limites dès que la région **EU_868** est choisie (étape 5). Ne modifie pas la puissance ni les fréquences à la main, et n'émets jamais sans antenne.

## Pour aller plus loin

Des pistes d'exercices, de la plus simple à la plus ambitieuse :

- **Vocabulaire** : aider Whisper avec une liste de mots du domaine (col, refuge, sentier…).
- **Vraie voix** : encoder la note en Codec2, la découper en paquets, la réassembler et la jouer à l'arrivée, puis comparer avec le texte.
- **GPS** : ajouter un module GNSS en 3,3 V et partager la position, seulement dans le canal privé.
- **Batterie** : une LiPo 3,7 V pour mesurer l'autonomie réelle d'une carte.
- **Raspberry Pi** : remplacer le PC par un ordinateur embarqué, avec micro et haut-parleur.

## Ce qui existe déjà

L'idée de transmettre « ce qui est dit » plutôt que « le son » est ancienne, et la recherche récente arrive aux mêmes conclusions que nos mesures :

- **1989** — J. Picone et G. Doddington (Texas Instruments), [*A Phonetic Vocoder*](https://isip.piconepress.com/publications/conference_proceedings/1989/ieee_icassp/vocoder/paper_v00.pdf) : reconnaître les sons de la parole, ne transmettre que leur liste (≈ 170 bit/s) et resynthétiser à l'arrivée.
- **2025** — Université Nazarbayev, [*Voice Over LoRa*](https://nur.nu.edu.kz/items/29925539-2eea-4822-83d6-ae36bd809397) : Whisper, un modèle de langage qui résume, et une synthèse vocale, sur Raspberry Pi 4 avec LoRa, hors ligne.
- **2026** — SLIIT, [*Voice Communication over Meshtastic Networks*](https://rda.sliit.lk/handle/123456789/5109) : sur Meshtastic, la voix Codec2 demande des minutes de transmission, la transcription un seul paquet.

Briques utilisées : [Meshtastic](https://meshtastic.org) (GPL-3.0), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) et les modèles [Whisper](https://github.com/openai/whisper) d'OpenAI (MIT).

---

## Structure du dépôt

```
trailnet-proto/
├── README.md          ce guide
├── trailnet_pc.py     le script (ports, canal, noeuds, dicter, ecouter)
├── requirements.txt   bibliothèques Python
├── LICENSE            GNU GPL v3
└── docs/
    └── schema.svg     le schéma du montage
```

## Auteurs

**Sekou & Issa** — [Sparkia Box](https://sparkiabox.com), la communauté des étudiants GEII.

Une question, un bug, une amélioration ? Ouvre un ticket sur le dépôt.

## Licence

Code et documentation sous **GNU General Public License v3.0** ou ultérieure : voir [LICENSE](LICENSE). Tu peux réutiliser, modifier et redistribuer ce projet, à condition que tes versions restent libres sous la même licence.
