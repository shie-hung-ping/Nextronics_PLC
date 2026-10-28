# PyInstaller 設定：資料夾形式（onedir）、無主控台視窗。
# 建置：uv run python -m PyInstaller packaging/NextronicsPLC.spec --noconfirm
# 輸出：dist/NextronicsPLC/NextronicsPLC.exe

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    excludes=["pytest", "tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NextronicsPLC",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="NextronicsPLC")
