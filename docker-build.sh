#!/bin/bash

# Mimi-Bro Cursor Agent Docker Build Script
# This script builds the Docker image for the mimi-bro agent

set -e

# Configuration
IMAGE_NAME="cursor-agent"
IMAGE_TAG="latest"

# Get the script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR" && pwd)"

echo "🏗️  Building Mimi-Bro Cursor Agent Docker Image..."
echo "📁 Project root: $PROJECT_ROOT"
echo "📁 Dockerfile: $SCRIPT_DIR/Dockerfile"

# Build Docker image
echo "🐳 Building Docker image: $IMAGE_NAME:$IMAGE_TAG"
docker build -f "$SCRIPT_DIR/Dockerfile" -t "$IMAGE_NAME:$IMAGE_TAG" "$PROJECT_ROOT"

# Tag with version if provided
if [ ! -z "$1" ]; then
    VERSION_TAG="$1"
    docker tag "$IMAGE_NAME:$IMAGE_TAG" "$IMAGE_NAME:$VERSION_TAG"
    echo "🏷️  Tagged image as: $IMAGE_NAME:$VERSION_TAG"
fi

# Clean up dangling images
echo "🧹 Cleaning up dangling Docker images..."
docker image prune -f

echo "✅ Docker image built successfully!"
echo "   Image: $IMAGE_NAME:$IMAGE_TAG"
echo ""
echo "🚀 Run with:"
echo "   docker-compose -f docker-compose.yml up -d"
