# Dashboard and camera refresh

## Web UI

The camera and sink status appear first. Clips load lightweight thumbnails;
only the selected clip opens a video player. Filters separate tagged and untagged
clips. Household and Camera & setup have their own pages.

The clip browser filters the whole retained library by day, roommate, and tag
status before pagination. Load more is not capped at the first 200 recordings.
Clips are grouped by cooking session, with parts in chronological order. The
review player has previous/next controls, speed settings, optional auto-next,
and N/P/J/L keyboard shortcuts. Saving a tag keeps its original clip target,
even if the dialog is closed while the request is in flight.

Live preview uses authenticated `/live.jpg` responses, avoiding buffered multipart
streams through Authentik/Cloudflare. Requests are sequential, stop in hidden tabs,
retry on failures, and retain the last frame. Frames older than ten seconds are
refused as live. A camera lease expires 45 seconds after the last viewer request.
Closing one viewer does not stop another viewer's lease.

Preview uploads now use the same configured server-side rotation as snapshots;
calibration geometry and the Pi's capture orientation stay unchanged. Rotation
and JPEG encoding happen once per uploaded preview, not for each viewer. The
browser polls at the thermal frame-rate limit and sends the last frame ID; an
unchanged frame returns an empty 204 response while renewing the lease. Frame
IDs include a timestamp so reconnecting after a server restart cannot reuse an
old image merely because its sequence number repeats.

New thumbnails are capped at 480 pixels and are not rotated twice when their
source is an already-oriented detection image. Existing media is untouched.

All media requires dashboard credentials or trusted Authentik identity, regardless
of the viewer's network. Byte ranges support video seeking. Notification attachments
remain disabled; the existing notifications link to the authenticated dashboard.

## Pi performance

Detection stays at its existing camera resolution, keeping ROI geometry intact.
Clips and previews are reduced to 640 pixels wide. The Pi packages its JPEGs as
MJPEG AVI with stream copy; it no longer performs H.264 compression. The server
converts incoming video to upright H.264 with at most two encoder threads, outside
the request loop, with one upload/conversion at a time. Install ffmpeg in the
server image (included in the Dockerfile).

Continuous activity flushes roughly 60-second chunks (`CLIP_CHUNK_SEC`, bounded
to 15–120). Short visits produce shorter clips with a ten-second post-motion
tail. Visits less than three minutes apart share a session ID. Clip-only uploads
do not run sink detection on frames containing a person; the final still does.
Sampling preserves its 5 fps phase, and the container frame rate accounts for
slower capture so playback duration matches recorded elapsed time.

Camera reads continue during packaging and upload. Memory is capped at one
active chunk and one upload job; there is no unbounded queue. A prolonged network
outage can overwrite the oldest frames in the pending ring, and failed uploads
are logged rather than kept indefinitely on the SD card.

The server requests at most 2 preview frames per second by default. The edge caps
preview to 1 fps at 60°C, 0.5 fps at 70°C, and pauses it at 75°C. An unavailable
sensor caps preview at 1 fps. The existing systemd CPU quota stays in force.
Temperature and the preview limit appear in camera health reports.

Failed background jobs release their slot without terminating camera capture.
Encoder jobs own unique temporary files and remove them after upload or failure;
odd-sized frames are normalized to even dimensions for H.264. Invalid temperature
readings use the conservative sensor-unavailable limit.

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
Deploy the new server before the camera: older server versions do not convert
the camera's MJPEG chunks or implement `/camera/clip`.

Videos, thumbnails, and saved captures use a 14-day retention window. The hourly
sweep never traverses calibration, people, configuration, or PC backups. Clip
sidecars carry session/duration metadata and expire with the media.
