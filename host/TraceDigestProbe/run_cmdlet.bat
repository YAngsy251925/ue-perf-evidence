@echo off
rem Run a commandlet of the host project (B2) on one trace.
rem Usage: run_cmdlet.bat <commandlet name> <trace.utrace> <log_file> [extra args ...]
rem   TraceDigest      -> -run=TraceDigest trace <path>
rem   TraceDigestPerf  -> -run=TraceDigestPerf trace <path> mode frames|timers|timerstats out <abs.json> ...
rem
rem NOTE: cmd splits arguments on '=' ',' and ';', so write "mode frames", never "mode=frames".
rem       The host commandlet accepts both forms; this script forwards every trailing token as-is.
if "%~3"=="" (echo usage: %~nx0 ^<commandlet^> ^<trace.utrace^> ^<log_file^> [extra args ...] & exit /b 2)
if not defined UE_ROOT set "UE_ROOT=D:\Epic\Games\UE_5.8"
set "HERE=%~dp0"   rem SHIFT also moves %0 in cmd.exe -- capture %~dp0 first
set "CMDLET=%~1"
set "TRACE=%~f2"
set "TRACE=%TRACE:\=/%"
set "LOG=%~f3"
set "EXTRA="
shift
shift
shift
:collect
if "%~1"=="" goto run
set "EXTRA=%EXTRA% %~1"
shift
goto collect
:run
"%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" "%HERE%TraceDigestProbe.uproject" -run=%CMDLET% trace %TRACE% %EXTRA% -unattended -nosplash -nullrhi -nosound -stdout -ABSLOG="%LOG%"
echo EXITCODE=%ERRORLEVEL%
