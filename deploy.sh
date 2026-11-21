#!/usr/bin/env bash
# =============================================================================
#  Fhemni – déploiement automatique sur Azure (à lancer dans Azure Cloud Shell)
#  Usage :  bash deploy.sh
#  Le script peut être relancé sans risque : il réutilise les mêmes noms.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

RG="rg-fhemni"
LOC="francecentral"
ENV_FILE=".fhemni.env"

# Un suffixe aléatoire garde des noms uniques dans tout Azure ; il est mémorisé pour les relances.
if [ -f "$ENV_FILE" ]; then source "$ENV_FILE"; else
  SUFFIX=$(openssl rand -hex 3 | cut -c1-5)
  PROF_CODE="prof-$(openssl rand -hex 3)"
  echo "SUFFIX=$SUFFIX" > "$ENV_FILE"; echo "PROF_CODE=$PROF_CODE" >> "$ENV_FILE"
fi
ST="stfhemni$SUFFIX"; FUNC="func-fhemni-$SUFFIX"; SPEECH="speech-fhemni-$SUFFIX"
LANG_RES="lang-fhemni-$SUFFIX"; WPS="wps-fhemni-$SUFFIX"

step() { echo; echo "================================================================"; echo " $1"; echo "================================================================"; }
explain() { echo "  -> $1"; }

az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors >/dev/null
az account show --query "{abonnement:name}" -o table

# -----------------------------------------------------------------------------
step "Étape 0 – Groupe de ressources $RG ($LOC)"
explain "Le 'dossier' qui contient tout le projet. Le supprimer à la fin efface tout."
az group create -n "$RG" -l "$LOC" -o none

# -----------------------------------------------------------------------------
step "Étape 1 – Compte de stockage $ST + site web statique"
explain "Blob Storage héberge les pages (conteneur public \$web) et les transcriptions (conteneur privé)."
az storage account create -n "$ST" -g "$RG" -l "$LOC" --sku Standard_LRS --kind StorageV2 \
  --min-tls-version TLS1_2 --allow-blob-public-access false -o none
ST_CONN=$(az storage account show-connection-string -n "$ST" -g "$RG" --query connectionString -o tsv)
az storage blob service-properties update --static-website --index-document index.html \
  --connection-string "$ST_CONN" -o none
SITE_URL=$(az storage account show -n "$ST" -g "$RG" --query primaryEndpoints.web -o tsv)
SITE_ORIGIN=${SITE_URL%/}
explain "Site : $SITE_URL"

# -----------------------------------------------------------------------------
step "Étape 3 – AI Speech $SPEECH (reconnaissance + traduction en direct)"
explain "Offre gratuite F0 : 5 h de traduction par mois. Une seule ressource F0 est permise par abonnement."
if ! az cognitiveservices account show -n "$SPEECH" -g "$RG" -o none 2>/dev/null; then
  az cognitiveservices account create -n "$SPEECH" -g "$RG" -l "$LOC" --kind SpeechServices --sku F0 --yes -o none \
  || { explain "F0 refusé (déjà utilisé ?) : création en S0 (payant à l'usage, quelques centimes pour des tests)."; \
       az cognitiveservices account create -n "$SPEECH" -g "$RG" -l "$LOC" --kind SpeechServices --sku S0 --yes -o none; }
fi
SPEECH_KEY=$(az cognitiveservices account keys list -n "$SPEECH" -g "$RG" --query key1 -o tsv)

# -----------------------------------------------------------------------------
step "Étape 6 – AI Language $LANG_RES (résumé du cours)"
if ! az cognitiveservices account show -n "$LANG_RES" -g "$RG" -o none 2>/dev/null; then
  az cognitiveservices account create -n "$LANG_RES" -g "$RG" -l "$LOC" --kind TextAnalytics --sku F0 --yes -o none \
  || { explain "F0 refusé : création en S."; \
       az cognitiveservices account create -n "$LANG_RES" -g "$RG" -l "$LOC" --kind TextAnalytics --sku S --yes -o none; }
fi
LANG_KEY=$(az cognitiveservices account keys list -n "$LANG_RES" -g "$RG" --query key1 -o tsv)
LANG_ENDPOINT=$(az cognitiveservices account show -n "$LANG_RES" -g "$RG" --query properties.endpoint -o tsv)

