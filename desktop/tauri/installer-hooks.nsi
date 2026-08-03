; Where Hearth installs itself.
;
; Tauri's NSIS template puts a per-user install in $LOCALAPPDATA\<productName>,
; which for this application is %LOCALAPPDATA%\Hearth. That is already taken:
; agent/hearth_paths.py resolves the DATA directory to exactly the same path,
; and it holds checkpoints, downloaded model weights, fetched GPU engines,
; staged updates and the sandbox scratch area. Installing on top of it puts an
; uninstaller in the same folder as the user's work, and gigabytes of weights
; inside a directory Windows lists as the program's install location.
;
; The Electron build installed to %LOCALAPPDATA%\Programs\Hearth, which is the
; convention for a per-user install and is what docs/licensing.md and
; THIRD-PARTY-NOTICES.md give as the path to the notices. This restores it.
;
; The hook runs after the template's SetOutPath and before the first File
; command, so setting $INSTDIR here and calling SetOutPath again is enough:
; everything the template does afterwards, including WriteUninstaller and the
; registry entries that the uninstaller and the next upgrade read back, uses
; the value set here.

!macro NSIS_HOOK_PREINSTALL
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
  CreateDirectory "$INSTDIR"
  SetOutPath "$INSTDIR"
!macroend

!macro NSIS_HOOK_POSTINSTALL
!macroend

!macro NSIS_HOOK_PREUNINSTALL
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
!macroend
