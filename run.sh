#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
echo "👶 Запуск трекера сна Севы (@Sevasleepbot)..."
python main.py
