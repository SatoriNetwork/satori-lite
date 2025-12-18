@echo off
setlocal enabledelayedexpansion

:: Check if Docker is installed
where docker >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Docker is not installed. Please install Docker first.
    echo Visit: https://docs.docker.com/get-docker/
    exit /b 1
)

:: Check if Docker is running
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Docker is not running. Please start Docker and try again.
    exit /b 1
)

:: Production mode - uses Docker image code
set IMAGE_TAG=latest
set IMAGE=satorinet/satori-lite:%IMAGE_TAG%
set BASE_PORT=24601

:: Parse instance number (default: 1)
set INSTANCE=1
set CMD=

:: Check if first arg is a number
set "ARG1=%~1"
if defined ARG1 (
    echo %ARG1%| findstr /r "^[0-9][0-9]*$" >nul 2>&1
    if !errorlevel! equ 0 (
        set INSTANCE=%ARG1%
        set "CMD=%~2"
    ) else (
        set "CMD=%ARG1%"
    )
)

set CONTAINER_NAME=satori-%INSTANCE%
set /a PORT=%BASE_PORT% + %INSTANCE% - 1
set DATA_DIR=%cd%\%INSTANCE%

:: Help
if "%CMD%"=="--help" goto :help
if "%CMD%"=="-h" goto :help

:: Commands
if "%CMD%"=="start" goto :start
if "%CMD%"=="stop" goto :stop
if "%CMD%"=="restart" goto :restart
if "%CMD%"=="logs" goto :logs
if "%CMD%"=="status" goto :status
if "%CMD%"=="" goto :cli
goto :unknown

:help
echo Satori Neuron
echo.
echo Usage: satori [instance] [command]
echo.
echo Instance:
echo   (number)    Instance number (default: 1)
echo.
echo Commands:
echo   (none)      Enter interactive CLI
echo   start       Start the neuron container
echo   stop        Stop the neuron container
echo   restart     Restart the neuron container
echo   logs        Show container logs
echo   status      Show container status
echo   --help      Show this help
echo.
echo Examples:
echo   satori          # Run instance 1 on port 24601
echo   satori 2        # Run instance 2 on port 24602
echo   satori 2 stop   # Stop instance 2
echo   satori 3 logs   # Show logs for instance 3
echo.
echo Data persists in: .\^<instance^>\config, wallet, models, data
exit /b 0

:start
docker start %CONTAINER_NAME%
echo Satori neuron %INSTANCE% started (port %PORT%)
exit /b 0

:stop
docker stop %CONTAINER_NAME% 2>nul
docker rm %CONTAINER_NAME% 2>nul
echo Satori neuron %INSTANCE% stopped and removed
exit /b 0

:restart
echo Restarting Satori neuron %INSTANCE%...
:: Stop and remove container
docker stop %CONTAINER_NAME% 2>nul
docker rm %CONTAINER_NAME% 2>nul

:: Create data directories
if not exist "%DATA_DIR%\config" mkdir "%DATA_DIR%\config"
if not exist "%DATA_DIR%\wallet" mkdir "%DATA_DIR%\wallet"
if not exist "%DATA_DIR%\models" mkdir "%DATA_DIR%\models"
if not exist "%DATA_DIR%\data" mkdir "%DATA_DIR%\data"

:: Pull latest image
echo Pulling latest image...
docker pull %IMAGE%

:: Create and run container
docker run -d --name %CONTAINER_NAME% ^
    --restart unless-stopped ^
    -p %PORT%:%PORT% ^
    -e SATORI_ENV=prod ^
    -e SATORI_UI_PORT=%PORT% ^
    -v "%DATA_DIR%\config:/Satori/Neuron/config" ^
    -v "%DATA_DIR%\wallet:/Satori/Neuron/wallet" ^
    -v "%DATA_DIR%\models:/Satori/models" ^
    -v "%DATA_DIR%\data:/Satori/Engine/db" ^
    %IMAGE%

if %errorlevel% neq 0 (
    echo Error: Failed to create container.
    exit /b 1
)

echo Satori neuron %INSTANCE% restarted with latest image (port %PORT%)
exit /b 0

:logs
docker logs %CONTAINER_NAME% --tail 100 -f
exit /b 0

:status
docker ps --format "{{.Names}}" | findstr /r "^%CONTAINER_NAME%$" >nul 2>&1
if %errorlevel% equ 0 (
    echo Satori neuron %INSTANCE% is running (port %PORT%)
    docker ps --filter "name=%CONTAINER_NAME%" --format "Container: {{.Names}}\nStatus: {{.Status}}\nCreated: {{.CreatedAt}}"
) else (
    echo Satori neuron %INSTANCE% is not running
)
exit /b 0

:cli
:: Check if container is running
docker ps --format "{{.Names}}" | findstr /r "^%CONTAINER_NAME%$" >nul 2>&1
if %errorlevel% equ 0 goto :run_cli

echo Satori neuron %INSTANCE% is not running. Starting on port %PORT%...

:: Try to start existing container
docker start %CONTAINER_NAME% >nul 2>&1
if %errorlevel% equ 0 (
    timeout /t 2 /nobreak >nul
    goto :run_cli
)

echo Container not found. Creating...

:: Create data directories
if not exist "%DATA_DIR%\config" mkdir "%DATA_DIR%\config"
if not exist "%DATA_DIR%\wallet" mkdir "%DATA_DIR%\wallet"
if not exist "%DATA_DIR%\models" mkdir "%DATA_DIR%\models"
if not exist "%DATA_DIR%\data" mkdir "%DATA_DIR%\data"

:: Pull latest image
echo Pulling latest image...
docker pull %IMAGE%

:: Create and run container
docker run -d --name %CONTAINER_NAME% ^
    --restart unless-stopped ^
    -p %PORT%:%PORT% ^
    -e SATORI_ENV=prod ^
    -e SATORI_UI_PORT=%PORT% ^
    -v "%DATA_DIR%\config:/Satori/Neuron/config" ^
    -v "%DATA_DIR%\wallet:/Satori/Neuron/wallet" ^
    -v "%DATA_DIR%\models:/Satori/models" ^
    -v "%DATA_DIR%\data:/Satori/Engine/db" ^
    %IMAGE%

if %errorlevel% neq 0 (
    echo Error: Failed to create container.
    exit /b 1
)

timeout /t 2 /nobreak >nul

:run_cli
docker exec -it %CONTAINER_NAME% python /Satori/Neuron/cli.py
exit /b 0

:unknown
echo Unknown command: %CMD%
echo Run 'satori --help' for usage
exit /b 1
