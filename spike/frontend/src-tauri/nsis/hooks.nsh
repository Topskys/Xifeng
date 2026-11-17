; Xifeng 安装器钩子（Tauri NSIS）
;
; 为什么需要：Tauri 的 perUser 安装默认落到 `$LOCALAPPDATA\${PRODUCTNAME}`，
; 而 Xifeng 的用户数据目录也叫 `%LOCALAPPDATA%\Xifeng`（D21）。两者相同会把
; exe/core 和 db/models 混在一个目录里，用户"删掉安装目录重装"就会顺手删掉历史。
;
; 这里只改**默认值**：用户没在目录页改动时，把安装位置挪到
; `%LOCALAPPDATA%\Programs\Xifeng`；用户手动选了别的目录则原样尊重。

!macro NSIS_HOOK_PREINSTALL
  ${If} $INSTDIR == "$LOCALAPPDATA\Xifeng"
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Xifeng"
    SetOutPath "$INSTDIR"
  ${EndIf}
!macroend
