#!/bin/bash
set -e

usage() {
  echo "Usage: bump-server.sh <version>"
  echo "       bump-server.sh increment <major|minor|patch>"
  echo "Examples:"
  echo "  bump-server.sh 0.3.0"
  echo "  bump-server.sh increment minor"
}

if [ -z "$1" ]; then
  usage
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKERFILE="$ROOT/drift-beacon/Dockerfile"
CONFIG_FILE="$ROOT/drift-beacon/config.yaml"

if [ "$1" = "increment" ]; then
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
    major)
      VERSION="$((MAJOR + 1)).0.0"
      ;;
    minor)
      VERSION="${MAJOR}.$((MINOR + 1)).0"
      ;;
    patch)
      VERSION="${MAJOR}.${MINOR}.$((PATCH + 1))"
      ;;
    *)
      echo "Invalid increment part: $2"
      usage
      exit 1
      ;;
  esac
else
  VERSION="$1"
fi

IMAGE="ghcr.io/rshallam/drift-beacon-server:${VERSION}"

# Update Dockerfile
sed -i '' "s|ARG SERVER_IMAGE=ghcr.io/rshallam/drift-beacon-server:.*|ARG SERVER_IMAGE=${IMAGE}|" "$DOCKERFILE"

# Update config.yaml version
sed -i '' "s|^version: \".*\"|version: \"${VERSION}\"|" "$CONFIG_FILE"

echo "Bumped server to ${VERSION}"
grep --color=always -n "drift-beacon-server\|^version:" "$DOCKERFILE" "$CONFIG_FILE"
