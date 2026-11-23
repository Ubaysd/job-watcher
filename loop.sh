#!/usr/bin/env bash
# Boucle de veille : un passage toutes les 15 min pendant ~5h30, puis le workflow se relance lui-même.
set -u
DURATION=${LOOP_SECONDS:-19800}   # 5h30 (limite GitHub d'un job : 6h)
INTERVAL=${LOOP_INTERVAL:-900}    # 15 min
end=$((SECONDS + DURATION))
n=0
while :; do
  start=$SECONDS
  n=$((n + 1))
  echo "=== Passage $n ($(date -u +%H:%M) UTC) ==="
  python watcher.py || echo "::warning::passage $n en erreur, on continue"

  # sauvegarde de l'état et des offres (seule la boucle écrit ces 2 fichiers)
  git add state.json 2>/dev/null
  [ -f offres.csv ] && git add offres.csv
  if ! git diff --cached --quiet; then
    git commit -q -m "state update" \
      && { git pull -q --rebase origin "${GITHUB_REF_NAME:-main}" && git push -q origin "HEAD:${GITHUB_REF_NAME:-main}"; } \
      || echo "::warning::push en échec au passage $n"
  fi

  next=$((start + INTERVAL))
  [ "$next" -ge "$end" ] && break
  wait_s=$((next - SECONDS))
  [ "$wait_s" -gt 0 ] && sleep "$wait_s"
done
echo "Fin de boucle après $n passages"