# -----------------------------------------------------------------------------
step "Étape 4 – Web PubSub $WPS (diffusion en temps réel vers les téléphones)"
explain "Offre Free : 20 connexions simultanées, 20 000 messages par jour."
if ! az webpubsub show -n "$WPS" -g "$RG" -o none 2>/dev/null; then
  az webpubsub create -n "$WPS" -g "$RG" -l "$LOC" --sku Free_F1 -o none
fi
WPS_CONN=$(az webpubsub key show -n "$WPS" -g "$RG" --query primaryConnectionString -o tsv)

# -----------------------------------------------------------------------------
step "Étape 2 – Function App $FUNC (le backend)"
explain "Plan serverless : on ne paie qu'à l'exécution. Essai en Flex Consumption, sinon Consumption."
if ! az functionapp show -n "$FUNC" -g "$RG" -o none 2>/dev/null; then
  az functionapp create -n "$FUNC" -g "$RG" --storage-account "$ST" \
     --flexconsumption-location "$LOC" --runtime python --runtime-version 3.11 -o none \
  || az functionapp create -n "$FUNC" -g "$RG" --storage-account "$ST" \
     --consumption-plan-location "$LOC" --os-type Linux --runtime python --runtime-version 3.11 \
     --functions-version 4 -o none
fi

explain "Les clés vont dans les paramètres d'application : jamais dans les pages web."
az functionapp config appsettings set -n "$FUNC" -g "$RG" -o none --settings \
  "PROF_CODE=$PROF_CODE" "SPEECH_KEY=$SPEECH_KEY" "SPEECH_REGION=$LOC" \
  "WEBPUBSUB_CONNECTION=$WPS_CONN" "STORAGE_CONNECTION=$ST_CONN" \
  "LANGUAGE_ENDPOINT=$LANG_ENDPOINT" "LANGUAGE_KEY=$LANG_KEY"

explain "CORS : autoriser le site Blob à appeler les fonctions."
az functionapp cors add -n "$FUNC" -g "$RG" --allowed-origins "$SITE_ORIGIN" -o none 2>/dev/null || true

explain "Publication du code Python (les bibliothèques sont installées côté Azure)."
( cd api
  if command -v func >/dev/null; then
    func azure functionapp publish "$FUNC" --python
  else
    rm -f ../api.zip; zip -qr ../api.zip . -x '.venv/*' '__pycache__/*' 'local.settings*.json'
    az functionapp deployment source config-zip -n "$FUNC" -g "$RG" --src ../api.zip --build-remote true -o none
  fi )
# Sur le plan Flex Consumption, defaultHostName peut être vide : on retombe sur le nom standard.
FUNC_HOST=$(az functionapp show -n "$FUNC" -g "$RG" --query "properties.defaultHostName || defaultHostName" -o tsv 2>/dev/null || true)
[ -n "$FUNC_HOST" ] || FUNC_HOST="$FUNC.azurewebsites.net"
API_URL="https://$FUNC_HOST/api"

# -----------------------------------------------------------------------------
step "Étape 1 (suite) – Publication des pages web"
explain "config.js indique aux pages l'adresse du backend (aucune clé dedans)."
printf '// Adresse du backend Azure Functions. Aucune clé ici : ce fichier est public.\nwindow.FHEMNI_API = "%s";\n' "$API_URL" > web/config.js
az storage blob upload-batch -d '$web' -s web --connection-string "$ST_CONN" --overwrite -o none

# -----------------------------------------------------------------------------
step "Test rapide du backend"
code=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/negotiate?session=test-fhemni" || true)
explain "GET /api/negotiate -> HTTP $code (200 attendu ; si 404, attendez 1 à 2 minutes et relancez le script)"

step "TERMINÉ"
echo "  Site            : $SITE_URL"
echo "  Page professeur : ${SITE_URL}prof.html"
echo "  Page étudiant   : ${SITE_URL}etudiant.html"
echo "  Backend         : $API_URL"
echo "  Code professeur : $PROF_CODE   (à taper sur la page professeur, ne pas publier)"
echo
echo "  Pour tout supprimer après la soutenance :  az group delete -n $RG --yes --no-wait"
