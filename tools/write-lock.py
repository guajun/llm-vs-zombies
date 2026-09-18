"""Explicitly refresh the dependency manifest after reviewing a dependency change."""
import hashlib
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
paths = ["game/original/PlantsVsZombies.exe", "game/original/PlantsVsZombies.dat",
         "game/original/main.pak", "game/original/bass.dll",
         "avz/framework/release/env2/2.9.2_2026_06_26.zip", "avz/runtime/bin/injector.exe",
         "experiments/scenarios/liangyi/game1_13.dat"]
files = []
for relative in paths:
    path = root / relative
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    files.append(dict(path=relative, sha256=digest, size=path.stat().st_size))
lock = dict(schema_version=1,
    avz_repository="https://github.com/vector-wlc/AsmVsZombies",
    avz_commit=subprocess.check_output(['git','-C',str(root/'avz/framework'),'rev-parse','HEAD'],text=True).strip(),
    avz_runtime_release="2.9.2_2026_06_26",
    game_source="D:/pvz/Plants_Vs_Zombies_V1.0.0.1051_EN",
    game_declared_version="English 1.0.0.1051 (source folder name; live verification pending)",
    toolchain=dict(name="LLVM-MinGW", release="20260908", target="i686-w64-mingw32",
        archive="https://github.com/mstorsjo/llvm-mingw/releases/download/20260908/llvm-mingw-20260908-ucrt-x86_64.zip",
        sha256="1bcf74d06b724aeecaa6412ca85f5b26fb1da770e7cdcefa9263c9c5c3ad34b6"),files=files)
(root/'dependencies.lock.json').write_text(json.dumps(lock,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('Locked',len(files),'files and AvZ commit',lock['avz_commit'])
