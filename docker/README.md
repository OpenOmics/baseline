## About

The steps below show how to build a Docker image locally from the provided Dockerfile, tag the image, and push it to DockerHub. The image is built for the `linux/amd64` platform, which makes it compatible with both M-series Apple Silicon (i.e ARM-based) and Intel-based (i.e x86_64) Macs.

> [!NOTE]  
> Replace `skchronicles` with your own DockerHub username.

### Steps for Building Docker Images

```bash
# See listing of images on computer
docker image ls

# Build from Dockerfile
docker buildx build --platform linux/amd64 --load --no-cache -f Dockerfile --tag=example:v0.1.0 .

# Testing, take a peek inside
docker run --platform linux/amd64 -ti example:v0.1.0 /bin/bash

# Updating Tags before pushing to DockerHub
docker tag example:v0.1.0 skchronicles/example:v0.1.0
docker tag example:v0.1.0 skchronicles/example:latest

# Check out new tags
docker image ls

# Push new tagged image to DockerHub
docker push skchronicles/example:v0.1.0
docker push skchronicles/example:latest
```

### Other Recommended Steps

Scan your image for known vulnerabilities:

```bash
docker scout cves example:v0.1.0
```
