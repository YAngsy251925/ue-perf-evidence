@echo off
rem Build the editor target of the B2 host project (installed engine).
if not defined UE_ROOT set "UE_ROOT=D:\Epic\Games\UE_5.8"
call "%UE_ROOT%\Engine\Build\BatchFiles\Build.bat" TraceDigestProbeEditor Win64 Development -Project="%~dp0TraceDigestProbe.uproject" -WaitMutex
echo EXITCODE=%ERRORLEVEL%
