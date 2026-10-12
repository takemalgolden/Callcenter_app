# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['realtime_sync', 'app', 'flexible_optimizer', 'bulk_shift_delete', 'operator_shift_export', 'operator_shift_visibility', 'monthly_workdays', 'attendance_sync', 'legacy_operator_migration', 'server']
hiddenimports += collect_submodules('openpyxl')


a = Analysis(
    ['unified.py'],
    pathex=['C:/Users/takemaru/Downloads/シフト休憩提案アプリ統合版/shift_planner_app', 'C:/Users/takemaru/Downloads/シフト休憩提案アプリ統合版/break_engine'],
    binaries=[],
    datas=[('shift_planner_app/templates', 'shift_planner_app/templates'), ('break_engine/static', 'break_engine/static')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CallCenterUnified',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CallCenterUnified',
)
