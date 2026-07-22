# WSI Thumbnail Viewer

FastAPI app for uploading a whole-slide image and returning a thumbnail plus a short metadata summary.

Supported readers:

- CZI: `aicspylibczi`
- SVS / NDPI / pyramidal TIFF: OpenSlide
- ordinary TIFF fallback: `tifffile`

## Build

```bash
git clone https://github.com/your-github-user/read-wsi.git
cd read-wsi
docker build -t read-wsi:latest .
```

## Run A Local Image

```bash
docker run --rm -p 8000:8000 read-wsi:latest
```

Open:

```text
http://127.0.0.1:8000
```

## Run From Docker Hub

After publishing the image, users can run:

```bash
docker run --rm -p 8000:8000 your-dockerhub-user/read-wsi:latest
```

## Keep Uploaded Files And Outputs

```bash
mkdir -p ./read_wsi_runtime/uploads
mkdir -p ./read_wsi_runtime/outputs

docker run --rm -p 8000:8000 \
  -v "$(pwd)/read_wsi_runtime/uploads:/app/uploads" \
  -v "$(pwd)/read_wsi_runtime/outputs:/app/outputs" \
  read-wsi:latest
```

## Run With Docker Compose

```bash
docker compose up --build
```

This maps:

- `./uploads` -> `/app/uploads`
- `./outputs` -> `/app/outputs`

so uploaded slides and generated thumbnails remain available on the host.

## Health Check

```bash
curl http://127.0.0.1:8000/health
```
