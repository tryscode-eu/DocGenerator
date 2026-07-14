# DocGenerator — worker RabbitMQ

Ce composant appartient au dépôt DocGenerator et vit dans `worker/`. Il ne
constitue pas un produit ou dépôt indépendant.

Worker RabbitMQ qui rend les documents pédagogiques à partir de contrats JSON
contrôlés. Il génère des PDF A4 TrysCode avec ReportLab et les écrit dans
`DOCUMENT_OUTPUT_DIR` : sujet, certificat de réussite, attestation de
formation, carte apprenant imprimable et compte rendu de revue humaine.

Le message attendu est de la forme :

```json
{
  "contract_version": "tryscode.document-render.v1",
  "action": "render_subject_pdf",
  "job_id": "job-2026-01",
  "subject_code": "kubernetes-workloads",
  "title": "Kubernetes Workloads",
  "author": "Baptiste RENNESON BOUTARD",
  "sections": [{"heading": "Objectif", "body": "..."}]
}
```

Actions disponibles :

- `render_subject_pdf` : `subject_code`, `title`, `author`, `sections`.
- `render_certificate_pdf` : `certificate_code`, `certificate_name`,
  `learner_name`, `issued_on`, `verification_code` et, si nécessaire,
  `rncp_code`.
- `render_attestation_pdf` : `training_code`, `training_name`,
  `learner_name`, `campus_name`, `period_start`, `period_end`, `total_hours`
  et `issued_on`.
- `render_student_card_pdf` : `member_id`, `learner_name`, `campus_name`,
  `issued_on` et `valid_until`.
- `render_review_pdf` : sujet, groupe, participants, reviewer, date, type,
  verdict, retour, éventuelles médailles et preuve technique optionnelle
  `moulinette_evidence`. Les médailles ne sont acceptées que pour une revue
  finale humaine validée.

Pour une revue, `moulinette_evidence` peut être absent ou `null`. Sinon, il
doit respecter exactement le contrat `tryscode.review-evidence.v1` et le
schéma de rapport `javamoulinette.report.v1` : aucun champ supplémentaire
n'est accepté, à la racine du message de revue comme dans les objets de preuve.
`run_id` est un identifiant de 1 à 96 caractères, l'empreinte est un SHA-256
hexadécimal minuscule, le mode vaut `drawer` ou `expected`, et chaque compte
est un entier strict entre 0 et 200 (le total est compris entre 1 et 200).
Les sommes globales, requises et par statut doivent être cohérentes. Le code de
motif est limité à 64 caractères et son texte à 500 caractères.

Le PDF ne reprend de cette preuve que l'identifiant d'exécution, le mode, le
verdict et son motif, les comptes agrégés et l'empreinte. Il n'affiche ni item,
ni chemin, ni payload worker, ni erreur interne, ni score, GPA ou rang. La
preuve reste informative : la décision pédagogique et l'attribution des
médailles restent humaines.

Les identifiants, dates et tailles de texte sont validés, le contenu est
échappé avant le rendu et aucun corps de document n’est écrit dans les logs.
Le contrat `tryscode.document-render.v1` est rendu avec une version exacte de
ReportLab. Un replay produit les mêmes octets pour les cinq familles de
documents. Le PDF est installé une seule fois par lien atomique : un fichier
existant identique est réutilisé, tandis qu’une même clé portant d’autres
octets provoque une collision fermée. Le fichier puis son entrée de répertoire
sont synchronisés avant toute notification. Une mise à jour de la chaîne de
rendu susceptible de changer les octets exige donc un nouveau contrat ou une
preuve explicite de compatibilité binaire.

Les échecs transitoires passent par
`document_tasks.retry`, déclarée comme quorum queue. Le worker confirme la
copie retry ou archive avec `mandatory=true` avant d'acquitter le message
source. La file retry utilise en plus le dead-lettering `at-least-once`,
`reject-publish` et des bornes en nombre et en octets : son retour vers la file
principale attend donc un confirm interne RabbitMQ. Une panne peut produire un
doublon, mais pas une perte silencieuse entre ces files. Les tâches
invalides ou épuisées deviennent une enveloppe minimale dans
`document_tasks.archive` : version, nombre d'essais, code d'échec, taille et
empreinte bornée du message. Le corps, les noms, le feedback, les URL et les
headers entrants ne sont jamais archivés. L'archive expire par défaut après
sept jours et conserve au plus 10 000 messages ; atteindre la borne supprime
explicitement le plus ancien. Un message supérieur à 5 Mio est archivé sans
être décodé.

Le compteur de retry n'est repris que sur un message revenu de la file retry
avec un en-tête `x-death` RabbitMQ cohérent et une signature HMAC du corps, du
compteur et du mode interne. Cette signature utilise
`DOCUMENT_RETRY_SIGNING_KEY`, secret dédié distinct du token Harmony. Un
producteur ne peut donc pas forger un retry ou un callback terminal ; un état
interne non signé est archivé sans rendu ni callback. La clé doit être commune
aux instances et la file retry doit être drainée avant rotation. La signature
garantit l'intégrité, pas l'anti-rejeu d'une continuation déjà signée : le compte
RabbitMQ du producteur ne doit avoir aucun droit de lecture sur les files retry
ou archive ; seul le worker peut les lire et connaître la clé HMAC. Un rejeu
résiduel après redelivery reste couvert par l'idempotence du rendu, du stockage
et des callbacks. Déployer ces files dans un vhost dédié avec deux utilisateurs
distincts : le producteur peut seulement écrire vers la queue principale via
l'exchange autorisé, sans droit `read` ni `configure` sur retry/archive ; le
worker lit la queue principale et possède les seuls droits nécessaires pour
déclarer et écrire les files internes. Une identité opérateur séparée administre
l'archive. Ne jamais partager le compte worker avec le producteur. Les confirms,
connexions et alarmes
de blocage sont bornés par timeout afin qu'une panne du broker déclenche un
reconnect avec temporisation au lieu de bloquer l'unique livraison en vol.
Le broker doit être RabbitMQ 3.10+ avec le feature flag `stream_queue` actif ;
la cible V1 testée est RabbitMQ 3.13, où ce flag est actif par défaut sur les
clusters nouvellement créés.

