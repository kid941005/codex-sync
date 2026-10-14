' =====================================================================
'  codex-sync : silent daemon launcher
'
'  Starts cx_daemon.py with NO visible window.
'
'  WHY python.exe AND NOT pythonw.exe
'  ---------------------------------
'  pythonw.exe is a GUI-subsystem binary. On some Windows setups the
'  sockets/services it hosts are unreachable and the process is killed
'  or ignored in ways that are very hard to debug. python.exe (console
'  subsystem) is reliable, so we launch THAT and simply hide its console
'  window via WshShell.Run(..., 0, False). Window style 0 = SW_HIDE.
'
'  This script is what the Startup shortcut (created by
'  create-autostart.py) points at, so the daemon is already running by
'  the time Codex is opened after logon.
' =====================================================================
Option Explicit

Dim fso, shell, verDir, folder, best, cur, scriptDir, entry, py

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' ---- 1) locate the Python that ships with WorkBuddy (newest version) ----
verDir = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\binaries\python\versions"
best = ""
If fso.FolderExists(verDir) Then
  For Each folder In fso.GetFolder(verDir).SubFolders
    If fso.FileExists(folder.Path & "\python.exe") Then
      If best = "" Or folder.Name > best Then
        best = folder.Name
      End If
    End If
  Next
End If

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' ---- 2) if the bundled Python is missing, try whatever is on PATH ----
If best <> "" Then
  py = verDir & "\" & best & "\python.exe"
Else
  py = "python.exe"
End If

entry = scriptDir & "\cx_daemon.py"
If Not fso.FileExists(entry) Then
  ' Nothing to do - write a breadcrumb so the failure is diagnosable.
  On Error Resume Next
  Dim cfgDir, f
  cfgDir = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\codex-sync"
  If Not fso.FolderExists(cfgDir) Then fso.CreateFolder(cfgDir)
  Set f = fso.OpenTextFile(cfgDir & "\daemon.log", 8, True)
  f.WriteLine "[" & Now & "] ERR  cx_daemon.py not found at " & entry
  f.Close
  On Error GoTo 0
  WScript.Quit 1
End If

' ---- 3) launch hidden, do not wait ----
' 0 = hidden window, False = do not wait for it to finish
shell.CurrentDirectory = scriptDir
shell.Run """" & py & """ """ & entry & """", 0, False
