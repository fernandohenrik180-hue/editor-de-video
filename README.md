# Editor de Vídeo com Claude

Projeto que transforma o Claude Code num editor de vídeo profissional (Reels, TikTok, Shorts, YouTube).
O Claude "assiste" o vídeo (frames + transcrição), decide os cortes, escreve um projeto de edição em JSON e
renderiza com ffmpeg: cortes sem silêncio, 9:16, zoom punch-in, textos, legendas, música com ducking,
áudio em -14 LUFS e capa. Depois revisa o resultado antes de entregar.

## Como usar

1. Instale: `bash ferramentas/instalar.sh` (ffmpeg + faster-whisper)
2. Coloque o vídeo bruto em `entrada/` (e trilhas em `musicas/`)
3. Abra o Claude Code nesta pasta e peça, por exemplo:
   - `/editar entrada/gravacao.mp4 reels de 30s vendendo a mentoria, CTA "chama no direct"`
   - "corta os silêncios e coloca legenda nesse vídeo"
   - "transforma esse vídeo do YouTube em 3 cortes verticais"
4. O vídeo final sai em `saida/`.

Ferramenta manual: `python3 ferramentas/editor.py --help`
(info, frames, silencios, transcrever, renderizar, verificar, capa)
