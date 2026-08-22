# Installation on Debian 13

This guide installs the Shot Counter dashboard and audio processor under a dedicated unprivileged account. The example paths are:

```text
Application:       /opt/shot-counter
Configuration:     /opt/shot-counter/config.yaml
SQLite database:   /var/lib/shot-counter/shots.db
Incoming audio:    /srv/shot-counter/incoming
Processed audio:   /srv/shot-counter/processed
Failed audio:      /srv/shot-counter/failed
Temporary work:    /var/cache/shot-counter
Dashboard:         http://127.0.0.1:8080
```

Run the administrative commands below from an account with `sudo` access.

## 1. Install operating-system packages

```bash
sudo apt update
sudo apt install -y \
  git \
  python3 \
  python3-venv \
  ffmpeg \
  libsndfile1 \
  nginx
```

Nginx is optional if the dashboard will only be reached locally or through a different reverse proxy.

Verify the audio tools:

```bash
ffmpeg -version
ffprobe -version
```

## 2. Create the service account and directories

```bash
sudo adduser \
  --system \
  --group \
  --home /opt/shot-counter \
  shot-counter

sudo install -d -o shot-counter -g shot-counter /opt/shot-counter
sudo install -d -o shot-counter -g shot-counter /var/lib/shot-counter
sudo install -d -o shot-counter -g shot-counter /var/cache/shot-counter
sudo install -d -o shot-counter -g shot-counter /srv/shot-counter/incoming
sudo install -d -o shot-counter -g shot-counter /srv/shot-counter/processed
sudo install -d -o shot-counter -g shot-counter /srv/shot-counter/failed
```

If `shot-counter` already exists, `adduser` will report that and you can continue after verifying the account:

```bash
getent passwd shot-counter
```

## 3. Download the application

The repository is private, so authenticate with GitHub using your normal Git credentials or an SSH deploy key. Do not put an access token in shell history or in this repository.

```bash
sudo git clone \
  https://github.com/G33kM0bile/Shot-counter.git \
  /opt/shot-counter

sudo chown -R shot-counter:shot-counter /opt/shot-counter
```

If the target directory was created in step 2 and Git refuses to clone into it, clone into a temporary empty directory and copy the checkout into `/opt/shot-counter`, or remove only the empty target after verifying it contains no data.

## 4. Create the Python environment

```bash
sudo -u shot-counter \
  python3 -m venv /opt/shot-counter/venv

sudo -u shot-counter \
  /opt/shot-counter/venv/bin/pip install --upgrade pip

sudo -u shot-counter \
  /opt/shot-counter/venv/bin/pip install \
  -r /opt/shot-counter/requirements.txt
```

## 5. Create the configuration

```bash
sudo cp \
  /opt/shot-counter/config.example.yaml \
  /opt/shot-counter/config.yaml

sudo chown root:shot-counter /opt/shot-counter/config.yaml
sudo chmod 640 /opt/shot-counter/config.yaml
sudoedit /opt/shot-counter/config.yaml
```

At minimum, review:

- `timezone`
- `detector.name`
- `detector.range`
- `detector.mode`
- all database and upload paths
- the audio detection parameters under `processing`

Start with `detector.mode: uploaded` for real recordings. The defaults reproduce the original proof-of-concept detector and are not universal calibration values.

## 6. Perform a local application test

Run the dashboard temporarily as the service user:

```bash
sudo -u shot-counter \
  env SHOT_COUNTER_CONFIG=/opt/shot-counter/config.yaml \
  /opt/shot-counter/venv/bin/python \
  /opt/shot-counter/app.py
```

From another terminal:

```bash
curl http://127.0.0.1:8080/api/status
```

Stop the temporary process with `Ctrl+C` before enabling systemd.

## 7. Install and start the systemd services

