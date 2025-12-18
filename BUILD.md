# Building Satori-Lite Docker Image

## Quick Reference

```bash
./build.sh                  # Build locally
./build.sh dev              # Build :dev locally
./build.sh push             # Push :latest to Docker Hub
./build.sh push dev         # Push :dev
./build.sh push latest dev  # Push multiple tags
```

## Notes

- **Push** builds for both `amd64` (Windows/Intel) and `arm64` (Apple Silicon)
- **Local** builds only for your current platform
- Requires Docker Hub login: `docker login`
