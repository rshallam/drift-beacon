#!/bin/bash
set -e

usage() {
  echo "Usage: bump-server.sh set|s <version>"
  echo "       bump-server.sh increment|inc|i <major|maj|minor|min|patch|p>"
  echo "Examples:"
  echo "  bump-server.sh set 0.3.0"
  echo "  bump-server.sh increment minor"
  echo "  bump-server.sh i min"
}

if [ -z "$1" ]; then
  usage
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKERFILE="$ROOT/drift-beacon/Dockerfile"
CONFIG_FILE="$ROOT/drift-beacon/config.yaml"
INTEGRATION_MANIFEST="$ROOT/custom_components/drift_beacon/manifest.json"

case "$1" in
  increment|inc|i) COMMAND="increment" ;;
  set|s) COMMAND="set" ;;
  *)
    echo "Unknown command: $1"
    usage
    exit 1
    ;;
esac

if [ "$COMMAND" = "increment" ]; then
  if [ -z "$2" ]; then
    echo "Missing increment part."
    usage
    exit 1
  fi

  CURRENT_VERSION="$(sed -n 's|^ARG SERVER_IMAGE=ghcr\.io/rshallam/drift-beacon-server:\(.*\)$|\1|p' "$DOCKERFILE")"

  if ! [[ "$CURRENT_VERSION" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    echo "Current server version must be MAJOR.MINOR.PATCH, got: ${CURRENT_VERSION:-<empty>}"
    exit 1
  fi

  MAJOR="${BASH_REMATCH[1]}"
  MINOR="${BASH_REMATCH[2]}"
  PATCH="${BASH_REMATCH[3]}"

  case "$2" in
    major|maj)
      VERSION="$((MAJOR + 1)).0.0"
      ;;
    minor|min)
      VERSION="${MAJOR}.$((MINOR + 1)).0"
      ;;
    patch|p)
      VERSION="${MAJOR}.${MINOR}.$((PATCH + 1))"
      ;;
    *)
      echo "Invalid increment part: $2"
      usage
      exit 1
      ;;
  esac
else
  if [ -z "$2" ]; then
    echo "Missing version."
    usage
    exit 1
  fi

  if ! [[ "$2" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Version must be MAJOR.MINOR.PATCH, got: $2"
    exit 1
  fi

  VERSION="$2"
fi

IMAGE="ghcr.io/rshallam/drift-beacon-server:${VERSION}"

# Update Dockerfile
sed -i '' "s|ARG SERVER_IMAGE=ghcr.io/rshallam/drift-beacon-server:.*|ARG SERVER_IMAGE=${IMAGE}|" "$DOCKERFILE"

# Update config.yaml version
sed -i '' "s|^version: \".*\"|version: \"${VERSION}\"|" "$CONFIG_FILE"

# Keep the Home Assistant integration release in lockstep with the add-on server.
sed -i '' "s|  \"version\": \".*\"|  \"version\": \"${VERSION}\"|" "$INTEGRATION_MANIFEST"

echo "Bumped server and Home Assistant integration to ${VERSION}"
grep --color=always -n "drift-beacon-server\|^version:\|\"version\"" \
  "$DOCKERFILE" "$CONFIG_FILE" "$INTEGRATION_MANIFEST"
