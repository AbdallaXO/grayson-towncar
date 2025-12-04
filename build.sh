#!/usr/bin/env bash
# Exit on error
set -o errexit

echo "Ensuring pip exists..."
python3 -m ensurepip --upgrade || true

echo "Upgrading pip..."
pip3 install --upgrade pip || true

echo "Installing project requirements..."
pip3 install -r requirements.txt

echo "Collecting static files..."
python3 manage.py collectstatic --no-input || true

echo "Applying migrations..."
python3 manage.py migrate --no-input || true

echo "Build complete."