```bash
sudo install -m 644 \
  /opt/shot-counter/deploy/systemd/shot-counter.service \
  /etc/systemd/system/shot-counter.service

sudo install -m 644 \
  /opt/shot-counter/deploy/systemd/shot-counter-upload-processor.service \
  /etc/systemd/system/shot-counter-upload-processor.service

sudo systemctl daemon-reload
sudo systemctl enable --now shot-counter.service
sudo systemctl enable --now shot-counter-upload-processor.service
```

Verify both services:

```bash
sudo systemctl status shot-counter.service
sudo systemctl status shot-counter-upload-processor.service
```

Follow logs:

```bash
sudo journalctl -u shot-counter.service -f
sudo journalctl -u shot-counter-upload-processor.service -f
```

## 8. Test an audio upload

The browser form can upload WAV, M4A, MP3, and AAC files. A command-line test is:

```bash
curl -f \
  -F 'audio=@/path/to/test-recording.m4a' \
  http://127.0.0.1:8080/api/upload
```

The request should return HTTP `202`. Watch the processor log and confirm the original moves to either `processed/` or `failed/`:

```bash
find /srv/shot-counter/processed -maxdepth 1 -type f -ls
find /srv/shot-counter/failed -maxdepth 1 -type f -ls
```

Check the database and dashboard:

```bash
sqlite3 /var/lib/shot-counter/shots.db \
  'SELECT COUNT(*) FROM shots;'

curl http://127.0.0.1:8080/api/status
```

Install `sqlite3` with `sudo apt install sqlite3` if that diagnostic command is needed.

## 9. Optional Nginx reverse proxy

Copy the example and replace `shot-counter.example.org` with the intended hostname:

```bash
sudo cp \
  /opt/shot-counter/deploy/nginx/shot-counter.conf.example \
  /etc/nginx/sites-available/shot-counter

sudoedit /etc/nginx/sites-available/shot-counter

sudo ln -s \
  /etc/nginx/sites-available/shot-counter \
  /etc/nginx/sites-enabled/shot-counter

sudo nginx -t
sudo systemctl reload nginx
```

Add TLS before public use. If the range router cannot be changed, use an outbound Cloudflare Tunnel for public access or Tailscale for private administration. No inbound port forwarding is required for either approach.

## 10. Public-access hardening

Before publishing the site, add authentication or an access gateway to:

- `POST /api/upload`
- `POST /api/privacy-reset`
- any future administrative endpoint

Also configure rate limits, upload quotas, TLS, log retention, and an audio retention policy. The included bot policies discourage indexing but do not make a site private.

## Updating the installation

Back up configuration and SQLite first:

```bash
sudo systemctl stop shot-counter-upload-processor.service
sudo systemctl stop shot-counter.service

sudo cp -a \
  /var/lib/shot-counter/shots.db \
  /var/lib/shot-counter/shots.db.backup
```

Then update the checkout and dependencies:

```bash
cd /opt/shot-counter
sudo -u shot-counter git pull --ff-only
sudo -u shot-counter \
  /opt/shot-counter/venv/bin/pip install \
  -r requirements.txt

sudo systemctl daemon-reload
sudo systemctl start shot-counter.service
sudo systemctl start shot-counter-upload-processor.service
```

Remove the backup only after the updated services and data have been verified.

## Troubleshooting

### Dashboard does not start

```bash
sudo journalctl -u shot-counter.service -n 100 --no-pager
sudo ss -ltnp | grep 8080
sudo -u shot-counter test -r /opt/shot-counter/config.yaml
sudo -u shot-counter test -w /var/lib/shot-counter
```

### Uploaded files remain in `incoming/`

```bash
sudo journalctl \
  -u shot-counter-upload-processor.service \
  -n 100 --no-pager

sudo -u shot-counter test -w /srv/shot-counter/incoming
sudo -u shot-counter test -w /srv/shot-counter/processed
sudo -u shot-counter test -w /srv/shot-counter/failed
ffprobe /srv/shot-counter/incoming/example.m4a
```

### Incorrect counts

Keep the source recording and note the real shot count. Compare the logged threshold and detected offsets before changing parameters. A threshold that fixes one 9 mm sample can cause missed detections for quieter firearms such as .22 LR.

