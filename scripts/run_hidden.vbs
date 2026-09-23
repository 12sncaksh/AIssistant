Set shell = CreateObject("WScript.Shell")
cmd = Chr(34) & WScript.Arguments(0) & Chr(34) & " __hidden__"
shell.Run cmd, 0, False
