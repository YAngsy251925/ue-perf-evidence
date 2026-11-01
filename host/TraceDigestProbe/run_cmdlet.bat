@echo off
rem Run the TraceDigest commandlet (B2) on one trace.
rem Usage: run_cmdlet.bat <trace.utrace> <log_file>
if "%~2"=="" (echo usage: %~nx0 ^<trace.utrace^> ^<log_file^> & exit /b 2)
if not defined UE_ROOT set "UE_ROOT=D:\Epic\Games\UE_5.8"
set "TRACE=%~f1"
set "TRACE=%TRACE:\=/%"
"%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" "%~dp0TraceDigestProbe.uproject" -run=TraceDigest trace=%TRACE% -unattended -nosplash -nullrhi -nosound -stdout -ABSLOG="%~f2"
echo EXITCODE=%ERRORLEVEL%
