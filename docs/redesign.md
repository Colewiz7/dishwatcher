# Dashboard and camera refresh

## Web UI

The camera and sink status appear first. Clips load lightweight thumbnails;
only the selected clip opens a video player. Filters separate tagged and untagged
clips. Household and Camera & setup have their own pages.

Live preview uses authenticated `/live.jpg` responses, avoiding buffered multipart
streams through Authentik/Cloudflare. Requests are sequential, stop in hidden tabs,
retry on failures, and retain the last frame. Frames older than ten seconds are
refused as live. A camera lease expires 45 seconds after the last viewer request.
Closing one viewer does not stop another viewer's lease.

All media requires dashboard credentials or trusted Authentik identity, regardless
of the viewer's network. Byte ranges support video seeking. Notification attachments
remain disabled; the existing notifications link to the authenticated dashboard.

## Pi performance

Detection stays at its existing camera resolution, keeping ROI geometry intact.
Clips and previews are reduced to 640 pixels wide. H.264 uses one encoder thread.
Clip encoding/upload and live preview each have a single background job with no
unbounded queue. Camera reads continue while a clip is encoded. One pending sink
visit stays in the bounded camera ring until the encoder is available.

The server requests at most 2 preview frames per second by default. The edge caps
preview to 1 fps at 60°C, 0.5 fps at 70°C, and pauses it at 75°C. An unavailable
sensor caps preview at 1 fps. The existing systemd CPU quota stays in force.
Temperature and the preview limit appear in camera health reports.

Server image processing and notification I/O run outside the async request loop.
Uploads remain serialized so state updates preserve order. Clean calibration now
uses the latest raw frame, not a JPEG with detection boxes burned into it. Existing
calibration is preserved. After a server restart, saving a new reference waits for
the next camera upload.

## Backups and deployment

Run `bash scripts/backup-local.sh` before deployment. The maintained source copy is
`/home/cole/Projects/dishwatcher`, with dated snapshots in the adjacent
`dishwatcher-backups` folder. Store the corresponding GitOps manifests in that backup.

Build the server image with the repository's GitHub Actions workflow, then pin
its immutable SHA tag in `homelab-gitops/apps/dishwatcher/deployment.yaml`.
Argo CD performs the rollout. Roll back by restoring the prior image tag in GitOps.

The Pi uses the `dishwatcher-edge` systemd service under `/home/cole/dishwatcher/camera`.
Back up its source before copying changed camera modules. Preserve `.env` and restart
the service; verify frames, health reports, clips, preview, temperature and throttling.
