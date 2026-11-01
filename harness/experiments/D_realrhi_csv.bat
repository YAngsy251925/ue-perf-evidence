@echo off
rem D shape test (real RHI, D3D12): frame tracks, GPU timeline and CSV profiler header.
rem Opens a 960x540 game window on the desktop. Markers: "Capture Ended" "Capture Ended".
rem The CSV path is engine-chosen; read it from the "Capture Ended. Writing CSV to file :" log line.
rem Usage: D_realrhi_csv.bat <run_dir>    writes <run_dir>\engine.log and <run_dir>\trace.utrace
if "%~1"=="" (echo usage: %~nx0 ^<run_dir^> & exit /b 2)
if not defined UE_ROOT set "UE_ROOT=D:\Epic\Games\UE_5.8"
for %%I in ("%~dp0..\..\host\TraceDigestProbe\TraceDigestProbe.uproject") do set "UPROJECT=%%~fI"
set "RUN=%~f1"
"%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" "%UPROJECT%" /Engine/Maps/Entry -game -windowed -resx=960 -resy=540 -unattended -nosplash -nosound -statnamedevents -trace=cpu,frame,gpu,bookmark,file -tracefile="%RUN%\trace.utrace" -csvCaptureFrames=300 -stdout -ABSLOG="%RUN%\engine.log" -ExecCmds="t.MaxFPS 60"
echo EXITCODE=%ERRORLEVEL%
