' Run the probe without a visible console, retaining logging and the actual exit code.
Option Explicit
Dim sh, fso, repo, q, probeCommand, command, exitCode
If WScript.Arguments.Count <> 1 Then WScript.Quit 2
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
repo = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = repo
q = Chr(34)
probeCommand = q & WScript.Arguments(0) & q & " -u -X utf8 quote_probe.py >> data\probe_runs.log 2>&1"
command = q & sh.ExpandEnvironmentStrings("%ComSpec%") & q & " /c " & q & probeCommand & q
exitCode = sh.Run(command, 0, True)
WScript.Quit exitCode
