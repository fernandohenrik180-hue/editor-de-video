#!/usr/bin/env bash
# Instala as dependências do editor (Linux/macOS).
set -e
if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then brew install ffmpeg
  elif command -v apt-get >/dev/null; then sudo apt-get update && sudo apt-get install -y ffmpeg fonts-inter
  else echo "Instale o ffmpeg manualmente: https://ffmpeg.org/download.html"; exit 1; fi
fi
# transcrição automática (legendas)
pip install faster-whisper || pip install --break-system-packages faster-whisper
echo "OK. Teste com: python3 ferramentas/editor.py --help"
