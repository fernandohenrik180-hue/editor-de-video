#!/usr/bin/env python3
"""Editor de vídeo por linha de comando (ffmpeg) usado pelo Claude.

Fluxo principal: o Claude escreve um projeto JSON (lista de decisões de edição)
e roda `python3 ferramentas/editor.py renderizar projetos/x.json`.

Subcomandos:
  info ARQ                      metadados (resolução, fps, duração, áudio)
  frames ARQ [--cada 2]         extrai frames para o Claude olhar
  silencios ARQ                 lista silêncios e sugere trechos a manter (JSON)
  transcrever ARQ [--modelo small]  gera .srt com faster-whisper (pt)
  renderizar PROJETO.json       monta o vídeo final a partir do projeto
  verificar ARQ                 confere o arquivo final e extrai frames de checagem
  capa ARQ --tempo 3            exporta um frame como capa .jpg
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
TRABALHO = RAIZ / "trabalho"
FONTE_BOLD = "/usr/share/fonts/opentype/inter/Inter-Bold.otf"
FONTE_BLACK = "/usr/share/fonts/opentype/inter/Inter-Black.otf"

FORMATOS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}

EXPORT_V = ["-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-pix_fmt", "yuv420p", "-profile:v", "high"]
EXPORT_A = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


# ---------------------------------------------------------------- utilidades
def rodar(cmd, silencioso=False):
    if not silencioso:
        print("▶", " ".join(str(c) for c in cmd[:6]), "...", file=sys.stderr)
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit(f"Falhou: {cmd[0]} (código {r.returncode})")
    return r


def sonda(arq):
    r = rodar(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", arq], silencioso=True)
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    info = {"arquivo": str(arq), "duracao": round(float(d["format"]["duration"]), 3),
            "tem_audio": a is not None}
    if v:
        num, den = v["r_frame_rate"].split("/")
        rot = 0
        for sd in v.get("side_data_list", []):
            rot = int(sd.get("rotation", rot) or 0)
        rot = int(v.get("tags", {}).get("rotate", rot) or 0)
        w, h = v["width"], v["height"]
        if abs(rot) in (90, 270):
            w, h = h, w
        info.update({"largura": w, "altura": h, "fps": round(int(num) / int(den), 3),
                     "codec_video": v["codec_name"]})
    if a:
        info.update({"codec_audio": a["codec_name"], "sample_rate": int(a["sample_rate"])})
    return info


def esc_filtro(caminho):
    """Escapa um caminho para uso dentro de um filtro do ffmpeg."""
    return str(caminho).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def ts(seg):
    h, rest = divmod(seg, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


# ---------------------------------------------------------------- comandos
def cmd_info(a):
    print(json.dumps(sonda(a.arquivo), indent=2, ensure_ascii=False))


def cmd_frames(a):
    nome = Path(a.arquivo).stem
    pasta = TRABALHO / f"frames_{nome}"
    shutil.rmtree(pasta, ignore_errors=True)
    pasta.mkdir(parents=True)
    rodar(["ffmpeg", "-v", "error", "-i", a.arquivo, "-vf",
           f"fps=1/{a.cada},scale=360:-2", "-q:v", "4", pasta / "f_%03d.jpg"])
    arqs = sorted(pasta.glob("*.jpg"))
    for i, f in enumerate(arqs):
        print(f"{f}  (~{i * a.cada:.1f}s)")


def detectar_silencios(arq, ruido, dur_min):
    r = subprocess.run(["ffmpeg", "-i", arq, "-af",
                        f"silencedetect=noise={ruido}dB:d={dur_min}", "-f", "null", "-"],
                       capture_output=True, text=True)
    inicios = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    fins = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return list(zip(inicios, fins + [None] * (len(inicios) - len(fins))))


def cmd_silencios(a):
    info = sonda(a.arquivo)
    dur = info["duracao"]
    sil = detectar_silencios(a.arquivo, a.ruido, a.minimo)
    manter, cursor = [], 0.0
    for ini, fim in sil:
        fim = dur if fim is None else fim
        corte_ini = ini + a.respiro
        if corte_ini - cursor > 0.15:
            manter.append({"arquivo": a.arquivo, "inicio": round(cursor, 3),
                           "fim": round(min(corte_ini, dur), 3)})
        cursor = max(fim - a.respiro, 0)
    if dur - cursor > 0.15:
        manter.append({"arquivo": a.arquivo, "inicio": round(cursor, 3), "fim": round(dur, 3)})
    total = sum(c["fim"] - c["inicio"] for c in manter)
    print(json.dumps({"silencios": [[round(i, 2), round(f or dur, 2)] for i, f in sil],
                      "clipes_sugeridos": manter,
                      "duracao_original": dur, "duracao_sem_silencios": round(total, 2)},
                     indent=2, ensure_ascii=False))


def cmd_transcrever(a):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("faster-whisper não instalado. Rode: bash ferramentas/instalar.sh")
    modelo = WhisperModel(a.modelo, device="cpu", compute_type="int8")
    segs, _ = modelo.transcribe(a.arquivo, language=a.idioma, word_timestamps=True,
                                vad_filter=True)
    palavras = [w for s in segs for w in (s.words or [])]
    blocos, atual = [], []
    for w in palavras:
        texto = " ".join(x.word.strip() for x in atual + [w])
        if atual and (len(texto) > a.max_chars or w.end - atual[0].start > 2.8
                      or atual[-1].word.strip().endswith((".", "?", "!"))):
            blocos.append(atual)
            atual = []
        atual.append(w)
    if atual:
        blocos.append(atual)
    saida = Path(a.saida or TRABALHO / f"{Path(a.arquivo).stem}.srt")
    with open(saida, "w", encoding="utf-8") as f:
        for i, b in enumerate(blocos, 1):
            txt = " ".join(x.word.strip() for x in b)
            f.write(f"{i}\n{ts(b[0].start)} --> {ts(b[-1].end)}\n{txt}\n\n")
    print(saida)


# ---------------------------------------------------------------- renderização
def preparar_clipe(c, i, W, H, fps, enquadramento):
    """Corta, reenquadra e normaliza um clipe para o formato final."""
    arq = c["arquivo"]
    info = sonda(arq)
    ini, fim = float(c.get("inicio", 0)), float(c.get("fim", info["duracao"]))
    vel = float(c.get("velocidade", 1.0))
    modo = c.get("enquadramento", enquadramento)
    zoom = float(c.get("zoom", 1.0))  # "punch-in" para variar o plano

    if modo == "blur":
        v = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,"
             f"crop={W}:{H},boxblur=40:6,eq=brightness=-0.08[bg];"
             f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    elif modo == "ajustar":  # barras pretas
        v = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black")
    else:  # crop (preenche a tela); foco_x 0..1 escolhe a área horizontal
        fx = float(c.get("foco_x", 0.5))
        fy = float(c.get("foco_y", 0.5))
        v = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
             f"crop={W}:{H}:(iw-{W})*{fx}:(ih-{H})*{fy}")
    if zoom != 1.0:
        v += f",scale=iw*{zoom}:-2,crop={W}:{H}"
    v += f",setsar=1,fps={fps},setpts=(PTS-STARTPTS)/{vel}[v]"

    if info["tem_audio"]:
        at = []
        r = vel
        while r > 2.0:
            at.append("atempo=2.0"); r /= 2.0
        while r < 0.5:
            at.append("atempo=0.5"); r /= 0.5
        at.append(f"atempo={r}")
        aud = f"[0:a]aresample=48000,asetpts=PTS-STARTPTS,{','.join(at)}[a]"
        mapa_a = ["-map", "[a]"]
        entradas = ["-ss", ini, "-to", fim, "-i", arq]
    else:
        aud = None
        mapa_a = ["-map", "1:a"]
        entradas = ["-ss", ini, "-to", fim, "-i", arq,
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    fc = v if not info["tem_audio"] else f"{v};{aud}"
    out = TRABALHO / f"clipe_{i:03d}.mp4"
    rodar(["ffmpeg", "-y", "-v", "error", *entradas, "-filter_complex", fc,
           "-map", "[v]", *mapa_a, "-shortest", *EXPORT_V, "-r", fps, *EXPORT_A, out])
    return out


def construir_drawtext(t, i, W, H):
    txt_arq = TRABALHO / f"texto_{i:02d}.txt"
    texto = t["texto"]
    limpo = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", texto).strip()
    if limpo != texto:
        print(f"⚠ emoji removido do texto {i} (a fonte não suporta): {texto!r}", file=sys.stderr)
    txt_arq.write_text(limpo, encoding="utf-8")
    tam = int(t.get("tamanho", round(W * 0.072)))
    pos = t.get("posicao", "topo")
    # áreas seguras: topo ~ 16% da altura, baixo acima de ~22%
    y = {"topo": f"{H * 0.16:.0f}", "centro": "(h-text_h)/2",
         "baixo": f"h-text_h-{H * 0.24:.0f}"}.get(pos, pos)
    fonte = FONTE_BLACK if t.get("estilo") == "forte" and os.path.exists(FONTE_BLACK) else FONTE_BOLD
    partes = [f"fontfile={esc_filtro(fonte)}", f"textfile={esc_filtro(txt_arq)}",
              f"fontsize={tam}", f"fontcolor={t.get('cor', 'white')}",
              "line_spacing=10", "text_align=C", "x=(w-text_w)/2", f"y={y}",
              f"enable='between(t,{t['inicio']},{t['fim']})'"]
    if t.get("caixa", True):
        partes += [f"box=1", f"boxcolor={t.get('cor_caixa', 'black@0.65')}",
                   f"boxborderw={int(tam * 0.35)}"]
    else:
        partes += ["borderw=6", "bordercolor=black"]
    return "drawtext=" + ":".join(partes)


def cmd_renderizar(a):
    proj = json.loads(Path(a.projeto).read_text(encoding="utf-8"))
    os.chdir(RAIZ)
    TRABALHO.mkdir(exist_ok=True)
    W, H = FORMATOS[proj.get("formato", "9:16")]
    fps = int(proj.get("fps", 30))
    enq = proj.get("enquadramento", "crop")

    # 1) clipes normalizados
    clipes = [preparar_clipe(c, i, W, H, fps, enq) for i, c in enumerate(proj["clipes"])]
    lista = TRABALHO / "lista_concat.txt"
    lista.write_text("".join(f"file '{c.resolve()}'\n" for c in clipes))
    base = TRABALHO / "montagem.mp4"
    rodar(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lista,
           "-c", "copy", base])
    dur = sonda(base)["duracao"]

    # 2) acabamento: textos, legenda, fades, música, loudnorm
    vf = []
    for i, t in enumerate(proj.get("textos", [])):
        vf.append(construir_drawtext(t, i, W, H))
    leg = proj.get("legenda")
    if leg:
        estilo = leg if isinstance(leg, dict) else {"arquivo": leg}
        # unidades do libass para .srt: altura virtual = 288
        margem = int(estilo.get("margem", 70 if H > W else 20))
        fs = estilo.get("tamanho", 11 if H > W else 16)
        cor = estilo.get("cor_destaque", "&H00FFFFFF")
        force = (f"FontName=Inter,FontSize={fs},Bold=1,PrimaryColour={cor},"
                 f"OutlineColour=&H00000000,BackColour=&H80000000,BorderStyle=1,"
                 f"Outline=2,Shadow=1,Alignment=2,MarginV={margem}")
        vf.append(f"subtitles={esc_filtro(estilo['arquivo'])}:force_style='{force}'")
    fade = proj.get("fade", {"entrada": 0, "saida": 0.4})
    if fade.get("entrada"):
        vf.append(f"fade=t=in:st=0:d={fade['entrada']}")
    if fade.get("saida"):
        vf.append(f"fade=t=out:st={max(dur - fade['saida'], 0):.3f}:d={fade['saida']}")

    entradas = ["-i", base]
    musica = proj.get("musica")
    if musica:
        entradas += ["-stream_loop", "-1", "-i", musica["arquivo"]]
        vol = musica.get("volume", 0.18)
        if musica.get("ducking", True):
            af = (f"[1:a]volume={vol},afade=t=out:st={max(dur - 1.5, 0):.3f}:d=1.5[m];"
                  f"[0:a]asplit[voz][sc];[m][sc]sidechaincompress=threshold=0.02:ratio=10:"
                  f"attack=20:release=400[duck];[voz][duck]amix=inputs=2:duration=first:normalize=0")
        else:
            af = (f"[1:a]volume={vol},afade=t=out:st={max(dur - 1.5, 0):.3f}:d=1.5[m];"
                  f"[0:a][m]amix=inputs=2:duration=first:normalize=0")
    else:
        af = "[0:a]anull"
    if proj.get("normalizar_audio", True):
        af += ",loudnorm=I=-14:TP=-1.5:LRA=11"
    if fade.get("saida"):
        af += f",afade=t=out:st={max(dur - fade['saida'], 0):.3f}:d={fade['saida']}"
    af += "[a]"
    fc = f"[0:v]{','.join(vf) or 'null'}[v];{af}"

    saida = Path(proj.get("saida", "saida/final.mp4"))
    saida.parent.mkdir(parents=True, exist_ok=True)
    rodar(["ffmpeg", "-y", "-v", "error", *entradas, "-filter_complex", fc,
           "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", *EXPORT_V, "-r", fps,
           *EXPORT_A, "-movflags", "+faststart", saida])
    print(json.dumps(sonda(saida), indent=2, ensure_ascii=False))


def cmd_verificar(a):
    info = sonda(a.arquivo)
    print(json.dumps(info, indent=2, ensure_ascii=False))
    pasta = TRABALHO / "checagem"
    shutil.rmtree(pasta, ignore_errors=True)
    pasta.mkdir(parents=True)
    n = 6
    for k in range(n):
        t = info["duracao"] * (k + 0.5) / n
        rodar(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", a.arquivo, "-frames:v", "1",
               "-vf", "scale=540:-2", pasta / f"check_{k}_{t:05.1f}s.jpg"], silencioso=True)
    r = subprocess.run(["ffmpeg", "-i", a.arquivo, "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    if m:
        print(f"Loudness integrado: {m[-1]} LUFS (alvo redes: -14)")
    for f in sorted(pasta.glob("*.jpg")):
        print(f)


def cmd_capa(a):
    saida = a.saida or str(Path("saida") / f"{Path(a.arquivo).stem}_capa.jpg")
    rodar(["ffmpeg", "-y", "-v", "error", "-ss", str(a.tempo), "-i", a.arquivo,
           "-frames:v", "1", "-q:v", "2", saida])
    print(saida)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("info"); s.add_argument("arquivo"); s.set_defaults(f=cmd_info)
    s = sp.add_parser("frames"); s.add_argument("arquivo"); s.add_argument("--cada", type=float, default=2)
    s.set_defaults(f=cmd_frames)
    s = sp.add_parser("silencios"); s.add_argument("arquivo")
    s.add_argument("--ruido", type=float, default=-35); s.add_argument("--minimo", type=float, default=0.45)
    s.add_argument("--respiro", type=float, default=0.12); s.set_defaults(f=cmd_silencios)
    s = sp.add_parser("transcrever"); s.add_argument("arquivo"); s.add_argument("--modelo", default="small")
    s.add_argument("--idioma", default="pt"); s.add_argument("--max-chars", type=int, default=32)
    s.add_argument("--saida"); s.set_defaults(f=cmd_transcrever)
    s = sp.add_parser("renderizar"); s.add_argument("projeto"); s.set_defaults(f=cmd_renderizar)
    s = sp.add_parser("verificar"); s.add_argument("arquivo"); s.set_defaults(f=cmd_verificar)
    s = sp.add_parser("capa"); s.add_argument("arquivo"); s.add_argument("--tempo", type=float, default=1)
    s.add_argument("--saida"); s.set_defaults(f=cmd_capa)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
