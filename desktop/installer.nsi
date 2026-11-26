; Ease My Bill for Windows: one-click install for the current user (no admin rights needed).
Unicode true
!define APP "Ease My Bill"
!define EXE "Ease My Bill.exe"
!define VERSION "1.1.0"
!define UNINST "Software\Microsoft\Windows\CurrentVersion\Uninstall\EaseMyBill"

Name "${APP}"
OutFile "dist\EaseMyBill-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\Ease My Bill"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "build\icon.ico"
UninstallIcon "build\icon.ico"
SilentInstall normal
AutoCloseWindow true
ShowInstDetails nevershow
BrandingText "Ease My Bill"
Caption "Installing Ease My Bill"
VIProductVersion "1.1.0.0"
VIAddVersionKey "ProductName" "${APP}"
VIAddVersionKey "FileDescription" "Ease My Bill installer"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "Ease My Bill"
VIAddVersionKey "LegalCopyright" "Ease My Bill"

Section "Install"
  ; close every running copy (the app runs as several processes) so its files can be replaced
  nsExec::Exec 'taskkill /F /T /IM "${EXE}"'
  Sleep 1500
  nsExec::Exec 'taskkill /F /T /IM "${EXE}"'
  Sleep 1000
  SetOutPath "$INSTDIR"
  File /r "dist\win-unpacked\*.*"
  File "build\icon.ico"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortCut "$DESKTOP\${APP}.lnk" "$INSTDIR\${EXE}" "" "$INSTDIR\icon.ico" 0
  CreateDirectory "$SMPROGRAMS\${APP}"
  CreateShortCut "$SMPROGRAMS\${APP}\${APP}.lnk" "$INSTDIR\${EXE}" "" "$INSTDIR\icon.ico" 0
  CreateShortCut "$SMPROGRAMS\${APP}\Uninstall ${APP}.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNINST}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${UNINST}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST}" "Publisher" "Ease My Bill"
  WriteRegStr HKCU "${UNINST}" "DisplayIcon" "$INSTDIR\icon.ico"
  WriteRegStr HKCU "${UNINST}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINST}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST}" "NoRepair" 1
  Exec '"$INSTDIR\${EXE}"'
SectionEnd

Section "Uninstall"
  nsExec::Exec 'taskkill /F /T /IM "${EXE}"'
  Sleep 1500
  Delete "$DESKTOP\${APP}.lnk"
  RMDir /r "$SMPROGRAMS\${APP}"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "electron.app.Ease My Bill"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Ease My Bill"
  DeleteRegKey HKCU "${UNINST}"
  RMDir /r "$INSTDIR"
SectionEnd
