# WSI Thumbnail Viewer
<img width="1032" height="554" alt="Snipaste_2026-07-22_13-49-05" src="https://github.com/user-attachments/assets/81d82222-a602-41c9-a7d4-1effc906dc2c" />

FastAPI app for uploading a whole-slide image and returning a thumbnail plus a short metadata summary.

Supported readers:

- CZI: `aicspylibczi`
- SVS / NDPI / pyramidal TIFF: OpenSlide
- ordinary TIFF fallback: `tifffile`


## Installation

Clone the repository:

```bash
git clone https://github.com/anxuanhan/read_wsi.git
cd read_wsi
```

> **Important:** Make sure Docker is running before building the image.

### Build the Docker image

#### macOS with Apple Silicon

```bash
docker build --platform linux/amd64 -t read-wsi:latest .
```

#### Other platforms

```bash
docker build -t read-wsi:latest .
```

### Run the application

```bash
docker run --rm -p 8000:8000 read-wsi:latest
```

Open the following address in your browser:

```text
http://localhost:8000
```

To stop the application, press `Ctrl+C` in the terminal.






## Upload Your Image
Open the web page, choose or drag a WSI file, and click Upload.
<img width="1072" height="648" alt="Snipaste_2026-07-22_13-49-55" src="https://github.com/user-attachments/assets/468038ee-bd3d-4a4e-b753-75c756153df0" />

<img width="1039" height="694" alt="Snipaste_2026-07-22_13-50-14" src="https://github.com/user-attachments/assets/9724083d-adc8-4b21-9124-2cd1e423f0a6" />

