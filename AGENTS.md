# Dishwatcher maintenance

- Keep a complete local source copy in `/home/cole/Projects/dishwatcher`.
- Before every deployment, run `bash scripts/backup-local.sh`. It writes a new
  dated snapshot under `/home/cole/Projects/dishwatcher-backups`; keep older copies.
- Save deployment manifests there too when updating `homelab-gitops`.
- Deploy the web server through `Colewiz7/homelab-gitops`, `apps/dishwatcher`.
  Argo CD reconciles that directory; edits only to the running pod will be lost.
- The camera is a separate service on `dishwatcher-pi`. Back up its current
  source before replacing it. Preserve its `.env` and calibration geometry.
- Pi 3B+ performance matters: keep work queues bounded, preview frames small,
  and honor thermal limits. Check actual temperature after camera changes.
- Keep authentication on all dashboard/media routes. Signed-in remote users
  must be able to watch clips and live preview through `sink.colewiz.dev`.