Les arguments TTL/longueur font partie de la déclaration RabbitMQ. Lors d'une
mise à niveau depuis une file archive créée sans ces arguments, ou après tout
changement ultérieur du TTL ou de la longueur maximale, arrêter le worker,
traiter puis supprimer l'ancienne archive sensible et recréer la file (ou
appliquer une politique RabbitMQ compatible) avant de redémarrer. RabbitMQ
refuse sinon la redéclaration avec `PRECONDITION_FAILED`.

La même précondition s'applique à `document_tasks.retry` : une file classique
existante doit être drainée puis recréée en quorum queue avec les arguments
`at-least-once`, `reject-publish` et les bornes configurées. Le type de file ne
peut pas être modifié par redéclaration ou politique.

`HARMONY_CALLBACK_URL` et `HARMONY_SERVICE_TOKEN` sont obligatoires. Le worker
appelle le callback interne `/api/v1/jobs/callback` après chaque rendu. Le
callback contient l’identifiant
de job, le statut, la clé de stockage opaque, la taille exacte, le SHA-256 et,
si le stockage en fournit une, la version d’objet. Il ne contient jamais un
chemin local ni le document. Harmony relit indépendamment cette version,
recalcule taille et empreinte, parse le PDF et refuse le contenu actif ou
embarqué avant de persister l’identité immuable. Après épuisement des retries de
traitement, un job complet et fiable reçoit un callback terminal minimal
`failed`, sans document ni diagnostic interne. Un message invalide dont
l'identité complète ne peut pas être validée reste archivé sans identifiant
inventé.

Si le callback de succès échoue après publication de l'artefact, le worker
reconstruit une continuation compacte contenant seulement l'identité opaque de
l'artefact, la signe et la passe par la file retry. Le document source, les
noms et le feedback ne sont pas republiés ; le rendu et l'upload ne sont pas
rejoués. Le callback terminal `failed` suit le même mécanisme. Après la borne de
retry, seule une enveloppe technique `callback-failed` est archivée. Une erreur
d'ACK ferme la connexion sans créer une copie retry supplémentaire ; une
redelivery éventuelle reste couverte par l'idempotence du rendu, du stockage et
des callbacks.
L’URL doit être une URL HTTP(S) sans credentials, query, fragment ni chemin
ambigu. Elle exige HTTPS hors `localhost`/loopback afin que le token ne transite
jamais en clair. Le client n’utilise aucun proxy d’environnement, refuse toutes les
redirections sans relayer le token, borne le timeout à 1–30 secondes, ferme la
réponse et n’accepte qu’un statut 2xx. Les erreurs exposées restent génériques.

En mode `local`, `DOCUMENT_OUTPUT_DIR` et `DOCUMENTS_BASE_PATH` de Harmony
doivent désigner le même volume monté. En mode `s3`, le worker écrit le PDF
dans le même bucket MinIO/S3 que Harmony avec le préfixe
`DOCUMENT_STORAGE_PREFIX` ; le téléchargement reste ensuite contrôlé par
Harmony, pas par une URL publique de stockage. L’upload emploie
`If-None-Match: *`, transmet le snapshot déjà vérifié plutôt que de rouvrir le
chemin, puis relit et rehache l’objet exact sans faire confiance à l’ETag. Les
octets locaux de staging sont supprimés avant le callback de succès : une panne
de nettoyage ne peut donc pas survenir après que Harmony a marqué le job
`done`. Les
timeouts et tentatives S3 sont bornés par `S3_CONNECT_TIMEOUT_SECONDS`,
`S3_READ_TIMEOUT_SECONDS` et `S3_MAX_ATTEMPTS`. Activer le versioning du bucket
en production permet à Harmony de relire le `VersionId` exact ; sans versioning,
un remplacement ultérieur reste détecté et rend le téléchargement indisponible
plutôt que de servir d’autres octets.

Une preuve MinIO locale et isolée exerce les vraies implémentations worker et
Harmony avec le versioning actif : création conditionnelle, rejeu identique,
collision d'octets, préfixe physique, taille/SHA-256/`VersionId`, altération de
la version courante et fermeture des flux GET. Elle crée un conteneur, un
réseau, un volume et un bucket aux noms jetables, puis les supprime même en cas
d'échec :

```sh
docker build -t tryscode/harmony:artifact-dev ../../FW_Harmony
sh scripts/run_minio_artifact_proof.sh
```

Le script refuse un endpoint non local et des noms ou identifiants qui ne sont
pas réservés à cette preuve. L'image Harmony de preuve peut être remplacée avec
`PROOF_IMAGE`. Cette exécution valide le contrat S3 sur
MinIO ; elle ne prouve ni les ACL, ni la sauvegarde, ni la restauration, ni le
réseau d'un environnement de recette.

Depuis la racine du dépôt, installer le lock et le package :

```sh
make install
```

Configurer [.env.example](.env.example), puis lancer depuis la racine :

```sh
make run-worker
```

Construction de l'image depuis la racine du dépôt :

```sh
docker build --file worker/Dockerfile --tag tryscode/docgenerator-worker:dev worker
```
