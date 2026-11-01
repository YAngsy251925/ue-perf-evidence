@echo off
rem B2 smoke (nullrhi): stats dumps with -statnamedevents bridging and a trace file.
rem Markers: "Single Frame" "Events history".
rem Usage: B2_smoke_bridge.bat <run_dir>    writes <run_dir>\engine.log and <run_dir>\trace.utrace
if "%~1"=="" (echo usage: %~nx0 ^<run_dir^> & exit /b 2)
if not defined UE_ROOT set "UE_ROOT=D:\Epic\Games\UE_5.8"
for %%I in ("%~dp0..\..\host\TraceDigestProbe\TraceDigestProbe.uproject") do set "UPROJECT=%%~fI"
set "RUN=%~f1"
"%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" "%UPROJECT%" /Engine/Maps/Entry -game -nullrhi -unattended -nosplash -nosound -statnamedevents -trace=cpu,frame,file -tracefile="%RUN%\trace.utrace" -tracetailmb=64 -stdout -ABSLOG="%RUN%\engine.log" -ExecCmds="stat dumpevents -ms=0.1,stat dumpframe -ms=0.1"
echo EXITCODE=%ERRORLEVEL%
