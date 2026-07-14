# DocGenerator

DocGenerator est le produit TrysCode responsable de la génération des documents
pédagogiques. Le dépôt contient deux processus complémentaires :

- `main.py` et `templates/` : moteur CLI historique de substitution dans des
  modèles ODT, avec conversion par LibreOffice ;
- `worker/` : worker RabbitMQ de production, rendu PDF déterministe, stockage
  local ou S3/MinIO et callback vers Harmony.

Le worker a été intégré depuis l'ancien dossier local non Git
`WKR_document_generator`. Sa provenance et les conditions de bascule sont
documentées dans [MIGRATION_PROVENANCE.md](MIGRATION_PROVENANCE.md).

## Prérequis

- Python 3.12 ;
- LibreOffice uniquement pour le moteur ODT historique ;
- Docker pour construire l'image du worker et exécuter la preuve MinIO ;
- un RabbitMQ 3.10+ pour lancer le worker, avec les capacités de quorum queue
  décrites dans [worker/README.md](worker/README.md).

Aucun chemin personnel n'est requis. Les scripts résolvent leurs chemins depuis
la racine du dépôt et acceptent les surcharges documentées par variables
d'environnement.

Le moteur ODT détecte les exécutables `libreoffice` et `soffice`. Une
installation non standard peut être indiquée avec `LIBREOFFICE_BIN`.

## Installation de développement

```sh
git clone https://github.com/tryscode-eu/DocGenerator.git
cd DocGenerator
make install
```

`make install` crée `.venv` si aucun environnement virtuel n'est actif, installe
le lock de développement avec vérification des empreintes, puis installe le
package worker sans résoudre de nouvelles dépendances.

La commande vérifie Python avant toute installation. Si `python3` n'est pas une
version 3.12, indiquer explicitement l'interpréteur, par exemple
`make install PYTHON=python3.12`. Un `.venv` créé avec une autre version est
refusé avec une consigne de recréation au lieu d'être réutilisé silencieusement.

## Vérifications

```sh
make lint
make test
make scan-secrets
make build-package
make docker-worker
```

La CI exécute ces contrôles sur Python 3.12.13 et construit l'image depuis le
contexte `worker/`.

## Moteur ODT historique

Le moteur remplace les marqueurs simples `{% variable %}` et les blocs
`{%! for items until %}...{%! end %}`, puis demande à LibreOffice de convertir
le résultat en PDF.

```sh
umask 077
$EDITOR /tmp/docgenerator-input.json
.venv/bin/python main.py templates/test_template.odt output.pdf \
  --data-file /tmp/docgenerator-input.json
```

Le fichier est refusé s'il n'est pas régulier ou si ses permissions dépassent
`0600`. `--data-stdin` permet de ne créer aucun fichier. Le JSON n'est jamais
accepté dans les arguments du processus, ni écrit dans les logs applicatifs ;
les chemins template/sortie restent toutefois visibles comme pour toute
commande locale. L'orchestration asynchrone, l'idempotence et le callback
Harmony appartiennent au worker.

## Worker RabbitMQ

```sh
cp worker/.env.example worker/.env
# Renseigner les valeurs locales sans les committer.
make run-worker
```

`HARMONY_CALLBACK_URL` et le token de service dédié sont obligatoires. Les
retries internes exigent aussi une clé HMAC dédiée
`DOCUMENT_RETRY_SIGNING_KEY`. Les
contrats, files, stratégies de retry/archive et modes de stockage sont
détaillés dans [worker/README.md](worker/README.md). Les secrets restent des
variables d'environnement ; seuls leurs noms sont versionnés.

## Image

```sh
docker build --file worker/Dockerfile --tag tryscode/docgenerator-worker:dev worker
```

L'image utilise une base Python référencée par digest, installe le lock avec
`--require-hashes` et s'exécute avec l'UID/GID non privilégié `10001`.

## Statut juridique

Le statut de licence est explicité dans [LICENSE_STATUS.md](LICENSE_STATUS.md).
Cette documentation ne prétend pas qu'une entité ou certification inexistante
aurait déjà été constituée ou obtenue.
