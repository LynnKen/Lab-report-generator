# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    # Only the derived, read-only approved_reference_pack is bundled from
    # data/ - NEVER data/reference_examples/ (raw PDFs, full-size source
    # photos, unapproved/draft/needs_edit/cannot_determine blocks, review
    # audit history). Regenerate the pack first: see
    # build_approved_reference_pack.py. The Reference Review ("בדיקת דוחות
    # ייחוס") tab is also disabled by default in a frozen build (see
    # config.REFERENCE_REVIEW_ENABLED) so it never tries to read the raw
    # dataset that isn't here.
    # templates/ also holds crane_refernces/, chassis_references/, and
    # actuator_refernces/ - the ORIGINAL client report DOCX files the
    # reference dataset was built from (~20MB of raw client material,
    # unused by any runtime code path: only empty_report_template.docx is
    # ever read, via config.CANONICAL_TEMPLATE_PATH). Bundle that one file
    # only - never the whole folder.
    datas=[
        ('data/style_guide.md', 'data'),
        ('data/approved_reference_pack', 'data/approved_reference_pack'),
        ('icons', 'icons'),
        ('prompts', 'prompts'),
        ('templates/empty_report_template.docx', 'templates'),
    ],
    hiddenimports=[],
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
    name='ReportGenerator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    name='ReportGenerator',
)
