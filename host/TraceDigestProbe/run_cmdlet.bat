@echo off
rem Run a commandlet of the host project (B2) on one trace.
rem Usage: run_cmdlet.bat <commandlet name> <trace.utrace> <log_file> [extra args ...]
rem   commandlet "TraceDigest"  -> -run=TraceDigest trace=...
rem   commandlet "TraceDigestPerf" -> -run=TraceDigestPerf trace=... prefix=... (pass prefix via extra args)
if "%~3"=="" (echo usage: %~nx0 ^<commandlet^> ^<trace.utrace^> ^<log_file^> [extra args ...] & exit /b 2)
if not defined UE_ROOT set "UE_ROOT=D:\Epic\Games\UE_5.8"
set "TRACE=%~f2"
set "TRACE=%TRACE:\=/%"
set "EXTRA=%~4 %~5 %~6 %~7 %~8 %~9"
"%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" "%~dp0TraceDigestProbe.uproject" -run=%~1 trace=%TRACE% %EXTRA% -unattended -nosplash -nullrhi -nosound -stdout -ABSLOG="%~f3"
echo EXITCODE=%ERRORLEVEL%
