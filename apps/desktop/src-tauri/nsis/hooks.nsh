; Xifeng 安装器钩子（Tauri NSIS）
;
; 默认落点：优先 `D:\Program Files (x86)\Xifeng`（该目录存在时），
; 否则回退 `%LOCALAPPDATA%\Programs\Xifeng`。
; 为什么不让默认落 `%LOCALAPPDATA%\Xifeng`：那是用户数据目录（D21），
; exe/core 与 db/models 混在一起，用户删安装目录重装就会顺手删掉历史。
; 只改**默认值**：用户在目录页手选了别的路径则原样尊重。

!macro NSIS_HOOK_PREINSTALL
  ${If} $INSTDIR == "$LOCALAPPDATA\Xifeng"
    ${If} ${FileExists} "D:\Program Files (x86)\*.*"
      StrCpy $INSTDIR "D:\Program Files (x86)\溪风"
    ${Else}
      StrCpy $INSTDIR "$LOCALAPPDATA\Programs\溪风"
    ${EndIf}
    SetOutPath "$INSTDIR"
  ${EndIf}
!macroend
