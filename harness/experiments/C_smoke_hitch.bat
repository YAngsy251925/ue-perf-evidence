@echo off
rem C smoke (nullrhi): both hitch detectors, with memreport as the hitch inducer.
rem Markers: "Thread Hitch" "Hitch detected on frame".
rem Hitch snapshots and memreports land under host\TraceDigestProbe\Saved\Profiling (engine-chosen paths).
rem Usage: C_smoke_hitch.bat <run_dir>    writes <run_dir>\engine.log and <run_dir>\trace.utrace
if "%~1"=="" (echo usage: %~nx0 ^<run_dir^> & exit /b 2)
if not defined UE_ROOT set "UE_ROOT=D:\Epic\Games\UE_5.8"
for %%I in ("%~dp0..\..\host\TraceDigestProbe\TraceDigestProbe.uproject") do set "UPROJECT=%%~fI"
set "RUN=%~f1"
"%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" "%UPROJECT%" /Engine/Maps/Entry -game -nullrhi -unattended -nosplash -nosound -statnamedevents -trace=cpu,frame,bookmark,file -tracefile="%RUN%\trace.utrace" -tracetailmb=64 -stdout -ABSLOG="%RUN%\engine.log" -ExecCmds="t.HitchFrameTimeThreshold 20,stat dumphitches -start,snapshothitches -start,memreport"
echo EXITCODE=%ERRORLEVEL%
