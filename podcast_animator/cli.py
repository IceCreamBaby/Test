"""Kommandozeile.

    python -m podcast_animator                 -> startet die Oberfläche im Browser
    python -m podcast_animator process FILE    -> verarbeitet eine Datei komplett ohne Oberfläche
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "huggingface_hub", "faster_whisper", "uvicorn.access", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_serve(args) -> None:
    import uvicorn

    from .server import create_app

    app = create_app()
    url = f"http://{args.host if args.host not in ('0.0.0.0', '::') else '127.0.0.1'}:{args.port}"
    print("\n" + "=" * 62)
    print("  Podcast Animator läuft!")
    print(f"  Öffne im Browser:  {url}")
    print("  Zum Beenden dieses Fenster schließen (oder Strg+C).")
    print("=" * 62 + "\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_process(args) -> None:
    """Komplette Verarbeitung ohne Browser (z.B. für Stapelverarbeitung)."""
    from . import media
    from .pipeline import Pipeline, default_settings
    from .store import ProjectStore

    src = Path(args.input)
    if not src.is_file():
        sys.exit(f"Datei nicht gefunden: {src}")
    store = ProjectStore()
    settings = default_settings()
    settings.update({
        "mode": "single" if args.single else "auto",
        "model": args.model or settings["model"],
        "num_speakers": args.speakers,
        "clip_count": args.clips,
        "min_len": args.min_len,
        "max_len": args.max_len,
        "use_claude": args.claude,
    })
    if args.start or args.end:
        settings["range"] = [media.parse_ts(args.start), media.parse_ts(args.end)]
    settings["render"].update({"layout": args.layout, "theme": args.theme})
    p = store.create(src.stem, settings)
    store.update(p["id"], lambda q: q.update(source=str(src.resolve())))
    pipe = Pipeline(store)
    cancel = threading.Event()
    last = [0.0]

    def printer():
        while not cancel.is_set():
            live = (store.get(p["id"]) or {}).get("live")
            if live and time.time() - last[0] > 2:
                print(f"  [{live['step']}] {live['message']}")
                last[0] = time.time()
            time.sleep(0.5)

    threading.Thread(target=printer, daemon=True).start()
    try:
        pipe.process(p["id"], cancel)
        proj = store.get(p["id"])
        if args.chars:
            ids = [c.strip() for c in args.chars.split(",")]
            store.update(p["id"], lambda q: [sp.update(char=ids[i]) for i, sp in enumerate(q["speakers"])
                                             if i < len(ids)])
            proj = store.get(p["id"])
        print("\nSprecher:")
        for sp in proj["speakers"]:
            print(f"  Sprecher {sp['spk'] + 1}: {sp['talk_time']:.0f}s -> {sp['char']}  \"{sp['sample_text'][:60]}\"")
        print(f"\n{len(proj['clips'])} Clip(s):")
        out_dir = Path(args.out) if args.out else None
        for c in proj["clips"]:
            print(f"  {media.format_ts(c['start'])}–{media.format_ts(c['end'])}  {c['title']}")
            pipe.render(p["id"], c["id"], threading.Event())
            c = store.clip(p["id"], c["id"])
            video = store.dir(p["id"]) / c["video"]
            if out_dir:
                out_dir.mkdir(parents=True, exist_ok=True)
                for key in ("video", "srt", "thumb"):
                    shutil.copy2(store.dir(p["id"]) / c[key], out_dir / Path(c[key]).name)
                video = out_dir / Path(c["video"]).name
            print(f"    -> {video}")
    finally:
        cancel.set()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="podcast-animator",
                                     description="Podcast-Ausschnitte automatisch als animierte Shorts.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    sp = sub.add_parser("serve", help="Oberfläche im Browser starten (Standard)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=7860)
    sp.add_argument("--no-browser", action="store_true")

    pp = sub.add_parser("process", help="Datei ohne Oberfläche verarbeiten")
    pp.add_argument("input")
    pp.add_argument("--start", help="nur ab dieser Zeit (z.B. 1:02:30)")
    pp.add_argument("--end", help="nur bis zu dieser Zeit")
    pp.add_argument("--single", action="store_true", help="ganze Datei/Bereich als EIN Clip")
    pp.add_argument("--clips", type=int, default=5, help="Anzahl Clips (Automatik)")
    pp.add_argument("--min-len", type=float, default=20)
    pp.add_argument("--max-len", type=float, default=55)
    pp.add_argument("--model", help="Whisper-Modell: base, small, medium, large-v3-turbo")
    pp.add_argument("--speakers", type=int, default=2, help="Anzahl Sprecher (0 = automatisch)")
    pp.add_argument("--chars", help="Figuren in Sprecher-Reihenfolge, z.B. rezo,julien")
    pp.add_argument("--claude", action="store_true", help="Clips mit Claude auswählen (API-Key nötig)")
    pp.add_argument("--layout", default="studio", choices=["studio", "split"])
    pp.add_argument("--theme", default="lila")
    pp.add_argument("--out", help="Ausgabeordner für die fertigen Videos")

    argv = list(sys.argv[1:] if argv is None else argv)
    # ohne Unterbefehl -> "serve" (z.B. "start.bat --port 7861")
    i = 0
    while i < len(argv) and argv[i] in ("-v", "--verbose"):
        i += 1
    if i == len(argv) or argv[i] not in ("serve", "process", "-h", "--help"):
        argv.insert(i, "serve")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if args.cmd == "process":
        cmd_process(args)
    else:
        cmd_serve(args)
